"""
tceq_stormwater.py — TCEQ stormwater construction general permit NOIs (TXR150000).

Any construction project disturbing one acre or more has to file a Notice of
Intent under TXR150000 before earthwork starts. That makes the NOI the earliest
public signal a large site exists — typically 12 to 24 months ahead of an ERCOT
energization date and often before an air permit application is filed. The NOI
carries the operator, a site address with coordinates, disturbed acreage and the
projected start/end dates.

Source: TCEQ water quality general permit search,
    https://www2.tceq.texas.gov/wq_dpa/index.cfm
Export the results grid to csv/xlsx (or save the HTML page) and ingest with
`--file`. `search_url` builds the query URL for a county if you want to automate
the export step.

Acreage alone does not say what is being built, so classification here leans on
the operator name and any project description; NOIs with no such signal are
tagged `construction` and surface in the dashboard as leads rather than as
confirmed data centers.
"""

import urllib.parse

import pandas as pd

from . import base
from ..normalize import clean_str

SOURCE = "tceq_swnoi"

SEARCH_BASE = "https://www2.tceq.texas.gov/wq_dpa/index.cfm"

HEADER_TOKENS = ["Permit", "Operator", "County", "Acres", "Site", "NOI"]

COLUMNS = {
    "permit_number": ["Permit Number", "Authorization Number", "NOI Number",
                      "TXR Number", "Permit No", "Permit"],
    "project_name": ["Site Name", "Project Name", "Project Site Name",
                     "Regulated Entity Name", "Facility Name", "Site"],
    "operator": ["Operator Name", "Operator", "Customer Name", "Company Name",
                 "Applicant", "Owner"],
    "regulated_entity": ["RN Number", "Regulated Entity Number", "RN"],
    "customer_number": ["CN Number", "Customer Number", "CN"],
    "county": ["County"],
    "address": ["Site Address", "Physical Location", "Location Description",
                "Street Address", "Address"],
    "city": ["City", "Nearest City"],
    "latitude": ["Latitude", "Site Latitude", "Lat"],
    "longitude": ["Longitude", "Site Longitude", "Long", "Lon"],
    "acres": ["Acres Disturbed", "Disturbed Acres", "Total Acres",
              "Project Acres", "Acreage", "Acres"],
    "status": ["Status", "Permit Status", "Authorization Status"],
    "issued_date": ["Issued Date", "Effective Date", "Date Issued",
                    "Authorization Date"],
    "start_date": ["Projected Start Date", "Construction Start", "Start Date"],
    "end_date": ["Projected End Date", "Construction End", "End Date",
                 "Termination Date"],
    "description": ["Project Description", "Description", "Nature of Activity",
                    "Type of Construction", "Comments"],
    "url": ["URL", "Link"],
}


def search_url(county=None, permit_type="TXR150000", operator=None):
    """Build a TCEQ water-quality general-permit search URL.

    Provided so the export step can be scripted; the response is an HTML results
    grid, which `read_file` can parse once saved.
    """
    params = {"fuseaction": "home.gp_search", "gp_type": permit_type}
    if county:
        params["county"] = county
    if operator:
        params["operator"] = operator
    return f"{SEARCH_BASE}?{urllib.parse.urlencode(params)}"


def read_file(path, sheet=None):
    """Read an NOI export (csv / xlsx / saved HTML results table)."""
    lower = str(path).lower()
    if lower.endswith((".html", ".htm")):
        tables = pd.read_html(path)
        if not tables:
            raise ValueError(f"no HTML tables found in {path}")
        df = max(tables, key=lambda t: (t.shape[1], t.shape[0]))
        df.columns = [clean_str(c) or f"col_{i}" for i, c in enumerate(df.columns)]
        return df.dropna(axis=0, how="all")
    return base.read_table(path, sheet=sheet, expected_tokens=HEADER_TOKENS)


def to_entities(df, source_file_id=None, min_acres=None):
    """Normalize NOI rows into entity dicts.

    min_acres filters out small sites; a hyperscale campus or a utility-scale
    generation site disturbs tens to hundreds of acres, so raising this is the
    cheapest way to cut noise from subdivisions and road work.
    """
    resolved = base.resolve_columns(df, COLUMNS)
    if not resolved:
        raise KeyError(
            f"no recognizable TCEQ stormwater columns; headers: {list(df.columns)[:40]}"
        )

    records = []
    for index, row in df.iterrows():
        permit_number = clean_str(base.get(row, resolved, "permit_number"))
        project_name = clean_str(base.get(row, resolved, "project_name"))
        operator = clean_str(base.get(row, resolved, "operator"))
        if not any((permit_number, project_name, operator)):
            continue

        source_key = permit_number or f"{project_name or operator}|{index}"

        address_parts = [
            clean_str(base.get(row, resolved, "address")),
            clean_str(base.get(row, resolved, "city")),
        ]
        address = ", ".join(part for part in address_parts if part) or None

        record = base.build_entity(
            source=SOURCE,
            source_key=source_key,
            raw_row=row.to_dict(),
            project_name=project_name or operator,
            operator=operator,
            county=base.get(row, resolved, "county"),
            address=address,
            latitude=base.get(row, resolved, "latitude"),
            longitude=base.get(row, resolved, "longitude"),
            description=base.get(row, resolved, "description"),
            acres=base.get(row, resolved, "acres"),
            status=base.get(row, resolved, "status"),
            decision_date=base.get(row, resolved, "issued_date"),
            received_date=base.get(row, resolved, "start_date"),
            projected_cod=base.get(row, resolved, "end_date"),
            permit_type="TCEQ stormwater construction NOI (TXR150000)",
            permit_number=permit_number,
            regulated_entity=base.get(row, resolved, "regulated_entity"),
            customer_number=base.get(row, resolved, "customer_number"),
            url=base.get(row, resolved, "url"),
            source_file_id=source_file_id,
        )

        if min_acres is not None:
            acres = record.get("acres")
            if acres is None or acres < min_acres:
                continue

        records.append(record)
    return records
