"""
ercot.py — ERCOT interconnection queues.

Two datasets:

  GIS report (monthly)
      Every generation interconnection request in ERCOT, with fuel, technology,
      nameplate MW, county, point of interconnection and study milestones. Sheets
      are split into large gen (>= 20 MW) and small gen.

  Large Load interconnection status (monthly)
      Where data centers, crypto miners and industrial electrification show up,
      with requested MW, county, and whether the load is colocated behind a
      generator's meter.

Both are published as Excel workbooks on the ERCOT MIS. The MIS exposes a JSON
document listing per report type:

    https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=<id>
    https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=<docId>

`fetch_latest` uses that API. Report type ids are configurable because ERCOT does
reorganize its report catalog — see REPORT_TYPE_IDS below. If the id is wrong or
network access is blocked, download the workbook by hand and use `--file`; that
is the primary supported path and needs no network at all.
"""

import io
import json
import os
import re
import urllib.request
import zipfile

import pandas as pd

from . import base
from ..normalize import clean_str, norm_text, parse_date

SOURCE_GIS = "ercot_gis"
SOURCE_LARGE_LOAD = "ercot_large_load"

MIS_DOC_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={}"
MIS_DOWNLOAD = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={}"

# ERCOT MIS report type ids. 15933 is the monthly GIS report. The large-load
# report id varies by ERCOT catalog revision, so it is left unset by default and
# read from the ERCOT_LARGE_LOAD_REPORT_TYPE_ID environment variable — set it once
# you have confirmed the id from the MIS report listing, or just use --file.
REPORT_TYPE_IDS = {
    SOURCE_GIS: int(os.environ.get("ERCOT_GIS_REPORT_TYPE_ID", "15933")),
    SOURCE_LARGE_LOAD: (
        int(os.environ["ERCOT_LARGE_LOAD_REPORT_TYPE_ID"])
        if os.environ.get("ERCOT_LARGE_LOAD_REPORT_TYPE_ID")
        else None
    ),
}

USER_AGENT = "viirs-nightfire-tool/permits (+https://github.com/tim03bo-ux/viirs-nightfire-tool)"

# --- GIS ---------------------------------------------------------------------

GIS_SHEETS = [
    "Project Details - Large Gen",
    "Project Details - Small Gen",
]

# Verified against the July 2026 workbook: Large Gen headers sit on row 30,
# Small Gen on row 14, under a title block and up to nine notes paragraphs.
GIS_HEADER_TOKENS = ["INR", "Project Name", "County", "Fuel", "Technology",
                     "Capacity", "Interconnecting Entity"]

GIS_COLUMNS = {
    "inr": ["INR", "Interconnection Request Number", "Queue Number"],
    "project_name": ["Project Name", "Generation Project Name", "Project"],
    "operator": ["Interconnecting Entity", "Interconnection Entity", "Developer",
                 "Company Name", "Entity"],
    "poi": ["POI Location", "Point of Interconnection", "POI"],
    "county": ["County"],
    "fuel": ["Fuel"],
    "technology": ["Technology"],
    "capacity_mw": ["Capacity (MW)", "Capacity MW", "Summer Capacity (MW)",
                    "Nameplate", "Capacity"],
    "projected_cod": ["Projected COD", "Approved for Energization",
                      "Commercial Operations Date", "COD"],
    "study_phase": ["GIM Study Phase", "Study Phase"],
    "screening_complete": ["Screening Study Complete", "Screening Study Started"],
    "ia_signed": ["IA Signed", "Interconnection Agreement"],
    "security_posted": ["Financial Security Posted", "Security Posted"],
    "zone": ["CDR Reporting Zone", "Zone"],
    "status": ["Project Status", "Status"],
    # Present in the real report and genuinely useful: ERCOT states each
    # generation project's air-permit position and its construction window.
    "air_permit": ["Air Permit"],
    "ghg_permit": ["GHG Permit"],
    "water_availability": ["Water Availability"],
    "construction_start": ["Construction Start"],
    "construction_end": ["Construction End"],
    "energization": ["Approved for Energization"],
    "synchronization": ["Approved for Synchronization"],
    "comment": ["Comment"],
}


def read_gis(path, sheet=None):
    """Read GIS project sheets into one DataFrame with a `_sheet` column."""
    sheets = [sheet] if sheet else None
    if sheets is None:
        available = []
        try:
            book = pd.ExcelFile(path)
            names = book.sheet_names
        except (ValueError, OSError):
            names = []
        for wanted in GIS_SHEETS:
            match = base.pick_sheet(path, [wanted])
            if match:
                available.append(match)
        if not available:
            # Fall back to any sheet whose name mentions project details.
            available = [name for name in names
                         if "project" in norm_text(name)] or [0]
        sheets = available

    frames = []
    for name in sheets:
        try:
            df = base.read_table(path, sheet=name, expected_tokens=GIS_HEADER_TOKENS)
        except (ValueError, KeyError):
            continue
        if df.empty:
            continue
        df["_sheet"] = str(name)
        frames.append(df)

    if not frames:
        raise ValueError(f"no readable GIS project sheets in {path}")
    return pd.concat(frames, ignore_index=True)


