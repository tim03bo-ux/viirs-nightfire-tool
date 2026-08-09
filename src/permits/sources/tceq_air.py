"""
tceq_air.py — TCEQ air permits and permit applications.

Covers every air authorization family TCEQ issues, both **already approved** and
**still sitting in process**:

  * New Source Review case-by-case permits (30 TAC 116), including the major
    source flavours — PSD and nonattainment NSR
  * Permits by Rule (30 TAC 106) — the registration route most backup-generator
    fleets take
  * Standard Permits (30 TAC 116 Subchapter F)
  * Title V federal operating permits (30 TAC 122)

Air permits are the strongest evidence that a project burns something: a data
center campus with a fleet of backup diesel gensets, a behind-the-meter gas
peaker, or a full combined-cycle plant all have to be authorized here, and the
application states the units, fuel and ratings — plus, on a case-by-case permit,
the allowable emission rates.

The program and the lifecycle (pending vs issued) are derived per row from the
permit type, permit number and status text, so a pending-applications export and
an issued-permits export ingest through exactly the same path — load both and
the database holds the full picture.

Useful TCEQ products, all of which this adapter can read once exported to
csv/xlsx:

  * Air NSR permit applications pending / recently issued
      https://www.tceq.texas.gov/permitting/air/nav/air_pendingpermits.html
  * Air permits issued, by county / by program
      https://www.tceq.texas.gov/permitting/air/
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
                    "Permit Action", "Program", "Type"],
    "status": ["Application Status", "Permit Status", "Status",
               "Review Status", "Current Status"],
    "received_date": ["Received Date", "Date Received", "Application Received",
                      "Filed Date", "Submitted Date", "Application Date"],
    "issued_date": ["Issued Date", "Date Issued", "Final Action Date",
                    "Effective Date", "Approval Date"],
    "description": ["Project Description", "Description", "Project Type",
                    "Facility Description", "Process Description", "Comments"],
    "naics": ["NAICS", "NAICS Code", "Primary NAICS"],
    "sic": ["SIC", "SIC Code", "Primary SIC"],
    "capacity_mw": ["Capacity (MW)", "Rated Capacity MW", "MW", "Megawatts"],
    # Allowable (permitted) emission rates, tons per year. TCEQ publishes these
    # on the MAERT for case-by-case permits; many exports carry a subset.
    "nox_tpy": ["NOx (TPY)", "NOX TPY", "NOx Allowable", "Nitrogen Oxides", "NOx"],
    "co_tpy": ["CO (TPY)", "CO TPY", "Carbon Monoxide", "CO Allowable"],
    "voc_tpy": ["VOC (TPY)", "VOC TPY", "Volatile Organic", "VOC Allowable"],
    "pm_tpy": ["PM2.5 (TPY)", "PM10 (TPY)", "PM TPY", "Particulate Matter", "PM"],
    "so2_tpy": ["SO2 (TPY)", "SO2 TPY", "Sulfur Dioxide", "SOx"],
    "ghg_tpy": ["CO2e (TPY)", "GHG TPY", "Greenhouse Gas", "CO2e"],
    "url": ["URL", "Link", "Document Link"],
}

# Authorization families this adapter reads. All of them ride in the same table:
# the program is derived per row from the permit type and permit number, so a
# pending-applications export and an issued-permits export ingest identically.
PROGRAMS_COVERED = (
    "New Source Review (case-by-case, including PSD and nonattainment), "
    "Permits by Rule (30 TAC 106), Standard Permits (30 TAC 116 Subchapter F), "
    "and Title V federal operating permits (30 TAC 122)"
)

# Unit counts in permit descriptions are written both ways — "8 x 37.5 MW" and
# "Twenty-two 12 MW engines" — and missing the spelled-out form turns a 264 MW
# power block into a 12 MW one, so both are parsed.
_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}

_WORD_NUMBER = "|".join(
    sorted(list(_TENS) + list(_UNITS), key=len, reverse=True)
)
# Optional count, then the rating. The count is either digits followed by 'x',
# or a (possibly hyphenated) number word directly preceding the rating.
_MW_TEXT_RE = re.compile(
    r"(?:(\d+)\s*[x×]\s*)?"
    rf"(?:\b((?:{_WORD_NUMBER})(?:[\s-]+(?:{_WORD_NUMBER}))?)\s+)?"
    r"(\d[\d,]*\.?\d*)\s*(?:mw\b|megawatt)",
    re.IGNORECASE,
)


def word_to_int(text):
    """'twenty-two' -> 22, 'eight' -> 8. None when unparseable."""
    if not text:
        return None
    parts = [part for part in re.split(r"[\s-]+", str(text).lower()) if part]
    total = 0
    matched = False
    for part in parts:
        if part in _TENS:
            total += _TENS[part]
            matched = True
        elif part in _UNITS:
            total += _UNITS[part]
            matched = True
        else:
            return None
    return total if matched else None


def capacity_from_text(text):
    """Total MW implied by a description, or None.

    '2 x 45 MW' -> 90.0, 'Twenty-two 12 MW engines' -> 264.0, '250 MW' -> 250.0.
    Several ratings in one description are summed, which is what a permit listing
    multiple unit types means.
    """
    if not text:
        return None
    total = 0.0
    found = False
    for digit_count, word_count, value in _MW_TEXT_RE.findall(str(text)):
        magnitude = parse_number(value)
        if magnitude is None:
            continue
        if digit_count:
            multiplier = int(digit_count)
        else:
            multiplier = word_to_int(word_count) or 1
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
                received_date=base.get(row, resolved, "received_date"),
                decision_date=base.get(row, resolved, "issued_date"),
                permit_type=permit_type or "TCEQ air permit",
                permit_number=permit_number or project_number,
                regulated_entity=regulated_entity,
                customer_number=base.get(row, resolved, "customer_number"),
                naics=base.get(row, resolved, "naics"),
                sic=base.get(row, resolved, "sic"),
                nox_tpy=base.get(row, resolved, "nox_tpy"),
                co_tpy=base.get(row, resolved, "co_tpy"),
                voc_tpy=base.get(row, resolved, "voc_tpy"),
                pm_tpy=base.get(row, resolved, "pm_tpy"),
                so2_tpy=base.get(row, resolved, "so2_tpy"),
                ghg_tpy=base.get(row, resolved, "ghg_tpy"),
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
