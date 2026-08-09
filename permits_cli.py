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
        sites = sites.sort_values(
            ["load_mw", "gen_mw"], ascending=False, na_position="last"
        ).head(args.limit)
        columns = [
            "site_name", "operator", "county", "site_class", "gen_mw", "load_mw",
            "fuels", "sources", "n_members", "confidence",
        ]
        with_pandas_display(sites[columns])
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
    sub.add_argument("--limit", type=int, default=40)
    sub.set_defaults(func=cmd_sites)

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
