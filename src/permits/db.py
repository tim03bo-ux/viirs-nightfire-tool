"""
db.py — SQLite store for permit / interconnection records and resolved sites.

Design notes:

* One row per *source record* in `entities`, keyed by (source, source_key) so a
  re-ingest of the same monthly file updates in place instead of duplicating.
  The untouched source row is kept in `raw_json` — every derived column can be
  recomputed without re-downloading anything.

* `sites` holds resolved clusters, and is fully derived from `entities` +
  `entity_links`. It is dropped and rebuilt by the linker, so it is safe to
  re-run linking after tuning thresholds.

* `first_seen` / `last_seen` track when a record entered and was last observed
  in a source file, which is how a project that vanishes from the ERCOT queue
  (withdrawn, or energized and moved off) stays visible in history.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd

SCHEMA_VERSION = 3

SOURCES = {
    "ercot_gis": "ERCOT Generator Interconnection Status (GIS) report",
    "ercot_large_load": "ERCOT Large Load interconnection status report",
    "tceq_air": "TCEQ air New Source Review permit applications",
    "tceq_swnoi": "TCEQ stormwater construction NOI (TXR150000)",
    "puct": "PUCT Interchange dockets (CCN, transmission, large load)",
}

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
);

-- Provenance: one row per ingested file or API pull.
CREATE TABLE IF NOT EXISTS source_files (
    file_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    uri         TEXT,
    sha256      TEXT,
    fetched_at  TEXT NOT NULL,
    n_rows      INTEGER,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS ix_source_files_source ON source_files(source);

-- One normalized record per source row. This is the unit of matching.
CREATE TABLE IF NOT EXISTS entities (
    entity_id       TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    project_name    TEXT,
    name_norm       TEXT,
    operator        TEXT,
    operator_norm   TEXT,
    county          TEXT,
    county_norm     TEXT,
    state           TEXT DEFAULT 'TX',
    address         TEXT,
    latitude        REAL,
    longitude       REAL,
    geo_precision   TEXT,          -- site | zip | county | none
    zip_code        TEXT,
    primary_business TEXT,   -- TCEQ Central Registry classification
    project_kind    TEXT,
    kind_confidence REAL,
    kind_evidence   TEXT,
    fuel            TEXT,
    technology      TEXT,
    fuel_confidence REAL,
    capacity_mw     REAL,          -- generation nameplate
    load_mw         REAL,          -- interconnecting load
    acres           REAL,
    status          TEXT,          -- verbatim agency text
    status_date     TEXT,
    lifecycle       TEXT,          -- pending | approved | operating | withdrawn | denied | expired | unknown
    stage           TEXT,          -- finer step within the lifecycle
    received_date   TEXT,          -- application filed
    decision_date   TEXT,          -- issued / final action
    projected_cod   TEXT,
    permit_type     TEXT,
    permit_program  TEXT,          -- nsr | psd | pbr | standard_permit | title_v | ...
    permit_action   TEXT,          -- new | amendment | alteration | renewal | ...
    permit_number   TEXT,
    regulated_entity TEXT,         -- TCEQ RN number
    customer_number TEXT,          -- TCEQ CN number
    -- Permitted (allowable) emissions in tons per year, where the export
    -- carries them. These are authorized rates, not measured emissions.
    -- ERCOT states these for generation projects; TCEQ never does.
    air_permit_status  TEXT,       -- obtained | not_required | verbatim text
    air_permit_date    TEXT,
    construction_start TEXT,
    construction_end   TEXT,
    nox_tpy         REAL,
    co_tpy          REAL,
    voc_tpy         REAL,
    pm_tpy          REAL,
    so2_tpy         REAL,
    ghg_tpy         REAL,
    url             TEXT,
    first_seen      TEXT,
    last_seen       TEXT,
    source_file_id  INTEGER REFERENCES source_files(file_id),
    raw_json        TEXT,
    UNIQUE(source, source_key)
);
CREATE INDEX IF NOT EXISTS ix_entities_source    ON entities(source);
CREATE INDEX IF NOT EXISTS ix_entities_kind      ON entities(project_kind);
CREATE INDEX IF NOT EXISTS ix_entities_county    ON entities(county_norm);
CREATE INDEX IF NOT EXISTS ix_entities_operator  ON entities(operator_norm);
CREATE INDEX IF NOT EXISTS ix_entities_lifecycle ON entities(lifecycle);
CREATE INDEX IF NOT EXISTS ix_entities_program   ON entities(permit_program);

-- Pairwise evidence that two records describe the same development.
CREATE TABLE IF NOT EXISTS entity_links (
    entity_a     TEXT NOT NULL REFERENCES entities(entity_id),
    entity_b     TEXT NOT NULL REFERENCES entities(entity_id),
    score        REAL NOT NULL,
    method       TEXT,
    distance_km  REAL,
    name_score   REAL,
    operator_score REAL,
    PRIMARY KEY (entity_a, entity_b)
);
CREATE INDEX IF NOT EXISTS ix_links_a ON entity_links(entity_a);
CREATE INDEX IF NOT EXISTS ix_links_b ON entity_links(entity_b);

-- Resolved clusters. Rebuilt wholesale by link.rebuild_sites().
CREATE TABLE IF NOT EXISTS sites (
    site_id        TEXT PRIMARY KEY,
    site_name      TEXT,
    operator       TEXT,
    county         TEXT,
    latitude       REAL,
    longitude      REAL,
    geo_precision  TEXT,
    site_class     TEXT,        -- generation_only | load_only | colocated_gen_load | construction_only | mixed
    has_generation INTEGER,
    has_load       INTEGER,
    gen_mw         REAL,
    load_mw        REAL,
    -- Split by authorization state, so "what is actually permitted" can be read
    -- apart from "what has been applied for".
    gen_mw_approved  REAL,
    gen_mw_pending   REAL,
    load_mw_approved REAL,
    load_mw_pending  REAL,
    lifecycle      TEXT,        -- most advanced lifecycle among the members
    has_pending    INTEGER,     -- any member application still in process
    fuels          TEXT,        -- comma-separated canonical fuels
    technologies   TEXT,
    programs       TEXT,        -- comma-separated authorization programs present
    dispatchable_mw REAL,
    nox_tpy        REAL,
    n_members      INTEGER,
    sources        TEXT,        -- comma-separated source ids present
    earliest_date  TEXT,
    latest_date    TEXT,
    confidence     REAL,        -- min pairwise link score holding the cluster together
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_sites_class  ON sites(site_class);
CREATE INDEX IF NOT EXISTS ix_sites_county ON sites(county);

CREATE TABLE IF NOT EXISTS site_members (
    site_id    TEXT NOT NULL REFERENCES sites(site_id),
    entity_id  TEXT NOT NULL REFERENCES entities(entity_id),
    PRIMARY KEY (site_id, entity_id)
);
CREATE INDEX IF NOT EXISTS ix_site_members_entity ON site_members(entity_id);
"""