def gis_to_entities(df, source_file_id=None):
    """Normalize GIS rows into entity dicts."""
    resolved = base.resolve_columns(df, GIS_COLUMNS, required=["project_name"])
    records = []
    for _, row in df.iterrows():
        project_name = clean_str(base.get(row, resolved, "project_name"))
        inr = clean_str(base.get(row, resolved, "inr"))
        # The real sheets carry blank spacer rows under the header.
        if not inr:
            continue

        # INR is ERCOT's stable key; fall back to name+county when a sheet omits it.
        county = clean_str(base.get(row, resolved, "county"))
        source_key = inr or f"{project_name}|{county or ''}"

        # GIM Study Phase is a compound string in the real report, e.g.
        # "SS Completed, FIS Started, No IA" — it already encodes the milestones,
        # so it is kept verbatim as the displayed status.
        phase = clean_str(base.get(row, resolved, "study_phase"))
        status = clean_str(base.get(row, resolved, "status")) or phase or "In queue"

        ia_signed = base.get(row, resolved, "ia_signed")
        energization = base.get(row, resolved, "energization")
        # An executed interconnection agreement, or approval to energize, is the
        # queue's decision point. Everything short of that is still an
        # application, whatever intermediate milestones have been met.
        decision_date = parse_date(energization) or parse_date(ia_signed)
        lifecycle_override = "approved" if decision_date else "pending"

        records.append(
            base.build_entity(
                source=SOURCE_GIS,
                source_key=source_key,
                raw_row=row.to_dict(),
                project_name=project_name,
                operator=base.get(row, resolved, "operator"),
                county=county,
                address=base.get(row, resolved, "poi"),
                fuel_code=base.get(row, resolved, "fuel"),
                technology=base.get(row, resolved, "technology"),
                capacity_mw=base.get(row, resolved, "capacity_mw"),
                projected_cod=base.get(row, resolved, "projected_cod"),
                status=status,
                decision_date=decision_date,
                lifecycle_override=lifecycle_override,
                air_permit=base.get(row, resolved, "air_permit"),
                construction_start=base.get(row, resolved, "construction_start"),
                construction_end=base.get(row, resolved, "construction_end"),
                permit_type="ERCOT interconnection request",
                permit_number=inr,
                source_file_id=source_file_id,
                kind_override="generation",
            )
        )
    return records


# --- Large loads -------------------------------------------------------------

LARGE_LOAD_SHEETS = [
    "Large Load", "Load Interconnection", "Project Details", "Large Loads",
]

LARGE_LOAD_HEADER_TOKENS = ["County", "MW", "Load", "Status"]

LARGE_LOAD_COLUMNS = {
    "request_id": ["Request Number", "Load Interconnection Request", "LIR",
                   "Queue Number", "Project Number", "ID"],
    "project_name": ["Project Name", "Load Name", "Facility Name", "Project"],
    "operator": ["Interconnecting Entity", "Customer", "Company", "Entity",
                 "Load Serving Entity", "Developer"],
    "county": ["County"],
    "load_mw": ["Requested Capacity (MW)", "Load (MW)", "Capacity (MW)",
                "Maximum Load", "MW", "Requested MW"],
    "load_type": ["Load Type", "Type of Load", "Sector", "Industry",
                  "Description", "Load Category"],
    "status": ["Status", "Study Phase", "Interconnection Status"],
    "energization": ["Projected Energization", "Requested Energization Date",
                     "Energization Date", "In-Service Date", "COD"],
    "colocated": ["Co-located", "Colocated", "Behind the Meter", "BTM",
                  "Co-location"],
    "poi": ["POI", "Point of Interconnection", "Substation", "Transmission Owner"],
    "zone": ["Weather Zone", "Load Zone", "Zone"],
}

# Values in a "co-located" style column that mean yes.
_TRUTHY = {"y", "yes", "true", "1", "co-located", "colocated", "btm",
           "behind the meter", "x"}


def read_large_load(path, sheet=None):
    """Read the large-load workbook into one DataFrame."""
    name = sheet or base.pick_sheet(path, LARGE_LOAD_SHEETS)
    df = base.read_table(path, sheet=name, expected_tokens=LARGE_LOAD_HEADER_TOKENS)
    if df.empty:
        raise ValueError(f"no rows read from {path}")
    df["_sheet"] = str(name or 0)
    return df


