"""
tceq_air.py — TCEQ air New Source Review (NSR) permit applications and permits.

Air permits are the strongest evidence that a project burns something: a data
center campus with a fleet of backup diesel gensets, a behind-the-meter gas
peaker, or a full combined-cycle plant all have to be authorized here, and the
application states the units, fuel and ratings.

Useful TCEQ products, all of which this adapter can read once exported to
csv/xlsx:

  * Air NSR permit applications pending / recently issued
      https://www.tceq.texas.gov/permitting/air/nav/air_pendingpermits.html
  * Central Registry regulated-entity search (RN / CN numbers, coordinates,
      NAICS/SIC)                       https://www15.tceq.texas.gov/crpub/
  * Point Source Emissions Inventory (operating facilities, actual emissions)

Because TCEQ's export layouts differ per query form, the column map below is
deliberately generous, and `read_file` accepts csv, xlsx or a saved HTML results
table.
"""

import re
import urllib.parse
import urllib.request

import pandas as pd

from . import base
from ..normalize import clean_str, parse_number

SOURCE = "tceq_air"

USER_AGENT = "viirs-nightfire-tool/permits (+https://github.com/tim03bo-ux/viirs-nightfire-tool)"

CENTRAL_REGISTRY_BASE = "https://www15.tceq.texas.gov/crpub/index.cfm"

HEADER_TOKENS = ["Permit", "County", "Company", "Regulated Entity", "RN", "Status"]

COLUMNS = {
    "permit_number": ["Permit Number", "Permit No", "Permit #", "Air Permit Number",
                      "Permit"],
    "project_number": ["Project Number", "Project No", "Application Number",
                       "Tracking Number"],
    "project_name": ["Project Name", "Regulated Entity Name", "RE Name",
                     "Site Name", "Facility Name", "Plant Name"],
    "operator": ["Customer Name", "Company Name", "Applicant", "Owner",
                 "Permit Holder", "Customer"],
    "regulated_entity": ["RN Number", "Regulated Entity Number", "RN"],
    "customer_number": ["CN Number", "Customer Number", "CN"],
    "county": ["County"],
    "address": ["Physical Location", "Site Location", "Location Description",
                "Physical Address", "Street Address", "Address"],
    "city": ["City", "Nearest City"],
    "latitude": ["Latitude", "Lat", "Site Latitude"],
    "longitude": ["Longitude", "Long", "Lon", "Site Longitude"],
    "permit_type": ["Permit Type", "Authorization Type", "Application Type",
                    "Permit Action", "Type"],
    "status": ["Application Status", "Permit Status", "Status"],
    "received_date": ["Received Date", "Date Received", "Application Received",
                      "Filed Date"],
    "issued_date": ["Issued Date", "Date Issued", "Final Action Date",
                    "Effective Date"],
    "description": ["Project Description", "Description", "Project Type",
                    "Facility Description", "Process Description", "Comments"],
    "naics": ["NAICS", "NAICS Code", "Primary NAICS"],
    "sic": ["SIC", "SIC Code", "Primary SIC"],
    "capacity_mw": ["Capacity (MW)", "Rated Capacity MW", "MW", "Megawatts"],
    "nox_tpy": ["NOx", "NOX TPY", "Nitrogen Oxides"],
    "url": ["URL", "Link", "Document Link"],
}

# Pulls "250 MW" / "2 x 45 MW" style ratings out of free-text descriptions when
# there is no dedicated capacity column.
_MW_TEXT_RE = re.compile(
    r"(?:(\d+)\s*[x×]\s*)?(\d[\d,]*\.?\d*)\s*(?:mw|megawatt)", re.IGNORECASE
)


def capacity_from_text(text):
    """Total MW implied by a description, or None. '2 x 45 MW' -> 90.0."""
    if not text:
        return None
    total = 0.0
    found = False
    for count, value in _MW_TEXT_RE.findall(str(text)):
        magnitude = parse_number(value)
        if magnitude is None:
            continue
        multiplier = int(count) if count else 1
        total += magnitude * multiplier
        found = True
    return total if found else None