# Columns written by ingest. Kept explicit so a source adapter that invents a
# field gets a clear error instead of silently dropping it.
ENTITY_COLUMNS = [
    "entity_id", "source", "source_key", "project_name", "name_norm", "operator",
    "operator_norm", "county", "county_norm", "state", "address", "latitude",
    "longitude", "geo_precision", "zip_code", "primary_business", "project_kind", "kind_confidence", "kind_evidence",
    "fuel", "technology", "fuel_confidence", "capacity_mw", "load_mw", "acres",
    "status", "status_date", "lifecycle", "stage", "received_date", "decision_date",
    "projected_cod", "permit_type", "permit_program", "permit_action",
    "permit_number", "regulated_entity", "customer_number",
    "air_permit_status", "air_permit_date", "construction_start", "construction_end",
    "nox_tpy", "co_tpy", "voc_tpy", "pm_tpy", "so2_tpy", "ghg_tpy",
    "url", "first_seen", "last_seen", "source_file_id", "raw_json",
]

# Columns added after the initial release, with their SQL declarations. Existing
# databases are upgraded in place by `migrate`, so a schema change never means
# re-downloading and re-ingesting everything.
_ADDED_COLUMNS = {
    "entities": {
        "lifecycle": "TEXT", "stage": "TEXT", "received_date": "TEXT",
        "decision_date": "TEXT", "permit_program": "TEXT", "permit_action": "TEXT",
        "nox_tpy": "REAL", "co_tpy": "REAL", "voc_tpy": "REAL", "pm_tpy": "REAL",
        "so2_tpy": "REAL", "ghg_tpy": "REAL",
        "zip_code": "TEXT", "primary_business": "TEXT",
        "air_permit_status": "TEXT", "air_permit_date": "TEXT",
        "construction_start": "TEXT", "construction_end": "TEXT",
    },
    "sites": {
        "gen_mw_approved": "REAL", "gen_mw_pending": "REAL",
        "load_mw_approved": "REAL", "load_mw_pending": "REAL",
        "lifecycle": "TEXT", "has_pending": "INTEGER", "programs": "TEXT",
        "nox_tpy": "REAL",
    },
}