def large_load_to_entities(df, source_file_id=None):
    """Normalize large-load rows into entity dicts."""
    resolved = base.resolve_columns(df, LARGE_LOAD_COLUMNS)
    if not resolved:
        raise KeyError(
            f"no recognizable large-load columns; headers: {list(df.columns)[:40]}"
        )

    records = []
    for index, row in df.iterrows():
        project_name = clean_str(base.get(row, resolved, "project_name"))
        request_id = clean_str(base.get(row, resolved, "request_id"))
        operator = clean_str(base.get(row, resolved, "operator"))
        county = clean_str(base.get(row, resolved, "county"))
        if not any((project_name, request_id, operator)):
            continue

        source_key = request_id or f"{project_name or operator}|{county or ''}|{index}"
        load_type = clean_str(base.get(row, resolved, "load_type"))
        colocated_raw = clean_str(base.get(row, resolved, "colocated"))
        colocated = bool(colocated_raw and norm_text(colocated_raw) in _TRUTHY)

        description = " ".join(
            part for part in (load_type, colocated_raw and f"colocated {colocated_raw}")
            if part
        )

        record = base.build_entity(
            source=SOURCE_LARGE_LOAD,
            source_key=source_key,
            raw_row=row.to_dict(),
            project_name=project_name or operator,
            operator=operator,
            county=county,
            address=base.get(row, resolved, "poi"),
            description=description,
            load_mw=base.get(row, resolved, "load_mw"),
            status=base.get(row, resolved, "status"),
            projected_cod=base.get(row, resolved, "energization"),
            permit_type="ERCOT large load interconnection request",
            permit_number=request_id,
            source_file_id=source_file_id,
        )
        if colocated:
            # ERCOT flagging the load as behind-the-meter is direct evidence of
            # colocation, independent of anything the geospatial linker finds.
            record["kind_evidence"] = (
                (record["kind_evidence"] or "") + "; ERCOT flags load as co-located/BTM"
            ).strip("; ")
        records.append(record)
    return records


# --- MIS client --------------------------------------------------------------

def _http_get(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def list_documents(report_type_id, timeout=60):
    """Return the MIS document list for a report type, newest first.

    Each item is a dict with at least docId, name and publishDate.
    """
    payload = _http_get(MIS_DOC_LIST.format(report_type_id), timeout=timeout)
    data = json.loads(payload.decode("utf-8", errors="replace"))
    documents = []
    for item in data:
        doc = item.get("Document", item) if isinstance(item, dict) else {}
        doc_id = doc.get("DocID") or doc.get("docId")
        if not doc_id:
            continue
        documents.append(
            {
                "doc_id": str(doc_id),
                "name": doc.get("FriendlyName") or doc.get("ConstructedName") or "",
                "publish_date": doc.get("PublishDate") or "",
                "extension": (doc.get("Extension") or "").lower(),
            }
        )
    documents.sort(key=lambda d: d["publish_date"], reverse=True)
    return documents


def download_document(doc_id, dest_dir, timeout=180):
    """Download one MIS document, unzipping single-file archives. Returns a path."""
    os.makedirs(dest_dir, exist_ok=True)
    payload = _http_get(MIS_DOWNLOAD.format(doc_id), timeout=timeout)

    if payload[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            if not names:
                raise ValueError(f"empty zip for doc {doc_id}")
            inner = names[0]
            out_path = os.path.join(dest_dir, os.path.basename(inner))
            with archive.open(inner) as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            return out_path

    out_path = os.path.join(dest_dir, f"ercot_{doc_id}.xlsx")
    with open(out_path, "wb") as handle:
        handle.write(payload)
    return out_path


def fetch_latest(source, dest_dir, name_filter=None, timeout=180):
    """Download the newest workbook for `source` from the MIS. Returns a path.

    Raises RuntimeError when the report type id for the source is unconfigured.
    """
    report_type_id = REPORT_TYPE_IDS.get(source)
    if not report_type_id:
        raise RuntimeError(
            f"no MIS report type id configured for {source}. Set the "
            f"{source.upper()}_REPORT_TYPE_ID environment variable, or download "
            "the workbook manually and ingest it with --file."
        )
    documents = list_documents(report_type_id, timeout=timeout)
    if name_filter:
        pattern = re.compile(name_filter, re.IGNORECASE)
        documents = [d for d in documents if pattern.search(d["name"])]
    documents = [d for d in documents if d["extension"] in ("", "xlsx", "xls", "zip")]
    if not documents:
        raise RuntimeError(f"no documents found for report type {report_type_id}")
    return download_document(documents[0]["doc_id"], dest_dir, timeout=timeout)
