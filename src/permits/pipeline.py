"""
pipeline.py — ingest orchestration.

    ingest_file(db_path, source, path)   one downloaded export -> entities
    ingest_dir(db_path, directory)       everything in data/raw, source inferred
    refresh(db_path)                     fetch what can be fetched, then ingest
    relink(db_path)                      rebuild links and sites only

Sources are registered in ADAPTERS; adding a feed means adding one entry.
"""

import os
import re

from . import db as dbmod
from . import normalize
from . import link as linkmod
from .sources import ercot, puct, tceq_air, tceq_registry, tceq_stormwater

SOURCE_GIS = ercot.SOURCE_GIS
SOURCE_LARGE_LOAD = ercot.SOURCE_LARGE_LOAD
SOURCE_TCEQ_AIR = tceq_air.SOURCE
SOURCE_TCEQ_SWNOI = tceq_stormwater.SOURCE
SOURCE_PUCT = puct.SOURCE

ADAPTERS = {
    SOURCE_GIS: {
        "label": dbmod.SOURCES[SOURCE_GIS],
        "read": ercot.read_gis,
        "to_entities": ercot.gis_to_entities,
    },
    SOURCE_LARGE_LOAD: {
        "label": dbmod.SOURCES[SOURCE_LARGE_LOAD],
        "read": ercot.read_large_load,
        "to_entities": ercot.large_load_to_entities,
    },
    SOURCE_TCEQ_AIR: {
        "label": dbmod.SOURCES[SOURCE_TCEQ_AIR],
        "read": tceq_air.read_file,
        "to_entities": tceq_air.to_entities,
    },
    SOURCE_TCEQ_SWNOI: {
        "label": dbmod.SOURCES[SOURCE_TCEQ_SWNOI],
        "read": tceq_stormwater.read_file,
        "to_entities": tceq_stormwater.to_entities,
    },
    SOURCE_PUCT: {
        "label": dbmod.SOURCES[SOURCE_PUCT],
        "read": puct.read_file,
        "to_entities": puct.to_entities,
    },
}

# Filename hints used by ingest_dir when --source is not given.
_FILENAME_HINTS = [
    (SOURCE_LARGE_LOAD, r"large[_\- ]?load|load[_\- ]?interconnect"),
    (SOURCE_GIS, r"\bgis\b|generator[_\- ]?interconnect|rpt[_\- ]?00015933"),
    (SOURCE_PUCT, r"puct|interchange|\bccn\b|docket"),
    (SOURCE_TCEQ_SWNOI, r"txr150|stormwater|storm[_\- ]?water|\bnoi\b|construction"),
    (SOURCE_TCEQ_AIR, r"\bair\b|\bnsr\b|permit|tceq"),
]


def infer_unit_rule(path):
    """Recover the TCEQ unit rule from an export's filename, or None."""
    name = os.path.basename(str(path)).lower()
    for rule in tceq_air.UNIT_RULES:
        if rule in name:
            return rule
    return None


def infer_source(path):
    """Guess the source from a filename, or None."""
    name = os.path.basename(str(path)).lower()
    for source, pattern in _FILENAME_HINTS:
        if re.search(pattern, name):
            return source
    return None


def ingest_file(
    db_path,
    source,
    path,
    sheet=None,
    min_acres=None,
    force=False,
    verbose=True,
    conn=None,
):
    """Read one export and upsert its rows. Returns a summary dict.

    Skips files whose sha256 was already ingested for this source unless `force`,
    so re-running against a directory of monthly reports is cheap.
    """
    if source not in ADAPTERS:
        raise ValueError(f"unknown source '{source}'; expected one of {list(ADAPTERS)}")

    owns_conn = conn is None
    conn = conn or dbmod.open_db(db_path)
    try:
        digest = dbmod.file_sha256(path)
        if not force and dbmod.already_ingested(conn, source, digest):
            if verbose:
                print(f"  skip (already ingested): {os.path.basename(path)}")
            return {"source": source, "path": path, "skipped": True,
                    "rows": 0, "inserted": 0, "updated": 0}

        adapter = ADAPTERS[source]
        df = adapter["read"](path, sheet=sheet) if sheet else adapter["read"](path)

        file_id = dbmod.record_source_file(
            conn, source, uri=os.path.abspath(path), sha256=digest,
            n_rows=len(df), notes=adapter["label"],
        )

        kwargs = {"source_file_id": file_id}
        if source == SOURCE_TCEQ_SWNOI and min_acres is not None:
            kwargs["min_acres"] = min_acres
        if source == SOURCE_TCEQ_AIR:
            # TCEQ's unit-rule pulls are named for the rule they used, and the
            # rule is evidence: an "electric generating facilities" export is
            # the agency asserting every row generates electricity, which no
            # amount of reading a company name can recover.
            rule = infer_unit_rule(path)
            if rule:
                kwargs["unit_rule"] = rule
        records = adapter["to_entities"](df, **kwargs)

        inserted, updated = dbmod.upsert_entities(conn, records)
        if verbose:
            print(
                f"  {source}: {len(df)} rows -> {len(records)} records "
                f"({inserted} new, {updated} updated) from {os.path.basename(path)}"
            )
        return {
            "source": source, "path": path, "skipped": False, "rows": len(df),
            "records": len(records), "inserted": inserted, "updated": updated,
            "file_id": file_id,
        }
    finally:
        if owns_conn:
            conn.close()


