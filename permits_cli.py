#!/usr/bin/env python3
"""
permits_cli.py — build and query the TCEQ / ERCOT permit database.

Typical first run, no network needed:

    python permits_cli.py demo                 # synthetic data, end to end
    streamlit run permits_dashboard.py

Real data:

    python permits_cli.py fetch                # ERCOT MIS, where reachable
    # ...or drop exports into data/raw/ and:
    python permits_cli.py ingest --dir data/raw
    python permits_cli.py link
    python permits_cli.py stats
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.permits import db as dbmod          # noqa: E402
from src.permits import link as linkmod      # noqa: E402
from src.permits import status as statusmod  # noqa: E402
from src.permits import pipeline, seed       # noqa: E402

DEFAULT_DB = os.path.join("output", "permits.db")
DEFAULT_RAW = os.path.join("data", "raw")


def cmd_init(args):
    conn = dbmod.open_db(args.db)
    conn.close()
    print(f"Initialized {args.db} (schema v{dbmod.SCHEMA_VERSION})")
    return 0


def cmd_demo(args):
    seed.build_demo_db(args.db, raw_dir=args.dir, threshold=args.threshold)
    conn = dbmod.open_db(args.db)
    try:
        _print_stats(dbmod.stats(conn))
    finally:
        conn.close()
    print(f"\nDemo database ready: {args.db}")
    print("Launch the dashboard with:  streamlit run permits_dashboard.py")
    return 0


def cmd_ingest(args):
    if not args.file and not args.dir:
        print("error: pass --file PATH or --dir DIRECTORY", file=sys.stderr)
        return 2

    if args.file:
        source = args.source or pipeline.infer_source(args.file)
        if not source:
            print(
                f"error: could not infer a source from '{os.path.basename(args.file)}'. "
                f"Pass --source with one of: {', '.join(pipeline.ADAPTERS)}",
                file=sys.stderr,
            )
            return 2
        pipeline.ingest_file(
            args.db, source, args.file, sheet=args.sheet,
            min_acres=args.min_acres, force=args.force,
        )
    else:
        pipeline.ingest_dir(
            args.db, args.dir, min_acres=args.min_acres, force=args.force
        )

    if not args.no_link:
        print("Linking...")
        pipeline.relink(args.db, threshold=args.threshold)
    return 0


def cmd_fetch(args):
    sources = args.source and [args.source] or None
    os.makedirs(args.dir, exist_ok=True)
    pipeline.fetch_ercot(args.db, args.dir, sources=sources, force=args.force)
    if not args.no_link:
        print("Linking...")
        pipeline.relink(args.db, threshold=args.threshold)
    return 0


def cmd_link(args):
    pipeline.relink(args.db, threshold=args.threshold)
    return 0


def _print_stats(info):
    print(f"\nRecords:  {info['entities']}")
    print(f"Sites:    {info['sites']}")
    print(f"Links:    {info['links']}")
    if info.get("last_ingest"):
        print(f"Last ingest: {info['last_ingest']}")
    for title, key in (
        ("By source", "by_source"),
        ("By project kind", "by_kind"),
        ("By permit lifecycle", "by_lifecycle"),
        ("By authorization program", "by_program"),
        ("By site class", "by_site_class"),
    ):
        table = info.get(key) or {}
        if not table:
            continue
        print(f"\n{title}:")
        width = max(len(str(name)) for name in table)
        for name, count in table.items():
            print(f"  {str(name):<{width}}  {count}")


def cmd_stats(args):
    conn = dbmod.open_db(args.db)
    try:
        _print_stats(dbmod.stats(conn))
    finally:
        conn.close()
    return 0


def cmd_sites(args):
    conn = dbmod.open_db(args.db)
    try:
        sites = dbmod.load_sites(conn)
        if sites.empty:
            print("No sites yet. Run `ingest` then `link`, or `demo`.")
            return 0
        if args.klass:
            sites = sites[sites["site_class"] == args.klass]
        if args.county:
            sites = sites[
                sites["county"].fillna("").str.lower() == args.county.lower()
            ]
        if args.colocated:
            sites = sites[sites["site_class"] == linkmod.SITE_COLOCATED]
        if args.lifecycle:
            sites = sites[sites["lifecycle"] == args.lifecycle]
        if args.has_pending:
            sites = sites[sites["has_pending"] == 1]
        sites = sites.sort_values(
            ["load_mw", "gen_mw"], ascending=False, na_position="last"
        ).head(args.limit)
        columns = [
            "site_name", "operator", "county", "site_class", "lifecycle",
            "gen_mw_approved", "gen_mw_pending", "load_mw", "fuels", "programs",
            "n_members", "confidence",
        ]
        with_pandas_display(sites[columns])
    finally:
        conn.close()
    return 0


# Site-name patterns for standby generators at premises whose business is not
# electricity. Matched against UPPER(project_name) with SQL LIKE.
BACKUP_GENSET_PATTERNS = [
    "%WAL-MART%", "%WALMART%", "%SUPERCENTER%", "%SAM'S CLUB%", "%COSTCO%",
    "%KROGER%", "%WALGREEN%", "%DOLLAR GENERAL%", "%DOLLAR TREE%",
    "%HOSPITAL%", "%MEDICAL CENTER%", "%UNIVERSITY%", "% ISD%", "%SCHOOL%",
    "%NURSING%", "%APARTMENT%", "%CHURCH%",
]


def cmd_scrape(args):
    """Pull permit documents and extract unit MW / engine manufacturer."""
    from src.permits.sources import tceq_records

    conn = dbmod.open_db(args.db)
    try:
        if args.rn:
            targets = [(args.rn, None)]
        else:
            # Generation first, and within that the case-by-case programs, since
            # PSD and NSR files are the ones that carry unit tables.
            sql = ("SELECT DISTINCT regulated_entity, operator FROM entities "
                   "WHERE source='tceq_air' AND regulated_entity IS NOT NULL ")
            if not (args.from_file or args.program):
                # Only apply the generation heuristic when nothing more precise
                # was asked for; --from-file and --program are already targeted.
                sql += ("  AND (project_kind='generation' "
                        "       OR primary_business LIKE '%POWER%' "
                        "       OR primary_business LIKE '%ELECTRIC%' "
                        "       OR permit_program IN ('psd','nsr')) ")
            params = []
            if args.from_file:
                # Target everything ingested from one export — the cleanest way
                # to sweep TCEQ's own unit-rule pulls. The Electric Generating
                # Facilities rule is 984 regulated entities, all standard
                # permits, which is where reciprocating engines live; the
                # generation keyword filter above would miss most of them
                # because primary_business is only populated for enriched RNs.
                sql += (" AND source_file_id IN (SELECT file_id FROM source_files"
                        "  WHERE uri LIKE ?)")
                params.append(f"%{args.from_file}%")
            if args.program:
                wanted = [p.strip() for p in args.program.split(",") if p.strip()]
                sql += f" AND permit_program IN ({','.join('?' * len(wanted))})"
                params.extend(wanted)
            if args.lifecycle:
                sql += " AND lifecycle = ?"
                params.append(args.lifecycle)
            if args.skip_backup:
                # TCEQ's electric-generating-facilities unit rule covers any
                # site with a generator, so a big-box store's emergency genset
                # sits in the same list as a merchant peaker. Both are real
                # authorizations, but a Walmart's standby diesel is not a
                # generation project, and there are enough of them to spend a
                # sweep on before reaching anything that matters.
                for pattern in BACKUP_GENSET_PATTERNS:
                    sql += " AND UPPER(COALESCE(project_name, '')) NOT LIKE ?"
                    params.append(pattern)
            sql += (" ORDER BY CASE permit_program WHEN 'psd' THEN 0 "
                    "WHEN 'nonattainment_nsr' THEN 1 WHEN 'nsr' THEN 2 ELSE 3 END, "
                    # Then by how much the name claims to be generation: a sweep
                    # that is cut short should have spent its time on plants.
                    "CASE WHEN UPPER(COALESCE(project_name, '')) LIKE '%GENERATING%' "
                    "       OR UPPER(COALESCE(project_name, '')) LIKE '%ENERGY CENTER%' "
                    "       OR UPPER(COALESCE(project_name, '')) LIKE '%POWER%' "
                    "       OR UPPER(COALESCE(project_name, '')) LIKE '%PEAK%' "
                    "  THEN 0 ELSE 1 END "
                    "LIMIT ?")
            params.append(args.limit)
            targets = [(r[0], r[1]) for r in conn.execute(sql, params)]
    finally:
        conn.close()

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)

    # Resume: a full sweep is thousands of HTTP round trips against one state
    # query app, so it has to survive being interrupted. Anything already
    # recorded is skipped, and each entity is flushed as it completes.
    results = []
    done = set()
    if os.path.exists(args.out) and not args.restart:
        try:
            with open(args.out) as handle:
                results = json.load(handle)
            # Only entities that actually completed count as done. A transient
            # failure — the sandbox proxy restarting mid-run, for instance —
            # has to be retried, not frozen in as a permanent empty result.
            done = {
                r.get("regulated_entity") for r in results if not r.get("error")
            }
            results = [r for r in results if not r.get("error")]
        except (ValueError, OSError):
            results, done = [], set()

    todo = [(rn, op) for rn, op in targets if rn not in done]
    print(f"{len(targets)} entities selected, {len(done)} already done, "
          f"{len(todo)} to scrape")

    access = tceq_records.get_access_id()
    for index, (rn, operator) in enumerate(todo, 1):
        try:
            found = tceq_records.scrape_entity(
                rn, args.dir, max_docs=args.max_docs, access=access,
                delay=args.delay, verbose=False, keep_files=args.keep_files,
            )
        except Exception as exc:
            print(f"  {rn}: ERROR {type(exc).__name__}: {exc}", flush=True)
            found = {"regulated_entity": rn, "error": str(exc)[:150],
                     "max_mw": None, "manufacturers": [], "models": [],
                     "documents_total": 0, "documents_read": 0, "findings": []}
        if found.get("max_mw") or found.get("manufacturers"):
            print(f"  [{index}/{len(todo)}] {rn} "
                  f"{str(found.get('entity_name') or operator)[:30]:30} "
                  f"max_mw={found['max_mw']} mfr={found['manufacturers'][:3]}",
                  flush=True)
        results.append(found)
        if index % 5 == 0 or index == len(todo):
            with open(args.out, "w") as handle:
                json.dump(results, handle, indent=1)
            print(f"    ... {index}/{len(todo)} scraped", flush=True)

    with open(args.out, "w") as handle:
        json.dump(results, handle, indent=1)
    hits = sum(1 for r in results if r["max_mw"] or r["manufacturers"])
    print(f"\n{hits}/{len(results)} entities yielded MW or manufacturer data "
          f"-> {args.out}")
    return 0


def cmd_enrich(args):
    pipeline.enrich_registry(args.db, limit=args.limit, delay=args.delay,
                             lifecycle=args.lifecycle, county=args.county)
    if not args.no_link:
        print("Linking...")
        pipeline.relink(args.db)
    return 0


DEFAULT_PUCT_QUERIES = [
    "data center", "large load", "transmission line",
    "certificate of convenience and necessity", "generating", "energy storage",
    "economic development rate",
]


def cmd_puct(args):
    """Pull PUCT Interchange dockets and store them as entities.

    The default queries cover the case styles that touch generation and large
    load. `--parties` opens each docket to collect who filed in it, which is the
    only public place a named data-center operator meets a named utility —
    ERCOT publishes its large-load queue in aggregate.
    """
    import pandas as pd

    from src.permits.sources import puct

    queries = args.query or DEFAULT_PUCT_QUERIES
    frames = []
    for query in queries:
        try:
            frame = puct.search(
                case_style=query, date_from=args.since, date_to=args.until,
                utility_type=args.utility_type,
            )
            print(f"  '{query}': {len(frame)} dockets")
            frames.append(frame)
        except Exception as exc:
            print(f"  ERROR '{query}': {exc}")

    if not frames:
        print("no dockets returned")
        return 1

    df = pd.concat(frames, ignore_index=True).drop_duplicates("control_number")
    print(f"  {len(df)} distinct dockets")

    if args.parties:
        print("Fetching docket filing lists...")
        df = puct.enrich_dockets(df, delay=args.delay, limit=args.limit)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    df.to_csv(args.out, index=False)
    print(f"  wrote {args.out}")

    if args.no_ingest:
        return 0

    conn = dbmod.open_db(args.db)
    try:
        file_id = dbmod.record_source_file(
            conn, puct.SOURCE, uri=os.path.abspath(args.out), sha256=None,
            n_rows=len(df), notes="PUCT Interchange docket search",
        )
        records = puct.to_entities(
            df, source_file_id=file_id, relevant_only=not args.all,
        )
        inserted, updated = dbmod.upsert_entities(conn, records)
        print(f"  puct: {len(records)} records ({inserted} new, {updated} updated)")
    finally:
        conn.close()

    if not args.no_link:
        print("Linking...")
        pipeline.relink(args.db)
    return 0


def cmd_timing(args):
    """How long decided authorizations took, from filing to decision."""
    import pandas as pd

    conn = dbmod.open_db(args.db)
    try:
        records = dbmod.load_entities(conn)
        if records.empty:
            print("No records yet.")
            return 0

        decided = records[records["lifecycle"].isin(statusmod.AUTHORIZED)].copy()
        decided["days"] = [
            statusmod.days_to_decision(r, d)
            for r, d in zip(decided["received_date"], decided["decision_date"])
        ]
        decided = decided[decided["days"].notna()]
        if args.program:
            decided = decided[decided["permit_program"] == args.program]
        if decided.empty:
            print("No decided records carry both a filing and a decision date.")
            return 0

        def summary(frame):
            return pd.Series({
                "n": len(frame),
                "median": frame["days"].median(),
                "mean": round(frame["days"].mean(), 1),
                "p90": frame["days"].quantile(0.90),
                "max": frame["days"].max(),
            })

        print(f"Decided authorizations with both dates: {len(decided)}")
        print(f"Median {decided['days'].median():.0f} days, "
              f"p90 {decided['days'].quantile(0.9):.0f}, "
              f"max {decided['days'].max():.0f}\n")

        for label, key in (("By program", "permit_program"),
                           ("By action", "permit_action")):
            grouped = decided.groupby(key).apply(summary, include_groups=False)
            grouped = grouped.sort_values("n", ascending=False).head(args.limit)
            print(f"{label}:")
            with_pandas_display(grouped.reset_index())
            print()

        # Still-pending applications, measured against the same clock.
        pending = records[records["lifecycle"].isin(statusmod.IN_PROCESS)].copy()
        pending["days"] = pending["received_date"].apply(statusmod.days_sitting)
        pending = pending[pending["days"].notna()]
        if not pending.empty:
            print(
                f"For comparison, {len(pending)} applications are still in "
                f"process, median {pending['days'].median():.0f} days sitting, "
                f"longest {pending['days'].max():.0f}."
            )
    finally:
        conn.close()
    return 0


def cmd_applications(args):
    """List authorizations still in process, oldest filing first."""
    conn = dbmod.open_db(args.db)
    try:
        records = dbmod.load_entities(conn)
        if records.empty:
            print("No records yet. Run `ingest` then `link`, or `demo`.")
            return 0

        pending = records[records["lifecycle"].isin(statusmod.IN_PROCESS)].copy()
        if args.program:
            pending = pending[pending["permit_program"] == args.program]
        if args.source:
            pending = pending[pending["source"] == args.source]
        if args.county:
            pending = pending[
                pending["county"].fillna("").str.lower() == args.county.lower()
            ]

        pending["days_sitting"] = pending["received_date"].apply(statusmod.days_sitting)
        if args.min_days:
            pending = pending[pending["days_sitting"].fillna(-1) >= args.min_days]

        if pending.empty:
            print("Nothing pending under those filters.")
            return 0

        pending = pending.sort_values(
            "days_sitting", ascending=False, na_position="last"
        ).head(args.limit)
        columns = [
            "project_name", "operator", "county", "permit_program", "permit_action",
            "stage", "received_date", "days_sitting", "capacity_mw", "load_mw",
            "fuel", "permit_number",
        ]
        with_pandas_display(pending[columns])
        print(
            f"\n{len(pending)} application(s) in process. "
            "days_sitting is measured from the filing date to today; blank means "
            "the export carried no received date."
        )
    finally:
        conn.close()
    return 0


def with_pandas_display(df):
    import pandas as pd

    with pd.option_context(
        "display.max_rows", 200, "display.max_columns", 40, "display.width", 200
    ):
        print(df.to_string(index=False))


def cmd_export(args):
    conn = dbmod.open_db(args.db)
    try:
        table = args.table
        if table == "sites":
            df = dbmod.load_sites(conn)
        elif table == "entities":
            df = dbmod.load_entities(conn)
        elif table == "links":
            import pandas as pd

            df = pd.read_sql_query("SELECT * FROM entity_links", conn)
        else:
            print(f"error: unknown table '{table}'", file=sys.stderr)
            return 2

        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        if args.out.lower().endswith(".json"):
            df.to_json(args.out, orient="records", indent=2)
        elif args.out.lower().endswith((".xlsx", ".xlsm")):
            df.to_excel(args.out, index=False)
        else:
            df.to_csv(args.out, index=False)
        print(f"Wrote {len(df)} rows to {args.out}")
    finally:
        conn.close()
    return 0


def cmd_explain(args):
    conn = dbmod.open_db(args.db)
    try:
        members = dbmod.load_site_detail(conn, args.site_id)
        if members.empty:
            print(f"No site {args.site_id}")
            return 1
        print(f"Site {args.site_id}: {len(members)} member record(s)\n")
        for _, row in members.iterrows():
            print(f"  [{row['source']}] {row['project_name']}")
            print(f"      operator : {row['operator']}")
            print(f"      kind     : {row['project_kind']} "
                  f"({row['kind_confidence']}) — {row['kind_evidence']}")
            if row["fuel"]:
                print(f"      fuel     : {row['fuel']} / {row['technology']}")
            if row["capacity_mw"]:
                print(f"      gen MW   : {row['capacity_mw']}")
            if row["load_mw"]:
                print(f"      load MW  : {row['load_mw']}")
            print(f"      geo      : {row['latitude']}, {row['longitude']} "
                  f"({row['geo_precision']})")
            print()
        links = linkmod.explain_site(conn, args.site_id)
        if links:
            print("Links holding this site together:")
            for link in links:
                distance = (
                    f"{link['distance_km']:.2f} km"
                    if link["distance_km"] is not None else "n/a"
                )
                print(
                    f"  {link['score']:.3f}  {link['method']:<24} dist={distance:<10} "
                    f"name={link['name_score']:.2f} operator={link['operator_score']:.2f}"
                )
                print(f"      {link['source_a']}: {link['name_a']}")
                print(f"      {link['source_b']}: {link['name_b']}")
        else:
            print("Single-record site (no links).")
    finally:
        conn.close()
    return 0


def cmd_sources(args):
    print("Registered sources:\n")
    for source, meta in pipeline.ADAPTERS.items():
        print(f"  {source:<18} {meta['label']}")
    print(
        "\nFilenames are matched to sources automatically by `ingest --dir`;\n"
        "use --source to override."
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="permits_cli.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"database path (default {DEFAULT_DB})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_threshold(sub):
        sub.add_argument(
            "--threshold", type=float, default=linkmod.DEFAULT_THRESHOLD,
            help="minimum link score to merge two records into one site "
                 f"(default {linkmod.DEFAULT_THRESHOLD})",
        )

    sub = subparsers.add_parser("init", help="create an empty database")
    sub.set_defaults(func=cmd_init)

    sub = subparsers.add_parser(
        "demo", help="generate synthetic data and build a complete database"
    )
    sub.add_argument("--dir", default=None, help="where to write the demo exports")
    add_threshold(sub)
    sub.set_defaults(func=cmd_demo)

    sub = subparsers.add_parser("ingest", help="ingest downloaded exports")
    sub.add_argument("--file", help="single file to ingest")
    sub.add_argument("--dir", help="directory of files to ingest")
    sub.add_argument("--source", choices=list(pipeline.ADAPTERS),
                     help="override source detection")
    sub.add_argument("--sheet", help="worksheet name for xlsx inputs")
    sub.add_argument("--min-acres", type=float, default=None,
                     help="drop stormwater NOIs below this disturbed acreage")
    sub.add_argument("--force", action="store_true",
                     help="re-ingest even if this file was already loaded")
    sub.add_argument("--no-link", action="store_true", help="skip relinking afterwards")
    add_threshold(sub)
    sub.set_defaults(func=cmd_ingest)

    sub = subparsers.add_parser("fetch", help="download ERCOT reports and ingest them")
    sub.add_argument("--dir", default=DEFAULT_RAW, help="download directory")
    sub.add_argument("--source", choices=[pipeline.SOURCE_GIS, pipeline.SOURCE_LARGE_LOAD])
    sub.add_argument("--force", action="store_true")
    sub.add_argument("--no-link", action="store_true")
    add_threshold(sub)
    sub.set_defaults(func=cmd_fetch)

    sub = subparsers.add_parser("link", help="rebuild links and sites")
    add_threshold(sub)
    sub.set_defaults(func=cmd_link)

    sub = subparsers.add_parser("stats", help="summarize the database")
    sub.set_defaults(func=cmd_stats)

    sub = subparsers.add_parser("sites", help="list resolved sites")
    sub.add_argument("--class", dest="klass", help="filter by site_class")
    sub.add_argument("--county")
    sub.add_argument("--colocated", action="store_true",
                     help="only sites with both generation and load")
    sub.add_argument("--lifecycle", choices=statusmod.LIFECYCLE_ORDER,
                     help="filter by the site's most advanced permit lifecycle")
    sub.add_argument("--has-pending", action="store_true",
                     help="only sites with an application still in process")
    sub.add_argument("--limit", type=int, default=40)
    sub.set_defaults(func=cmd_sites)

    sub = subparsers.add_parser(
        "scrape", help="pull permit documents; extract unit MW and manufacturer"
    )
    sub.add_argument("--rn", help="scrape one regulated entity")
    sub.add_argument("--from-file", dest="from_file",
                     help="scrape entities from one ingested export, matched on "
                          "its filename, e.g. electric_generating")
    sub.add_argument("--program",
                     help="comma-separated authorization programs, e.g. "
                          "psd,nonattainment_nsr — the major-source filings are "
                          "the ones carrying unit tables")
    sub.add_argument("--lifecycle", help="restrict to e.g. pending")
    sub.add_argument("--limit", type=int, default=25)
    sub.add_argument("--max-docs", type=int, default=6,
                     help="documents to open per entity")
    sub.add_argument("--delay", type=float, default=1.0)
    sub.add_argument("--dir", default=os.path.join("data", "raw", "tceq_docs"))
    sub.add_argument("--out", default=os.path.join("output", "permit_units.json"))
    sub.add_argument("--keep-files", action="store_true",
                     help="retain downloaded PDFs; off by default because a "
                          "statewide sweep is hundreds of GB")
    sub.add_argument("--restart", action="store_true",
                     help="ignore prior results and scrape everything again")
    sub.add_argument("--skip-backup", action="store_true",
                     help="drop standby gensets at stores, schools and "
                          "hospitals — they hold the same unit-rule "
                          "authorization as a peaker but are not projects")
    sub.set_defaults(func=cmd_scrape)

    sub = subparsers.add_parser(
        "enrich", help="add real site names + business class from Central Registry"
    )
    sub.add_argument("--limit", type=int, default=None,
                     help="stop after this many new lookups")
    sub.add_argument("--delay", type=float, default=0.3)
    sub.add_argument("--lifecycle",
                     help="only enrich records in this state, e.g. pending")
    sub.add_argument("--county")
    sub.add_argument("--no-link", action="store_true")
    sub.set_defaults(func=cmd_enrich)

    sub = subparsers.add_parser(
        "puct", help="pull PUCT Interchange dockets (CCN, transmission, large load)"
    )
    sub.add_argument("--query", action="append",
                     help="case-style search term; repeatable (default: a "
                          "generation/large-load set)")
    sub.add_argument("--since", default="01/01/2023", help="MM/DD/YYYY")
    sub.add_argument("--until", default=None, help="MM/DD/YYYY")
    sub.add_argument("--utility-type", default="Electric",
                     help="Electric, Water, Telephone, Others or All")
    sub.add_argument("--parties", action="store_true",
                     help="open each docket for its filing parties and dates")
    sub.add_argument("--limit", type=int, default=None,
                     help="with --parties, stop after this many dockets")
    sub.add_argument("--delay", type=float, default=0.5)
    sub.add_argument("--all", action="store_true",
                     help="keep dockets unrelated to generation or load")
    sub.add_argument("--out", default=os.path.join(DEFAULT_RAW, "puct_dockets.csv"))
    sub.add_argument("--no-ingest", action="store_true")
    sub.add_argument("--no-link", action="store_true")
    sub.set_defaults(func=cmd_puct)

    sub = subparsers.add_parser(
        "timing", help="how long decided permits took, filing to decision"
    )
    sub.add_argument("--from-file", dest="from_file",
                     help="scrape entities from one ingested export, matched on "
                          "its filename, e.g. electric_generating")
    sub.add_argument("--program", help="restrict to one authorization program")
    sub.add_argument("--limit", type=int, default=12)
    sub.set_defaults(func=cmd_timing)

    sub = subparsers.add_parser(
        "applications", help="list authorizations still sitting in process"
    )
    sub.add_argument("--from-file", dest="from_file",
                     help="scrape entities from one ingested export, matched on "
                          "its filename, e.g. electric_generating")
    sub.add_argument("--program", choices=statusmod.AIR_PROGRAMS + [
        statusmod.PROGRAM_STORMWATER, statusmod.PROGRAM_INTERCONNECTION,
    ], help="filter by authorization program")
    sub.add_argument("--source", choices=list(pipeline.ADAPTERS))
    sub.add_argument("--county")
    sub.add_argument("--min-days", type=int, default=None,
                     help="only applications filed at least this many days ago")
    sub.add_argument("--limit", type=int, default=40)
    sub.set_defaults(func=cmd_applications)

    sub = subparsers.add_parser("explain", help="show why one site's records were merged")
    sub.add_argument("site_id")
    sub.set_defaults(func=cmd_explain)

    sub = subparsers.add_parser("export", help="write a table to csv/xlsx/json")
    sub.add_argument("--table", default="sites", choices=["sites", "entities", "links"])
    sub.add_argument("--out", default="output/permit_sites.csv")
    sub.set_defaults(func=cmd_export)

    sub = subparsers.add_parser("sources", help="list registered sources")
    sub.set_defaults(func=cmd_sources)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