def read_file(path, sheet=None):
    """Read a TCEQ air export (csv / xlsx / saved HTML results table)."""
    lower = str(path).lower()
    if lower.endswith((".html", ".htm")):
        tables = pd.read_html(path)
        if not tables:
            raise ValueError(f"no HTML tables found in {path}")
        # The results grid is the widest table on a TCEQ results page.
        df = max(tables, key=lambda t: (t.shape[1], t.shape[0]))
        df.columns = [clean_str(c) or f"col_{i}" for i, c in enumerate(df.columns)]
        return df.dropna(axis=0, how="all")
    return base.read_table(path, sheet=sheet, expected_tokens=HEADER_TOKENS)


def to_entities(df, source_file_id=None):
    """Normalize TCEQ air rows into entity dicts."""
    resolved = base.resolve_columns(df, COLUMNS)
    if not resolved:
        raise KeyError(
            f"no recognizable TCEQ air columns; headers: {list(df.columns)[:40]}"
        )

    records = []
    for index, row in df.iterrows():
        permit_number = clean_str(base.get(row, resolved, "permit_number"))
        project_number = clean_str(base.get(row, resolved, "project_number"))
        regulated_entity = clean_str(base.get(row, resolved, "regulated_entity"))
        project_name = clean_str(base.get(row, resolved, "project_name"))
        operator = clean_str(base.get(row, resolved, "operator"))

        if not any((permit_number, project_number, regulated_entity, project_name)):
            continue

        # Prefer the permit/project number as the key; an RN alone is a site, not
        # an application, and a site can carry several applications over time.
        source_key = (
            project_number
            or permit_number
            or f"{regulated_entity or project_name}|{index}"
        )

        description = clean_str(base.get(row, resolved, "description"))
        permit_type = clean_str(base.get(row, resolved, "permit_type"))

        capacity = base.get(row, resolved, "capacity_mw")
        if capacity is None:
            capacity = capacity_from_text(
                " ".join(part for part in (project_name, description) if part)
            )

        address_parts = [
            clean_str(base.get(row, resolved, "address")),
            clean_str(base.get(row, resolved, "city")),
        ]
        address = ", ".join(part for part in address_parts if part) or None

        status_date = (
            base.get(row, resolved, "issued_date")
            or base.get(row, resolved, "received_date")
        )

        records.append(
            base.build_entity(
                source=SOURCE,
                source_key=source_key,
                raw_row=row.to_dict(),
                project_name=project_name or operator,
                operator=operator,
                county=base.get(row, resolved, "county"),
                address=address,
                latitude=base.get(row, resolved, "latitude"),
                longitude=base.get(row, resolved, "longitude"),
                description=description,
                technology=description,
                capacity_mw=capacity,
                status=base.get(row, resolved, "status"),
                status_date=status_date,
                permit_type=permit_type or "TCEQ air NSR",
                permit_number=permit_number or project_number,
                regulated_entity=regulated_entity,
                customer_number=base.get(row, resolved, "customer_number"),
                naics=base.get(row, resolved, "naics"),
                sic=base.get(row, resolved, "sic"),
                url=base.get(row, resolved, "url"),
                source_file_id=source_file_id,
            )
        )
    return records


def central_registry_url(rn_number):
    """Public Central Registry detail URL for a regulated entity number."""
    rn = re.sub(r"[^0-9]", "", str(rn_number or ""))
    if not rn:
        return None
    query = urllib.parse.urlencode(
        {"fuseaction": "regent.RNSearch", "re_ref_num": rn}
    )
    return f"{CENTRAL_REGISTRY_BASE}?{query}"


def fetch_central_registry(rn_number, timeout=60):
    """Fetch and parse a Central Registry detail page into a DataFrame.

    Requires network access to www15.tceq.texas.gov. Returns the widest table on
    the page, which is where the regulated-entity attributes live.
    """
    url = central_registry_url(rn_number)
    if not url:
        raise ValueError("a numeric RN number is required")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")
    tables = pd.read_html(html)
    if not tables:
        raise ValueError(f"no tables parsed from Central Registry page for {rn_number}")
    return max(tables, key=lambda t: (t.shape[1], t.shape[0]))