def ingest_dir(db_path, directory, min_acres=None, force=False, verbose=True):
    """Ingest every recognizable export in a directory. Returns a list of summaries."""
    if not os.path.isdir(directory):
        raise NotADirectoryError(directory)

    results = []
    conn = dbmod.open_db(db_path)
    try:
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            if not name.lower().endswith(
                (".csv", ".xlsx", ".xlsm", ".xls", ".html", ".htm", ".json")
            ):
                continue
            source = infer_source(path)
            if source is None:
                if verbose:
                    print(f"  skip (source not recognized from name): {name}")
                continue
            try:
                results.append(
                    ingest_file(
                        db_path, source, path, min_acres=min_acres, force=force,
                        verbose=verbose, conn=conn,
                    )
                )
            except (ValueError, KeyError, OSError) as exc:
                # One malformed export should not abort the whole directory.
                print(f"  ERROR ingesting {name} as {source}: {exc}")
    finally:
        conn.close()
    return results


def fetch_ercot(db_path, dest_dir, sources=None, verbose=True, **ingest_kwargs):
    """Download the newest ERCOT workbooks and ingest them.

    Needs outbound access to www.ercot.com. Failures are reported per source
    rather than raised, so a blocked large-load report does not stop the GIS pull.
    """
    sources = sources or [SOURCE_GIS, SOURCE_LARGE_LOAD]
    results = []
    for source in sources:
        try:
            path = ercot.fetch_latest(source, dest_dir)
            if verbose:
                print(f"  downloaded {source}: {os.path.basename(path)}")
            results.append(ingest_file(db_path, source, path, verbose=verbose,
                                       **ingest_kwargs))
        except Exception as exc:  # network, MIS layout, or parse failure
            print(f"  ERROR fetching {source}: {exc}")
            results.append({"source": source, "error": str(exc)})
    return results


def enrich_registry(db_path, cache_path=None, limit=None, delay=0.3, verbose=True,
                    lifecycle=None, county=None):
    """Fill in real site names and TCEQ's own business classification.

    TCEQ's permit search gives a company name and an RN but never the site's own
    name, and never says what the site does. Central Registry has both. This
    writes the site name into `project_name` (keeping the company in `operator`),
    stores the business classification, and re-geocodes to the ZIP centroid where
    Central Registry supplies a ZIP.
    """
    conn = dbmod.open_db(db_path)
    try:
        # Central Registry is one round trip per RN with no bulk endpoint, so a
        # statewide sweep is hours of traffic against a single query app.
        # Filtering to what is being asked about keeps it to minutes.
        sql = ("SELECT entity_id, regulated_entity FROM entities "
               "WHERE source = 'tceq_air' AND regulated_entity IS NOT NULL")
        params = []
        if lifecycle:
            sql += " AND lifecycle = ?"
            params.append(lifecycle)
        if county:
            sql += " AND county_norm = ?"
            params.append(county.lower())
        rows = [dict(row) for row in conn.execute(sql, params)]
        rns = [row["regulated_entity"] for row in rows]
        cache = tceq_registry.enrich(
            rns, cache_path=cache_path or tceq_registry.DEFAULT_CACHE,
            limit=limit, delay=delay, verbose=verbose,
        )

        updates = []
        for row in rows:
            record = cache.get(str(row["regulated_entity"]))
            if not record or record.get("not_found") or record.get("error"):
                continue
            name = record.get("name")
            business = record.get("primary_business")
            zip_code = record.get("near_zip_code")
            latitude, longitude = normalize.zip_centroid(zip_code)
            updates.append((
                name, normalize.norm_project(name) if name else None,
                business, zip_code, latitude, longitude,
                "zip" if latitude is not None else None,
                row["entity_id"],
            ))

        conn.executemany(
            "UPDATE entities SET "
            "  project_name   = COALESCE(?, project_name), "
            "  name_norm      = COALESCE(?, name_norm), "
            "  primary_business = COALESCE(?, primary_business), "
            "  zip_code       = COALESCE(?, zip_code), "
            "  latitude       = COALESCE(?, latitude), "
            "  longitude      = COALESCE(?, longitude), "
            "  geo_precision  = COALESCE(?, geo_precision) "
            "WHERE entity_id = ?",
            updates,
        )
        conn.commit()
        if verbose:
            print(f"  enriched {len(updates)} records from Central Registry")
        return {"enriched": len(updates), "cached": len(cache)}
    finally:
        conn.close()


