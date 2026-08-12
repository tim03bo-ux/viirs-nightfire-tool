"""
tceq_records.py — TCEQ Records Online document search and PDF extraction.

Unit megawatt ratings and engine make/model are not in any TCEQ *query* output:
not in the NSR permit search (19 fields, no capacity), not in Central Registry.
They exist only inside the permit documents. This module reaches them.

The route, worked out against the live site:

  1. GET  /cs/idcplg?IdcService=TCEQ_SEARCH_ADD_ACCESS&clientIP=<ip>&IsJson=1
         -> LocalData.accessID. The search refuses to run without one.
  2. GET  /cs/idcplg?IdcService=TCEQ_PERFORM_SEARCH&IsJson=1&QueryText=...
         -> JSON. ResultSets.SearchResults holds one row per document with 111
            fields, including dID, dDocName, dExtension, dDocTitle,
            xRecordSeries, xRefNumTxt and a direct URL path.
  3. GET  /cs/idcplg?IdcService=GET_FILE&dID=<dID>  (or the row's URL)
         -> the file itself.

`IsJson=1` is what makes this workable. The HTML results page renders its rows
client-side, so scraping it returns page furniture and nothing else; the JSON
service returns the same data as data.

Caveats worth keeping in view:

  * Coverage is uneven by nature. Older records are scans whose OCR ranges from
    good to unusable ("Ar R D E RC_10257 4803_404323"), so extraction returns
    None rather than a guess when the text is not legible.
  * A single regulated entity can carry thousands of documents — ExxonMobil
    Baytown returns 4,788 — so searches should be narrowed by record series and
    the document list filtered before anything is downloaded.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import net

BASE = "https://records.tceq.texas.gov/cs/idcplg"
USER_AGENT = "viirs-nightfire-tool/permits (+https://github.com/tim03bo-ux/viirs-nightfire-tool)"

# Record series worth searching for generation work. Values from the live
# xRecordSeries select.
# Verified against the live xRecordSeries select. There is no separate series
# for standard permits or permits by rule — 30 TAC 116 rides under New Source
# Review, so 1081 covers case-by-case permits, Subchapter F standard permits and
# registrations alike. An earlier guess of "1101" for standard permits matched
# nothing and would have returned silently empty results.
RECORD_SERIES = {
    "nsr_permit": "1081",              # incl. standard permits and registrations
    "nsr_county_general": "1091",
    "federal_operating_permit": "1051",
    "emissions_reduction_credit": "1041",
    "discrete_emission_credit": "1021",
    "emissions_banking": "1031",
}

# Document titles that plausibly carry unit tables. Applied to dDocTitle before
# downloading, because a permit file can run to thousands of scanned pages of
# correspondence that will never contain a rating.
# Distinct other regulated entities named in one document, above which it is
# treated as a register covering many facilities rather than one plant's file.
REGISTER_RN_THRESHOLD = 5

USEFUL_TITLE_HINTS = [
    "application", "maert", "emission", "permit", "technical", "review",
    "table", "unit", "attachment", "amendment", "project",
]


def _get(url, timeout=90, retries=None):
    """Fetch a URL as bytes, retrying transport failures with backoff.

    Delegated to `permits.net`, which re-discovers the sandbox proxy's port when
    a connection is refused. Rebuilding `ProxyHandler()` per request was not
    enough on its own: it re-reads `os.environ`, and a process that is already
    running never sees the new port, so a long sweep degrades into an unbroken
    run of "connection refused" while looking alive. Only connection-level
    errors are retried — an HTTP status is the server's answer.
    """
    return net.request_bytes(url, timeout=timeout, retries=retries)


def client_ip(timeout=30):
    """The site records the caller's IP with the access grant, as its own JS does."""
    try:
        payload = _get("https://api.ipify.org?format=json", timeout=timeout)
        return json.loads(payload.decode("utf-8"))["ip"]
    except Exception:
        return "0.0.0.0"