def utcnow():
    """ISO-8601 UTC timestamp, second resolution."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_entity_id(source, source_key):
    """Deterministic id so re-ingesting the same record maps to the same row."""
    digest = hashlib.sha1(f"{source}|{source_key}".encode("utf-8")).hexdigest()
    return f"{source}:{digest[:16]}"


def connect(db_path):
    """Open (creating parent dirs as needed) and return a configured connection."""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn):
    """Add columns introduced after a database was first created.

    SQLite cannot add a column that already exists, and CREATE TABLE IF NOT
    EXISTS silently leaves an older table alone — so the schema text above only
    helps new databases. Existing ones are brought forward here. Returns the
    list of "table.column" additions made.
    """
    added = []
    for table, columns in _ADDED_COLUMNS.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
        }
        if not existing:
            continue  # table itself is new; the schema script just created it
        for column, declaration in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
                added.append(f"{table}.{column}")
    if added:
        conn.commit()
    return added


def init_db(conn):
    """Create the schema if absent, migrate an older one, and stamp the version."""
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def open_db(db_path):
    """connect() + init_db() in one call."""
    return init_db(connect(db_path))


def record_source_file(conn, source, uri, sha256=None, n_rows=None, notes=None):
    """Log a file/API pull and return its file_id."""
    cur = conn.execute(
        "INSERT INTO source_files(source, uri, sha256, fetched_at, n_rows, notes) "
        "VALUES(?, ?, ?, ?, ?, ?)",
        (source, uri, sha256, utcnow(), n_rows, notes),
    )
    conn.commit()
    return cur.lastrowid


def file_sha256(path):
    """Hash a file so an unchanged monthly report can be skipped."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def already_ingested(conn, source, sha256):
    """True if a file with this hash was already ingested for this source."""
    row = conn.execute(
        "SELECT 1 FROM source_files WHERE source = ? AND sha256 = ? LIMIT 1",
        (source, sha256),
    ).fetchone()
    return row is not None


def upsert_entities(conn, records, observed_at=None):
    """Insert or update normalized records.

    `records` is a list of dicts using ENTITY_COLUMNS keys (entity_id, name_norm
    and the *_norm fields are filled by the caller via sources.base.build_entity).

    On conflict the observed fields are refreshed and `last_seen` advances, but
    `first_seen` is preserved — that difference is what makes queue history
    readable later.

    Returns (n_inserted, n_updated).
    """
    if not records:
        return (0, 0)

    observed_at = observed_at or utcnow()
    existing = {
        row["entity_id"]
        for row in conn.execute("SELECT entity_id FROM entities")
    }

    inserted = updated = 0
    rows = []
    for record in records:
        unknown = set(record) - set(ENTITY_COLUMNS)
        if unknown:
            raise ValueError(
                f"unknown entity columns {sorted(unknown)} for "
                f"{record.get('source')}:{record.get('source_key')}"
            )
        row = {column: record.get(column) for column in ENTITY_COLUMNS}
        row["first_seen"] = row.get("first_seen") or observed_at
        row["last_seen"] = observed_at
        if row["entity_id"] in existing:
            updated += 1
        else:
            inserted += 1
        rows.append(row)

    placeholders = ", ".join("?" for _ in ENTITY_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in ENTITY_COLUMNS
        if column not in ("entity_id", "source", "source_key", "first_seen")
    )
    sql = (
        f"INSERT INTO entities ({', '.join(ENTITY_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(entity_id) DO UPDATE SET {updates}"
    )
    conn.executemany(sql, [[row[column] for column in ENTITY_COLUMNS] for row in rows])
    conn.commit()
    return (inserted, updated)


