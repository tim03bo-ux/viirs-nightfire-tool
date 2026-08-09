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
import urllib.parse
import urllib.request

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
USEFUL_TITLE_HINTS = [
    "application", "maert", "emission", "permit", "technical", "review",
    "table", "unit", "attachment", "amendment", "project",
]


def _get(url, timeout=90):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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
    "caterpillar", "cat", "wartsila", "wärtsilä", "cummins", "waukesha",
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


def extract_text(path, max_pages=40):
    """Text of the first `max_pages` pages, or '' when unreadable.

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
    for page in reader.pages[:max_pages]:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(chunks)


def extract_units(text, entity_name=None):
    """Pull candidate unit ratings and manufacturers out of permit text.

    Returns {'mw_values': [...], 'max_mw':, 'total_mw':, 'manufacturers': [...],
    'models': [...]}. Everything is a candidate, not a verified nameplate — the
    text around these numbers is inconsistent and often OCR'd, so the caller
    should treat them as evidence to review rather than as facts.
    """
    if not text:
        return {"mw_values": [], "max_mw": None, "total_mw": None,
                "manufacturers": [], "models": []}

    lowered = text.lower()
    values = []
    for count, magnitude, unit in _MW_RE.findall(text):
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
    # A bare "cat" is too common in prose to count on its own.
    if "cat" in makers and "caterpillar" not in makers:
        makers.remove("cat")

    models = sorted(set(_MODEL_RE.findall(text)))[:12]

    return {
        "mw_values": sorted(values, reverse=True)[:25],
        "max_mw": max(values) if values else None,
        "total_mw": round(sum(values), 2) if values else None,
        "manufacturers": makers,
        "models": models,
    }


def scrape_entity(rn_number, dest_dir, record_series="nsr_permit", max_docs=6,
                  access=None, delay=1.0, verbose=True, keep_files=False,
                  max_bytes=40 * 1024 * 1024):
    """Search, download and extract for one regulated entity.

    Returns a dict with the documents examined and the merged extraction.
    """
    documents, total = search_documents(
        rn_number=rn_number, record_series=record_series, access=access
    )
    useful = [doc for doc in documents if looks_useful(doc)][:max_docs]
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
            if size > max_bytes:
                extracted = extract_units("")
                extracted["skipped"] = f"file too large ({size // 1048576} MB)"
            else:
                extracted = extract_units(
                    extract_text(path), entity_name=document.get("entity_name")
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

    merged = {
        "regulated_entity": rn_number,
        "entity_name": documents[0].get("entity_name") if documents else None,
        "documents_total": total,
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