def get_access_id(ip=None, timeout=60):
    """Obtain a search access id. Required — the search errors server-side without."""
    ip = ip or client_ip()
    query = urllib.parse.urlencode({
        "IdcService": "TCEQ_SEARCH_ADD_ACCESS", "clientIP": ip,
        "searchType": "External", "IsJson": "1",
    })
    payload = _get(f"{BASE}?{query}", timeout=timeout)
    data = json.loads(payload.decode("utf-8", errors="replace"))
    return data.get("LocalData", {}).get("accessID"), ip


def search_documents(rn_number=None, permit_number=None, entity_name=None,
                     record_series=None, result_count=50, start_row=0,
                     access=None, timeout=120):
    """Return a list of document dicts for a regulated entity or permit.

    Exactly one of rn_number / permit_number / entity_name is expected; they are
    combined with AND when more than one is given.
    """
    clauses = []
    if rn_number:
        clauses.append(f"xRefNumTxt <matches> `{rn_number}`")
    if permit_number:
        clauses.append(f"xPrimaryID <matches> `{permit_number}`")
    if entity_name:
        clauses.append(f"xRegEntName <substring> `{entity_name}`")
    if record_series:
        series = RECORD_SERIES.get(record_series, record_series)
        clauses.append(f"xRecordSeries <matches> `{series}`")
    if not clauses:
        raise ValueError("give at least one of rn_number, permit_number, entity_name")

    access_id, ip = access or get_access_id()
    query = urllib.parse.urlencode({
        "IdcService": "TCEQ_PERFORM_SEARCH", "IsJson": "1",
        "IsExternalSearch": "1", "xIdcProfile": "Record",
        "QueryText": " <AND> ".join(clauses),
        "ResultCount": str(result_count), "StartRow": str(start_row + 1),
        "SortField": "dInDate", "SortOrder": "Desc",
        "accessID": access_id or "", "clientIP": ip,
    })
    payload = _get(f"{BASE}?{query}", timeout=timeout)
    data = json.loads(payload.decode("utf-8", errors="replace"))

    result_set = data.get("ResultSets", {}).get("SearchResults")
    if not result_set:
        return [], 0
    names = [field["name"] for field in result_set["fields"]]
    rows = [dict(zip(names, row)) for row in result_set["rows"]]

    total = 0
    enterprise = data.get("ResultSets", {}).get("EnterpriseSearchResults")
    if enterprise and enterprise.get("rows"):
        keys = [field["name"] for field in enterprise["fields"]]
        first = dict(zip(keys, enterprise["rows"][0]))
        total = int(first.get("TotalRows") or 0)

    documents = [
        {
            "doc_id": row.get("dID"),
            "doc_name": row.get("dDocName"),
            "title": row.get("dDocTitle"),
            "extension": (row.get("dExtension") or "").lower(),
            "record_series": row.get("xRecordSeries"),
            "regulated_entity": row.get("xRefNumTxt"),
            "entity_name": row.get("xRegEntName"),
            "created": row.get("dDocCreatedDate"),
            "url": row.get("URL"),
        }
        for row in rows
    ]
    return documents, total


def search_all_documents(page_size=50, max_results=None, access=None, **kw):
    """Every document for a search, following the result pages.

    `search_documents` returns one page. A station's docket runs to hundreds of
    records — 743 for one Houston plant — so reading page one and stopping was
    silently sampling the most recent 50 and calling it the docket. The unit
    tables sit in the original application, which is the *oldest* record, so the
    one page we did read was the least likely to hold what we came for.

    The access grant is fetched once and reused across pages: it is tied to a
    session, and taking a new one per page invalidates the one in flight.
    """
    access = access or get_access_id()
    documents, total = search_documents(
        result_count=page_size, start_row=0, access=access, **kw
    )
    if not documents:
        return [], total
    seen = {doc["doc_id"] for doc in documents}
    while len(documents) < (total or 0):
        if max_results and len(documents) >= max_results:
            break
        page, _ = search_documents(
            result_count=page_size, start_row=len(documents), access=access, **kw
        )
        # A server that ignores StartRow, or a docket that shrank mid-sweep,
        # would otherwise spin here forever re-reading the same page.
        fresh = [doc for doc in page if doc["doc_id"] not in seen]
        if not fresh:
            break
        seen.update(doc["doc_id"] for doc in fresh)
        documents.extend(fresh)
    return (documents[:max_results] if max_results else documents), total