def load_entities(conn, sources=None, kinds=None):
    """Read entities into a DataFrame, optionally filtered."""
    sql = "SELECT * FROM entities"
    clauses, params = [], []
    if sources:
        clauses.append(f"source IN ({', '.join('?' for _ in sources)})")
        params.extend(sources)
    if kinds:
        clauses.append(f"project_kind IN ({', '.join('?' for _ in kinds)})")
        params.extend(kinds)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return pd.read_sql_query(sql, conn, params=params)


def load_sites(conn):
    """Read resolved sites into a DataFrame."""
    return pd.read_sql_query("SELECT * FROM sites", conn)


def load_site_detail(conn, site_id):
    """All member entities of one site, in a stable display order."""
    return pd.read_sql_query(
        "SELECT e.* FROM entities e "
        "JOIN site_members m ON m.entity_id = e.entity_id "
        "WHERE m.site_id = ? "
        "ORDER BY e.source, e.project_name",
        conn,
        params=[site_id],
    )


def stats(conn):
    """Counts used by the CLI `stats` command and the dashboard header."""
    out = {}
    out["entities"] = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    out["sites"] = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    out["links"] = conn.execute("SELECT COUNT(*) FROM entity_links").fetchone()[0]
    out["by_source"] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT source, COUNT(*) FROM entities GROUP BY source ORDER BY 2 DESC"
        )
    }
    out["by_kind"] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT project_kind, COUNT(*) FROM entities GROUP BY project_kind "
            "ORDER BY 2 DESC"
        )
    }
    out["by_site_class"] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT site_class, COUNT(*) FROM sites GROUP BY site_class ORDER BY 2 DESC"
        )
    }
    out["by_lifecycle"] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT COALESCE(lifecycle, 'unknown'), COUNT(*) FROM entities "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
    }
    out["by_program"] = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT COALESCE(permit_program, 'unclassified'), COUNT(*) FROM entities "
            "GROUP BY 1 ORDER BY 2 DESC"
        )
    }
    row = conn.execute(
        "SELECT MAX(fetched_at) FROM source_files"
    ).fetchone()
    out["last_ingest"] = row[0] if row else None
    # True when any ingested file was the synthetic demo set, so the dashboard
    # can say so even if real exports were later loaded alongside it.
    out["is_demo"] = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM source_files WHERE notes LIKE '%SYNTHETIC%')"
    ).fetchone()[0] == 1
    return out


def mark_demo_files(conn, uri_prefix, banner):
    """Tag ingested files under `uri_prefix` as synthetic. Returns rows updated."""
    cur = conn.execute(
        "UPDATE source_files SET notes = ? || ' | ' || COALESCE(notes, '') "
        "WHERE uri LIKE ?",
        (banner, f"{uri_prefix}%"),
    )
    conn.commit()
    return cur.rowcount


def to_json(value):
    """Serialize a source row for the raw_json column, tolerating numpy/NaT."""
    def default(obj):
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        if hasattr(obj, "item"):
            try:
                return obj.item()
            except (ValueError, AttributeError):
                return str(obj)
        return str(obj)

    cleaned = {}
    for key, val in dict(value).items():
        if val is None:
            continue
        try:
            # pd.isna returns an array for list-likes; those are always kept.
            if pd.isna(val):
                continue
        except (TypeError, ValueError):
            pass
        cleaned[str(key)] = val
    return json.dumps(cleaned, default=default, ensure_ascii=False)