def relink(db_path, threshold=linkmod.DEFAULT_THRESHOLD, verbose=True):
    """Rebuild entity links and sites from the entities already stored."""
    conn = dbmod.open_db(db_path)
    try:
        summary = linkmod.rebuild_sites(conn, threshold=threshold, verbose=verbose)
        if verbose:
            print(
                f"  linked {summary['entities']} records into {summary['sites']} sites "
                f"({summary['colocated']} colocated gen+load)"
            )
        return summary
    finally:
        conn.close()


def refresh(
    db_path,
    raw_dir="data/raw",
    fetch=False,
    threshold=linkmod.DEFAULT_THRESHOLD,
    min_acres=None,
    force=False,
    verbose=True,
):
    """Full run: optional fetch, ingest everything in raw_dir, then relink."""
    if verbose:
        print(f"Refreshing {db_path}")
    if fetch:
        if verbose:
            print("Fetching ERCOT reports...")
        fetch_ercot(db_path, raw_dir, verbose=verbose, force=force)
    if os.path.isdir(raw_dir):
        if verbose:
            print(f"Ingesting {raw_dir}...")
        ingest_dir(db_path, raw_dir, min_acres=min_acres, force=force, verbose=verbose)
    if verbose:
        print("Linking...")
    return relink(db_path, threshold=threshold, verbose=verbose)


def run(db_path="output/permits.db", **kwargs):
    """Convenience entry point used by the dashboard's refresh button."""
    return refresh(db_path, **kwargs)


def unit_payload(result):
    """Database columns for one scraped entity, or None when it carries nothing.

    Drawn from the per-line unit rows, never from the whole-text summary. The
    summary fields are a scan of everything in the document and are the one
    place the foreign-RN, precedent-row and clearinghouse guards do not reach,
    so a manufacturer that only ever appears in a BACT comparison table would
    arrive as though it were installed here. The unit rows cost nothing in
    coverage -- both give 79 of 99 sites -- and they are attributable.

    Only same-line pairings are rated at all. "nearby" is kept upstream as
    evidence of presence but is not strong enough to attribute a number to, and
    prose megawatts are refused outright: over one 99-entity sweep they produced
    66,675 MW for a 600 MW station and a run of 3,000-4,300 figures for plants a
    fifth that size, because a permit application is required to quote other
    plants' capacities in its BACT demonstration.

    The two surviving ratings go to separate columns because they are separate
    quantities, and merging them reads as one. A horsepower rating converts to
    the output of a single engine -- median 1.1 MW across this sweep, right for
    a reciprocating unit. A megawatt figure printed beside a manufacturer is
    usually the block or the station -- median 172 MW -- because that is the
    unit an electrical rating is quoted in. Taking the larger of the two would
    have made every site with both look like it had one enormous engine.
    """
    from . import classify

    units = result.get("units") or []
    if not units:
        return None

    makers = sorted({
        classify.canonical_maker(unit["manufacturer"]) or unit["manufacturer"]
        for unit in units if unit.get("manufacturer")
    })
    models = sorted({unit["model"] for unit in units if unit.get("model")})
    rated = [unit for unit in units if unit.get("proximity") == "same_line"]
    per_unit = [unit["mw_from_hp"] for unit in rated if unit.get("mw_from_hp")]
    stated = [unit["mw"] for unit in rated if unit.get("mw")]
    if not (makers or models or per_unit or stated):
        return None
    return {
        "unit_manufacturers": ", ".join(makers) or None,
        "unit_models": ", ".join(models[:12]) or None,
        "unit_mw_table": max(per_unit) if per_unit else None,
        "unit_mw_stated": max(stated) if stated else None,
        "unit_count": len(per_unit) + len(stated) or None,
    }


def merge_unit_results(db_path, path, verbose=True):
    """Write scraped unit data onto every tceq_air row for each entity.

    A regulated entity holds one set of equipment and usually several permit
    filings, so the scrape is per entity and the result belongs on all of them.
    """
    import json

    with open(path) as handle:
        results = json.load(handle)

    conn = dbmod.open_db(db_path)
    updated = rows = skipped = unmatched = 0
    try:
        for result in results:
            rn = result.get("regulated_entity")
            if not rn or result.get("error"):
                skipped += 1
                continue
            payload = unit_payload(result)
            if not payload:
                skipped += 1
                continue
            assignments = ", ".join(f"{name} = ?" for name in payload)
            cursor = conn.execute(
                f"UPDATE entities SET {assignments} "
                f"WHERE source = 'tceq_air' AND regulated_entity = ?",
                list(payload.values()) + [rn],
            )
            if cursor.rowcount:
                updated += 1
                rows += cursor.rowcount
            else:
                unmatched += 1
        conn.commit()
    finally:
        conn.close()

    if verbose:
        print(f"{updated} entities merged onto {rows} permit records "
              f"({skipped} carried nothing, {unmatched} matched no record)")
    return {"entities": updated, "rows": rows,
            "skipped": skipped, "unmatched": unmatched}