def doc_date(document):
    """Sortable date for a document row, oldest sorting first.

    dDocCreatedDate arrives as "11/16/2016 3:42 PM". Sorting those as strings
    puts "1/10/2019" before "11/16/2016", so an oldest-first truncation would
    have kept whatever happened to start with a low digit. Unparseable dates
    sort last, where they cost nothing.
    """
    raw = (document.get("created") or "").strip()
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if not match:
        return (9999, 12, 31)
    month, day, year = (int(part) for part in match.groups())
    return (year, month, day)


def looks_useful(document):
    """Cheap filter so a scrape does not pull thousands of scanned letters."""
    if document.get("extension") not in ("pdf", "tif", "tiff"):
        return False
    title = (document.get("title") or "").lower()
    return any(hint in title for hint in USEFUL_TITLE_HINTS)


def download(document, dest_dir, timeout=240):
    """Fetch one document. Returns the local path, or None on failure."""
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(
        dest_dir, f"{document['doc_id']}_{document.get('doc_name') or 'doc'}"
        f".{document.get('extension') or 'pdf'}"
    )
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    # GET_FILE, not the row's URL path: that path serves an HTML interstitial
    # for many records and only sometimes the file itself.
    full = f"{BASE}?IdcService=GET_FILE&dID={document['doc_id']}&noSaveAs=1&allowInterrupt=1"
    try:
        payload = _get(full, timeout=timeout)
    except Exception:
        return None
    # A short HTML body means the interstitial came back instead of the document.
    if not payload or payload[:9].lstrip()[:5].upper() == b"<!DOC":
        return None
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


# --- Extraction ---------------------------------------------------------------

# Engine and turbine makers that appear in Texas air permit unit tables.
MANUFACTURERS = [
    "caterpillar", "wartsila", "wärtsilä", "cummins", "waukesha",
    "jenbacher", "innio", "man energy", "mtu", "rolls-royce", "guascor",
    "solar turbines", "siemens", "ge vernova", "general electric", "mitsubishi",
    "pratt & whitney", "kawasaki", "capstone", "detroit diesel", "perkins",
    "generac", "kohler", "baldor", "deutz", "yanmar", "doosan", "volvo penta",
    "clarke", "scania",
]

# "2 x 12.5 MW", "twenty-two 12 MW", "rated at 45.6 MW", "45,600 kW"
_MW_RE = re.compile(
    r"(?:(\d{1,3})\s*(?:x|×|units?\s+of)\s*)?"
    r"(\d[\d,]*\.?\d*)\s*(MW|megawatts?|kW|kilowatts?)\b",
    re.IGNORECASE,
)
_MODEL_RE = re.compile(
    r"\b(?:model|type)\s*(?:no\.?|number|:)?\s*([A-Z0-9][A-Z0-9\-/]{2,18})\b",
    re.IGNORECASE,
)


def extract_tables(path, max_pages=30):
    """Table rows as text lines, one line per row, cells joined by ' | '.

    `max_pages=None` reads the whole file. The default stops at 30, which is
    wrong for a permit application: the MAERT and unit tables are appendices,
    routinely past page 100, so a capped read scans the cover letter and the
    narrative and misses the only pages that carry a rating.

    pypdf flattens a table into one line per *cell*, which separates a
    manufacturer from the rating sitting in the next column and makes same-row
    pairing impossible. pdfplumber reconstructs the row, so "Caterpillar |
    G3520C | 2.0 MW" arrives intact.
    """
    try:
        import pdfplumber
    except ImportError:
        return ""
    lines = []
    try:
        with pdfplumber.open(path) as document:
            pages = document.pages if max_pages is None else document.pages[:max_pages]
            for page in pages:
                try:
                    tables = page.extract_tables() or []
                except Exception:
                    continue
                for table in tables:
                    for row in table:
                        cells = [
                            re.sub(r"\s+", " ", str(cell)).strip()
                            for cell in row if cell
                        ]
                        if cells:
                            lines.append(" | ".join(cells))
    except Exception:
        return ""
    return "\n".join(lines)


def extract_text(path, max_pages=40):
    """Text of the first `max_pages` pages, or '' when unreadable.

    `max_pages=None` reads every page. See extract_tables on why the cap loses
    exactly the pages worth reading.

    Scanned records with poor OCR return little or nothing; that is reported as
    empty rather than guessed at.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(path)
    except Exception:
        return ""
    chunks = []
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    for page in pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)


# A PSD application must survey comparable plants for its BACT demonstration,
# so every one carries a table of other companies' permits: "FGE Power, LLC
# Westbrook TX 1,620 MW Combined Cycle". Those rows name a company and a place,
# and reading a document in full ingests all of them as if they were the
# applicant's. They carry no RN, so the register guard cannot see them.
_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()
_PRECEDENT_RE = re.compile(
    r"\b(?:L\.?L\.?C|L\.?P|Inc|Corp(?:oration)?|Compan(?:y|ies)|Cooperative|"
    r"Partners(?:hip)?|Ltd)\b\.?[^.\n]{0,40}?\b(?:" + "|".join(_STATES) + r")\b"
)


def looks_like_precedent_row(line, entity_name=None):
    """True when a line is another company's entry in a comparison table.

    Keyed on company-plus-location, which is the shape of a BACT precedent row
    and not the shape of an equipment line: "Siemens Energy, Inc. will supply"
    names a company but no place, so it survives.
    """
    if not _PRECEDENT_RE.search(line):
        return False
    if entity_name:
        # The applicant appears in its own table. Any distinctive word of its
        # name on the line means the row is about this plant.
        own = [word for word in re.findall(r"[a-z]{4,}", entity_name.lower())
               if word not in _GENERIC_NAME_WORDS]
        lowered = line.lower()
        if any(word in lowered for word in own):
            return False
    return True


_GENERIC_NAME_WORDS = frozenset({
    "energy", "power", "center", "centre", "plant", "station", "generating",
    "generation", "electric", "company", "corporation", "holdings", "partners",
})


def extract_units(text, entity_name=None):
    """Pull candidate unit ratings and manufacturers out of permit text.

    Returns {'mw_values': [...], 'max_mw':, 'total_mw':, 'manufacturers': [...],
    'models': [...]}. Everything is a candidate, not a verified nameplate — the
    text around these numbers is inconsistent and often OCR'd, so the caller
    should treat them as evidence to review rather than as facts.
    """
    if not text:
        return {"mw_values": [], "max_mw": None, "total_mw": None,
                "manufacturers": [], "models": [], "precedent_rows": 0}

    lowered = text.lower()
    values = []
    precedent_rows = 0
    # Scanned line by line rather than over the whole text, so a megawatt figure
    # can be judged by the company it keeps.
    for line in text.splitlines():
        if looks_like_precedent_row(line, entity_name):
            precedent_rows += 1
            continue
        for count, magnitude, unit in _MW_RE.findall(line):
            try:
                number = float(magnitude.replace(",", ""))
            except ValueError:
                continue
            if unit.lower().startswith("k"):
                number /= 1000.0
            # Discard implausible readings: OCR turns table rules into digits.
            if not (0.01 <= number <= 5000):
                continue
            multiplier = int(count) if count else 1
            values.append(round(number * multiplier, 3))

    makers = sorted({
        maker for maker in MANUFACTURERS
        if re.search(rf"\b{re.escape(maker)}\b", lowered)
    })
    # A manufacturer that is merely the site's own name is not evidence about
    # its equipment: "SOLAR TURBINES DLS OVERHAUL CENTER" is a repair shop, and
    # every page of its file says "Solar Turbines".
    if entity_name:
        own = entity_name.lower()
        makers = [m for m in makers if m not in own]
    models = sorted(set(_MODEL_RE.findall(text)))[:12]

    return {
        "mw_values": sorted(values, reverse=True)[:25],
        "max_mw": max(values) if values else None,
        "total_mw": round(sum(values), 2) if values else None,
        "manufacturers": makers,
        "models": models,
        "precedent_rows": precedent_rows,
    }




# --- Unit-level pairing -------------------------------------------------------

# A rating is only evidence about a manufacturer's equipment when the two sit
# together in the same unit-table row. Document-level aggregation cannot tell a
# 400 MW plant's GE turbines from the Cummins emergency genset parked beside
# them: both inherit the largest number anywhere in the file.

_LINE_MW_RE = re.compile(
    r"(\d[\d,]*\.?\d*)\s*(MW|megawatts?|kW|kilowatts?)\b", re.IGNORECASE
)
# "(2)", "2 x", "two", "Qty: 3" — the count that multiplies a per-unit rating.
# TCEQ sizes combustion units the way an air permit does — horsepower for
# engines, MMBtu/hr heat input for turbines and boilers. Megawatts are an
# electrical-output concept that belongs to ERCOT, and are largely absent:
# a real unit table reads "Cummins QSK60G (NG-Fired Engine) | 8.58 | 4.29 | ..."
# where those numbers are lb/hr and tpy emission rates, with the size stated in
# the narrative as "1,945 horsepower".
_LINE_HP_RE = re.compile(
    r"(\d[\d,]*\.?\d*)\s*(?:horsepower|hp)\b", re.IGNORECASE
)
_LINE_MMBTU_RE = re.compile(
    r"(\d[\d,]*\.?\d*)\s*MM\s?Btu\s*/?\s*(?:hr|hour)", re.IGNORECASE
)

# Shaft horsepower to megawatts. Mechanical output, not generator terminal
# output — an alternator loses a few percent — so a derived figure is flagged
# as such rather than presented as a nameplate rating.
HP_TO_MW = 0.000745699


def _line_hp(line):
    values = []
    for magnitude in _LINE_HP_RE.findall(line):
        try:
            number = float(magnitude.replace(",", ""))
        except ValueError:
            continue
        if 1 <= number <= 200000:
            values.append(number)
    return values


def _line_mmbtu(line):
    values = []
    for magnitude in _LINE_MMBTU_RE.findall(line):
        try:
            number = float(magnitude.replace(",", ""))
        except ValueError:
            continue
        if 0.1 <= number <= 20000:
            values.append(number)
    return values


_LINE_COUNT_RE = re.compile(
    r"(?:qty\.?\s*:?\s*(\d{1,3})\b)"
    r"|(?:\((\d{1,3})\))"
    r"|(?:\b(\d{1,3})\s*(?:x|×)\s)",
    re.IGNORECASE,
)
# Two shapes cover most unit tables: "G3520C" (letters then a run of digits) and
# "SGT6-5000F" (letters, a digit, then a hyphenated block). Requiring two digits
# in the first form keeps emission point numbers — EPN-1, GT-2 — out.
_LINE_MODEL_RE = re.compile(
    r"\b([A-Z]{1,4}[\-]?\d{2,5}[A-Z0-9\-]{0,6})\b"
    r"|\b([A-Z]{2,4}\d{1,2}[\-]\d{2,5}[A-Z]{0,3})\b"
)


def _line_mw(line):
    """Ratings stated on one line, normalized to MW."""
    values = []
    for magnitude, unit in _LINE_MW_RE.findall(line):
        try:
            number = float(magnitude.replace(",", ""))
        except ValueError:
            continue
        if unit.lower().startswith("k"):
            number /= 1000.0
        if 0.01 <= number <= 5000:
            values.append(round(number, 3))
    return values


def _line_makers(line_lower):
    return [
        maker for maker in MANUFACTURERS
        if re.search(rf"\b{re.escape(maker)}\b", line_lower)
    ]


_RN_RE = re.compile(r"\bRN\d{9}\b")


def foreign_rns(line, rn_number):
    """Regulated-entity numbers on this line that are not the one being scraped.

    A docket's "Project File Folder" is not always about one facility. Rockwood
    Energy Center's folder carries a register listing many plants, so reading it
    in full attributed DCP Midstream's Wilcox gas plant engines -- and a 1,620 MW
    figure from some third station -- to Rockwood. The page caps used to hide
    this by never reaching those pages; reading the whole document exposes it,
    so the rows have to be attributed rather than merely found.
    """
    if not rn_number:
        return []
    return [found for found in _RN_RE.findall(line) if found != rn_number]


def extract_unit_records(text, entity_name=None, window=1, rn_number=None):
    """Pair manufacturers with the ratings stated alongside them.

    Returns a list of unit dicts, each carrying the manufacturer, the rating in
    MW, an optional model and unit count, and how the pairing was made:

        same_line  manufacturer and rating on one row — trustworthy
        nearby     found within `window` lines — a wrapped table row, weaker

    A manufacturer with no rating near it still yields a record with mw=None:
    knowing Cummins equipment is present is worth keeping even when the rating
    is unreadable, and it keeps the two facts from being silently merged.
    """
    if not text:
        return []

    own = (entity_name or "").lower()
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    units = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        makers = [maker for maker in _line_makers(lowered) if maker not in own]
        if not makers:
            continue
        # A line that names someone else's regulated entity is describing
        # someone else's equipment, whatever else it happens to contain.
        if foreign_rns(line, rn_number):
            continue

        values = _line_mw(line)
        horsepower = _line_hp(line)
        heat_input = _line_mmbtu(line)
        proximity = "same_line"
        if not (values or horsepower or heat_input):
            for offset in range(1, window + 1):
                for neighbour in (index - offset, index + offset):
                    if 0 <= neighbour < len(lines):
                        values = values or _line_mw(lines[neighbour])
                        horsepower = horsepower or _line_hp(lines[neighbour])
                        heat_input = heat_input or _line_mmbtu(lines[neighbour])
            proximity = (
                "nearby" if (values or horsepower or heat_input) else "unrated"
            )

        count_match = _LINE_COUNT_RE.search(line)
        count = next(
            (int(group) for group in (count_match.groups() if count_match else ())
             if group), None
        )
        models = [
            model for match in _LINE_MODEL_RE.findall(line)
            for model in (match if isinstance(match, tuple) else (match,)) if model
        ]
        # Neither a bare year nor an emission point number is a model. TCEQ
        # tables lead with EPN/ENG/GT identifiers that match the same shape as
        # a real model code, and they sort first on the line.
        models = [
            m for m in models
            if not re.fullmatch(r"(19|20)\d{2}", m)
            and not re.match(r"^(EPN|ENG|EG|GT|FIN|STK|TK|VENT|CT|HRSG)[\-]?\d",
                             m, re.IGNORECASE)
        ]

        for maker in makers:
            derived = (
                round(min(horsepower) * HP_TO_MW, 3) if horsepower else None
            )
            units.append({
                "manufacturer": maker,
                "mw": min(values) if values else None,
                "hp": min(horsepower) if horsepower else None,
                "mmbtu_hr": min(heat_input) if heat_input else None,
                # Derived from horsepower when the permit states no megawatts,
                # which is the usual case.
                "mw_from_hp": derived,
                "mw_candidates": sorted(set(values))[:6],
                "count": count,
                "model": models[0] if models else None,
                "proximity": proximity,
                "context": line[:180],
            })
    return units


def summarize_units(units):
    """Collapse unit records into per-manufacturer figures.

    Only same-line pairings are used for the rating: a "nearby" match is kept as
    evidence of presence but is not strong enough to attribute a number to.
    """
    by_maker = {}
    for unit in units:
        entry = by_maker.setdefault(unit["manufacturer"], {
            "manufacturer": unit["manufacturer"], "unit_mw": [], "unit_hp": [],
            "models": [],
            "mentions": 0, "same_line": 0,
        })
        entry["mentions"] += 1
        rating = unit.get("mw") or unit.get("mw_from_hp")
        if unit["proximity"] == "same_line" and rating is not None:
            entry["same_line"] += 1
            entry["unit_mw"].append(rating)
        if unit.get("hp"):
            entry.setdefault("unit_hp", []).append(unit["hp"])
        if unit.get("model"):
            entry["models"].append(unit["model"])

    summary = []
    for entry in by_maker.values():
        ratings = entry.pop("unit_mw")
        horsepowers = entry.pop("unit_hp", [])
        entry["max_unit_hp"] = max(horsepowers) if horsepowers else None
        entry["models"] = sorted(set(entry["models"]))[:6]
        entry["max_unit_mw"] = max(ratings) if ratings else None
        entry["min_unit_mw"] = min(ratings) if ratings else None
        entry["n_rated"] = len(ratings)
        summary.append(entry)
    return sorted(summary, key=lambda e: (-(e["max_unit_mw"] or 0), e["manufacturer"]))


def scrape_entity(rn_number, dest_dir, record_series="nsr_permit", max_docs=6,
                  access=None, delay=1.0, verbose=True, keep_files=False,
                  max_bytes=40 * 1024 * 1024, max_pages=40,
                  useful_only=True):
    """Search, download and extract for one regulated entity.

    Returns a dict with the documents examined and the merged extraction.

    Four independent ceilings decide how much of a docket is actually read, and
    every one of them silently returns a partial answer that looks complete:

      max_docs    documents opened, of those matching the title filter
      max_bytes   files above this are downloaded and thrown away unread
      max_pages   pages read per file (None = all)
      useful_only whether the title filter applies at all

    Pass max_docs=None, max_bytes=None, max_pages=None for an exhaustive sweep.
    """
    documents, total = search_all_documents(
        rn_number=rn_number, record_series=record_series, access=access
    )
    useful = [doc for doc in documents if looks_useful(doc)] if useful_only \
        else [doc for doc in documents
              if doc.get("extension") in ("pdf", "tif", "tiff")]
    if max_docs:
        # Oldest first, because the original application carries the unit table
        # and the search hands back newest first. Truncating a newest-first list
        # keeps the correspondence and drops the application.
        useful = sorted(useful, key=doc_date)[:max_docs]
    if verbose:
        print(f"  {rn_number}: {total} documents, {len(useful)} worth opening")

    findings = []
    for document in useful:
        path = download(document, dest_dir)
        if not path:
            continue
        size = os.path.getsize(path)
        try:
            # Permit files are big — five entities pulled 726 MB, which projects
            # to hundreds of gigabytes across the state. The text is what is
            # wanted, so it is taken and the file dropped unless asked to keep.
            if max_bytes and size > max_bytes:
                extracted = extract_units("")
                extracted["skipped"] = f"file too large ({size // 1048576} MB)"
            else:
                page_text = extract_text(path, max_pages=max_pages)
                table_text = extract_tables(path, max_pages=max_pages)
                extracted = extract_units(
                    page_text, entity_name=document.get("entity_name")
                )
                others = set(foreign_rns(page_text, rn_number))
                if len(others) >= REGISTER_RN_THRESHOLD:
                    # A multi-facility register, not this plant's application.
                    # Its prose megawatts belong to whichever station the
                    # sentence was about, so taking the largest attributes some
                    # other plant's capacity to this one.
                    extracted["register_rns"] = len(others)
                    extracted["mw_values"] = []
                    extracted["max_mw"] = None
                    extracted["total_mw"] = None
                # Table rows first: they are the only place a manufacturer and
                # its rating reliably share a line.
                extracted["units"] = (
                    extract_unit_records(
                        table_text, entity_name=document.get("entity_name"),
                        rn_number=rn_number,
                    )
                    + extract_unit_records(
                        page_text, entity_name=document.get("entity_name"),
                        rn_number=rn_number,
                    )
                )
        finally:
            if not keep_files:
                try:
                    os.remove(path)
                except OSError:
                    pass
        extracted["title"] = document.get("title")
        extracted["doc_id"] = document.get("doc_id")
        extracted["bytes"] = size
        findings.append(extracted)
        time.sleep(delay)

    all_units = [u for f in findings for u in (f.get("units") or [])]
    merged = {
        "regulated_entity": rn_number,
        "units": all_units,
        "by_manufacturer": summarize_units(all_units),
        "entity_name": documents[0].get("entity_name") if documents else None,
        "documents_total": total,
        "documents_matched": len(documents),
        "documents_useful": len(useful),
        "documents_read": len(findings),
        "max_mw": max(
            [f["max_mw"] for f in findings if f["max_mw"] is not None] or [0]
        ) or None,
        "manufacturers": sorted({
            maker for f in findings for maker in f["manufacturers"]
        }),
        "models": sorted({model for f in findings for model in f["models"]})[:12],
        "findings": findings,
    }
    return merged
