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

`search()` queries it live. Three things about that app cost real time to work
out, so they are written down here:

  * The query is a POST to index.cfm with `_fuseaction=home.validate_search_crit`
    submitted as an image button, so the parameter arrives as
    `_fuseaction=home.validate_search_crit.x`. A GET with `fuseaction=` (no
    underscore) serves the form; a POST with `_fuseaction=` runs the search.
  * `permit_status` and `app_status` are mutually exclusive. Sending both — the
    obvious thing to do when you want issued permits *and* pending applications
    — fails with "Select either permit OR application status", and the app
    reports it as a generic validation banner. Two queries, not one.
  * Results are held in the session and paged 50 at a time through
    `fuseaction=home.permit_list&CurrentPage=N`, so the cookie jar from the
    search POST has to be carried to every page.

The results grid has no acreage and no coordinates. The per-authorization
summary page has both, plus the site's RN — and the RN is the point: it is the
same identifier TCEQ air permits carry, so a fetched NOI detail joins a
construction site to its air permit by identity rather than by name. That is why
`fetch_detail` exists despite costing one request per record.

Searching by SIC code is what makes this tractable. 4911 (Electric Services)
finds generation and transmission construction statewide; 7374 (Data Processing)
finds data centers and crypto mines — Aligned, Cipher Mining, Riot, QTS all
appear under it. Without SIC the alternative is 254 county queries.

Acreage alone does not say what is being built, so classification here leans on
the operator name and any project description; NOIs with no such signal are
tagged `construction` and surface in the dashboard as leads rather than as
confirmed data centers.
"""

import json
import os
import re
import time
import urllib.parse

import pandas as pd

from . import base
from .. import net
from .. import normalize
from ..normalize import clean_str

SOURCE = "tceq_swnoi"

SEARCH_BASE = "https://www2.tceq.texas.gov/wq_dpa/index.cfm"
PAGE_URL = SEARCH_BASE + "?fuseaction=home.permit_list&CurrentPage={}"
SUMMARY_BASE = "https://www2.tceq.texas.gov/wq_dpa/"

# The option value is a pipe-composite of the program code and the label, and
# the server matches the whole string.
PERMIT_TYPE_NOI = "SWC|Construction Notice of Intent (TXR15)"

PAGE_SIZE = 50

# SIC codes worth sweeping for generation and large load. TCEQ validates each
# code against its own list and rejects the whole search on one bad entry, so
# these are the codes confirmed to exist there — 7370 does not, despite being a
# real SIC code, and 4911 alone is 6,711 authorizations.
SIC_ELECTRIC = ["4911", "4931", "4939"]
SIC_DATA = ["7374", "7379"]
DEFAULT_SIC = SIC_ELECTRIC + SIC_DATA

_BAD_SIC_RE = re.compile(r"SIC Code invalid\s*(?:&#x3a;|:)\s*(\d+)")

GRID_COLUMNS = ["Auth #", "Site Name", "Permittee", "SIC Code", "Segment #",
                "County", "Region", "City", "Site Location"]

# Site-name terms that find generation and large load. Searching by name is the
# only way to reach renewables: they do not cluster under a SIC code the way
# utilities do. Solar and wind NOIs land mostly in 1629 (heavy construction,
# generic) with 4911 a distant second, so a SIC sweep misses them. Name search
# also finds 153 data-center NOIs against SIC 7374's 34.
#
# The app matches substrings, so "WIND" returns Windsor and Winding Creek and
# "BESS" returns OBESSO RESIDENCE. `matches_term` re-checks on word boundaries.
NAME_TERMS_GENERATION = [
    "SOLAR", "WIND", "BESS", "ENERGY STORAGE", "GENERATING", "POWER PLANT",
    "ENERGY CENTER", "SUBSTATION",
]
NAME_TERMS_LOAD = ["DATA CENTER", "DATACENTER", "MINING"]

HEADER_TOKENS = ["Permit", "Operator", "County", "Acres", "Site", "NOI"]

COLUMNS = {
    "permit_number": ["Permit Number", "Auth #", "Authorization Number",
                      "NOI Number", "TXR Number", "Permit No", "Permit"],
    "project_name": ["Site Name on Permit", "site_name", "Site Name",
                     "Project Name", "Project Site Name",
                     "Regulated Entity Name", "RE Name", "Facility Name", "Site"],
    "operator": ["Permittee", "Operator Name", "Operator", "Customer Name",
                 "Company Name", "Applicant", "Owner"],
    "regulated_entity": ["RN Number", "regulated_entity", "RN",
                         "Regulated Entity Number"],
    "customer_number": ["CN Number", "customer_number", "Customer Number", "CN"],
    "sic": ["SIC Code", "Primary SIC Code", "sic_code"],
    "county": ["County"],
    "address": ["Site Location", "Site Address", "Physical Location",
                "Location Description", "Street Address", "Address"],
    "city": ["City", "Nearest City"],
    "latitude": ["Latitude", "Site Latitude", "Lat"],
    "longitude": ["Longitude", "Site Longitude", "Long", "Lon"],
    "acres": ["Area Disturbed (in Acres)", "acres", "Acres Disturbed",
              "Disturbed Acres", "Total Acres", "Project Acres", "Acreage",
              "Acres"],
    "status": ["Authorization Status", "status", "Status", "Permit Status"],
    "issued_date": ["Date Coverage Began", "Issued Date", "Effective Date",
                    "Date Issued", "Authorization Date"],
    "start_date": ["start_date", "Projected Start Date", "Construction Start",
                   "Start Date"],
    "end_date": ["Date Coverage Ended", "end_date", "Projected End Date",
                 "Construction End", "End Date", "Termination Date"],
    "description": ["Project Description", "Description", "Nature of Activity",
                    "Type of Construction", "Comments"],
    "url": ["URL", "Link"],
}


_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_COUNT_RE = re.compile(r"search returned\s+([\d,]+)\s+records", re.I)
_SUMMARY_RE = re.compile(r'href="([^"]*permit_summary[^"]*)"')


def _text(html):
    text = _TAG_RE.sub("", html)
    for code, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#x27;", "'"),
                       ("&#x28;", "("), ("&#x29;", ")"), ("&#x7e;", "~"),
                       ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(code, char)
    return re.sub(r"\s+", " ", text).strip()


def result_count(html):
    match = _COUNT_RE.search(_text(html))
    return int(match.group(1).replace(",", "")) if match else 0


def parse_results(html):
    """Parse one page of the results grid into a DataFrame."""
    records = []
    for row_html in _ROW_RE.findall(html):
        cells = [_text(cell) for cell in _CELL_RE.findall(row_html)]
        if len(cells) < 9 or not cells[0].upper().startswith("TXR"):
            continue
        link = _SUMMARY_RE.search(row_html)
        records.append({
            "Auth #": cells[0], "Site Name": cells[1], "Permittee": cells[2],
            "SIC Code": cells[3], "Segment #": cells[4], "County": cells[5],
            "Region": cells[6], "City": cells[7], "Site Location": cells[8],
            "detail_url": link.group(1).replace("&amp;", "&") if link else None,
        })
    return pd.DataFrame(records, columns=GRID_COLUMNS + ["detail_url"])


def matches_term(name, term):
    """Whether a site name really contains the term, on word boundaries.

    TCEQ matches substrings, so a search for WIND returns Windsor and a search
    for BESS returns OBESSO RESIDENCE. Dropping those is the difference between
    a wind-project list and a subdivision list.
    """
    pattern = r"\b" + r"\s+".join(re.escape(word) for word in str(term).split()) + r"\b"
    return re.search(pattern, str(name or ""), re.I) is not None


def _search_payload(sic=None, county=None, city=None, operator=None,
                    site_name=None, start_date=None, end_date=None,
                    permit_status="ALL", app_status=None, permit_type=None):
    """Build the POST body, in the order and multiplicity the form submits."""
    if permit_status and app_status:
        # The app rejects this outright, and reports it only as a generic
        # banner, so it is worth failing loudly here instead.
        raise ValueError(
            "TCEQ accepts permit_status OR app_status, not both — "
            "run two searches and concatenate"
        )
    pairs = [
        ("newsearch", "yes"),
        ("permit_type", permit_type or PERMIT_TYPE_NOI),
        ("start_date", start_date or ""),
        ("end_date", end_date or ""),
    ]
    if permit_status:
        pairs.append(("permit_status", permit_status))
    if app_status:
        pairs.append(("app_status", app_status))
    pairs += [
        ("princ_name", operator or ""),
        ("phys_name", site_name or ""),
        ("street_name", ""),
        ("city_name", city or ""),
        ("cnty_name", (county or "").upper()),
        ("region_name", ""),
        ("segment_no", ""),
    ]
    # The form renders eight SIC boxes and the server reads them positionally.
    codes = list(sic or [])[:8]
    pairs += [("sic_code", code) for code in codes + [""] * (8 - len(codes))]
    pairs += [
        ("goto", "search"),
        ("permitStatusList", "ACTIVE,DENIED,EXPIRED,TERMINATED,WITHDRAWN"),
        ("appStatusList", "APPROVED,PENDING,DENIED,WITHDRAWN"),
        # Image submit: the browser sends the button's name with .x/.y appended,
        # and the name here already contains an '='.
        ("_fuseaction=home.validate_search_crit.x", "12"),
        ("_fuseaction=home.validate_search_crit.y", "9"),
    ]
    return pairs


def search(sic=None, county=None, city=None, operator=None, site_name=None,
           start_date=None, end_date=None, permit_status="ALL", app_status=None,
           max_pages=None, delay=0.4, timeout=180, verbose=True, jar=None):
    """Query TXR150000 NOIs live. Returns a DataFrame of the results grid.

    Pass `sic` (a list of SIC codes) for a statewide sweep, or `county` to walk
    one county. `permit_status` covers issued authorizations and `app_status`
    covers applications; they cannot be combined, so `search_all` runs both.

    `jar` carries the session. It matters beyond politeness: the per-row detail
    links embed session-scoped record ids, so a detail page fetched outside the
    search's own session quietly returns the search form instead of the record.
    Pass the same jar to `enrich`, or use `collect`, which does it for you.
    """
    jar = jar or net.new_jar()
    # The search POST is only honoured inside a session that has seen the form.
    net.get(SEARCH_BASE, params={"fuseaction": "home.permit_info_search"},
            timeout=timeout, jar=jar)
    html = net.post(
        SEARCH_BASE,
        _search_payload(sic=sic, county=county, city=city, operator=operator,
                        site_name=site_name, start_date=start_date,
                        end_date=end_date, permit_status=permit_status,
                        app_status=app_status),
        timeout=timeout, jar=jar,
    )
    # TCEQ validates SIC codes against its own list and rejects the entire
    # search on a single unknown one, naming it. Dropping the offender and
    # retrying beats losing the sweep over one bad code.
    attempts = 0
    while "Errors were found" in html and attempts < 4:
        bad = _BAD_SIC_RE.search(html)
        if not bad or not sic:
            break
        dropped = bad.group(1)
        remaining = [code for code in sic if code != dropped]
        if remaining == list(sic):
            break
        if verbose:
            print(f"    TCEQ rejects SIC {dropped}; retrying without it")
        sic = remaining
        attempts += 1
        if not sic:
            break
        html = net.post(
            SEARCH_BASE,
            _search_payload(sic=sic, county=county, city=city, operator=operator,
                            site_name=site_name, start_date=start_date,
                            end_date=end_date, permit_status=permit_status,
                            app_status=app_status),
            timeout=timeout, jar=jar,
        )
    if "Errors were found" in html:
        raise ValueError(f"TCEQ rejected the search: {_search_error(html)}")

    total = result_count(html)
    frames = [parse_results(html)]
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if max_pages:
        pages = min(pages, max_pages)
    if verbose:
        print(f"  {total} NOIs, {pages} page(s)")

    for page in range(2, pages + 1):
        time.sleep(delay)
        try:
            frames.append(parse_results(
                net.get(PAGE_URL.format(page), timeout=timeout, jar=jar)
            ))
        except Exception as exc:
            # A statewide sweep is hundreds of pages; losing one beats losing all.
            print(f"    ERROR page {page}: {str(exc)[:90]}")
            continue
        if verbose and page % 20 == 0:
            print(f"    page {page}/{pages}")

    df = pd.concat(frames, ignore_index=True)
    # One authorization appears once per receiving-water segment, so the grid
    # repeats rows that are the same site.
    return df.drop_duplicates(subset=["Auth #"]).reset_index(drop=True)


def _search_error(html):
    """The specific complaint, not the banner.

    The app renders a generic "Errors were found while validating your search
    data" heading and puts the useful line — "Select either permit OR
    application status", "SIC Code invalid : 7370" — a little further down, so
    the banner alone tells you nothing.
    """
    text = _text(re.sub(r"<script.*?</script>", "", html, flags=re.S))
    for pattern in (r"(SIC Code invalid\s*:?\s*\d+)",
                    r"(Select either permit OR application status\.?)",
                    r"(Please enter[^.]{0,90}\.)",
                    r"([A-Z][^.]{0,90} (?:is required|invalid)\.?)"):
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return "unspecified validation error"


def search_all(sic=None, county=None, delay=0.4, verbose=True, **kwargs):
    """Both halves of the record: issued authorizations and pending applications.

    The app makes these mutually exclusive, and the project asks for both — a
    site whose NOI is still in review is exactly the early signal this feed is
    here to give.
    """
    frames = []
    for label, status in (("authorizations", {"permit_status": "ALL"}),
                          ("applications", {"app_status": "ALL",
                                            "permit_status": None})):
        try:
            if verbose:
                print(f"  {label}:")
            frames.append(search(sic=sic, county=county, delay=delay,
                                 verbose=verbose, **dict(kwargs, **status)))
        except Exception as exc:
            print(f"  ERROR {label}: {str(exc)[:120]}")
    if not frames:
        return pd.DataFrame(columns=GRID_COLUMNS + ["detail_url"])
    return (pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["Auth #"]).reset_index(drop=True))


# Labels on the authorization summary page, in the order they appear. Used as
# each other's stop markers, since the page is a definition list flattened.
_DETAIL_LABELS = [
    "Permit Number:", "Authorization Status:", "Date Coverage Began:",
    "Date Coverage Ended:", "Replaced Permit Number:", "Site Name on Permit:",
    "Authorization Type:", "Primary SIC Code:", "Area Disturbed (in Acres)",
    "common plan of development", "impaired water body", "MS4 Operator",
    "receiving water body", "segment number", "Operator:", "Address:",
    "Annual Fee Billing Address:", "RN:", "RE Name:", "Site Location:",
    "County:", "TCEQ Region:", "Latitude:", "Longitude:",
    # Longitude is the last labelled field, so without a marker for the section
    # that follows it swallows the heading and everything after.
    "Regulated Entity Site Information", "Additional ID", "Back to",
]

_DETAIL_FIELDS = {
    "Permit Number:": "permit_number",
    "Authorization Status:": "status",
    "Date Coverage Began:": "start_date",
    "Date Coverage Ended:": "end_date",
    "Site Name on Permit:": "site_name",
    "Primary SIC Code:": "sic_code",
    "Area Disturbed (in Acres)": "acres",
    "Operator:": "operator",
    "RN:": "regulated_entity",
    "RE Name:": "re_name",
    "Site Location:": "address",
    "County:": "county",
    "Latitude:": "latitude",
    "Longitude:": "longitude",
}


def parse_detail(html):
    """Pull acreage, coordinates and the RN out of one summary page."""
    text = _text(re.sub(r"<script.*?</script>", "", html, flags=re.S))
    record = {}
    for label, key in _DETAIL_FIELDS.items():
        start = text.find(label)
        if start < 0:
            continue
        start += len(label)
        end = len(text)
        for other in _DETAIL_LABELS:
            if other == label:
                continue
            position = text.find(other, start)
            if 0 <= position < end:
                end = position
        record[key] = clean_str(text[start:end].strip(" :"))
    # "CN603448994 - ROSENDIN ELECTRIC INC" carries the customer number.
    operator = record.get("operator") or ""
    match = re.match(r"(CN\d+)\s*-\s*(.+)", operator)
    if match:
        record["customer_number"], record["operator"] = match.group(1), match.group(2)
    return record


def fetch_detail(detail_url, timeout=90, jar=None):
    """Fetch and parse one authorization summary page.

    `jar` must be the session that produced `detail_url`; the ids in the link
    are session-scoped and a stale one serves the search form instead.
    """
    if not detail_url:
        return {}
    url = detail_url if detail_url.startswith("http") else (
        SUMMARY_BASE + detail_url.lstrip("/").replace("wq_dpa/", "", 1)
    )
    html = net.get(url, timeout=timeout, jar=jar)
    if "Summary of Authorization" not in html:
        # The session expired or was never established; a silently wrong record
        # is worse than none, and parse_detail would happily return the form.
        raise ValueError("detail page not returned (session expired?)")
    return parse_detail(html)


def _load_checkpoint(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path) as handle:
            return json.load(handle)
    except (ValueError, OSError):
        return {}


def _save_checkpoint(path, cache):
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w") as handle:
        json.dump(cache, handle, indent=0, sort_keys=True)


def enrich(df, delay=0.4, limit=None, verbose=True, timeout=90, jar=None,
           checkpoint=None, flush_every=25):
    """Add acreage, coordinates, dates and the RN to each grid row.

    One request per authorization, so it is opt-in — but it is what turns a
    construction notice into something joinable: the RN is TCEQ's own site
    identifier, shared with the air permit record, and the coordinates are real
    rather than a county centroid.
    """
    rows = df.to_dict("records")
    if limit:
        rows = rows[:limit]

    # A statewide sweep is thousands of round trips over hours, and this
    # container is reclaimed on idle. Writing only at the end meant one restart
    # threw away 3,425 completed detail fetches, so results are cached by
    # authorization number as they arrive and a re-run skips what it already
    # has. The grid search is cheap and is simply redone — its detail links are
    # session-scoped and could not be reused across a restart anyway.
    cache = _load_checkpoint(checkpoint)
    if verbose and cache:
        print(f"    resuming: {len(cache)} details already fetched")

    out, fetched = [], 0
    for index, row in enumerate(rows, 1):
        key = str(row.get("Auth #") or "")
        cached = cache.get(key)
        if cached is not None:
            out.append(dict(row, **cached))
            continue
        try:
            detail = fetch_detail(row.get("detail_url"), timeout=timeout, jar=jar)
        except Exception as exc:
            detail = {"detail_error": str(exc)[:120]}
        # A failed fetch is not cached: a proxy restart or an expired session is
        # transient, and freezing it in would make the gap permanent.
        if key and "detail_error" not in detail:
            cache[key] = detail
        out.append(dict(row, **detail))
        fetched += 1
        if verbose and fetched % 25 == 0:
            print(f"    {index}/{len(rows)} details ({fetched} new)")
        if checkpoint and fetched % flush_every == 0:
            _save_checkpoint(checkpoint, cache)
        time.sleep(delay)

    _save_checkpoint(checkpoint, cache)
    return pd.DataFrame(out)


def collect_names(terms=None, details=True, detail_limit=None, delay=0.4,
                  verbose=True, checkpoint=None, **kwargs):
    """Sweep by site name, keeping only real word-boundary matches.

    Returns a DataFrame with a `matched_term` column recording which term found
    each row, so a result can be traced back to why it is here.
    """
    terms = list(terms or (NAME_TERMS_GENERATION + NAME_TERMS_LOAD))
    frames = []
    for term in terms:
        try:
            if verbose:
                print(f"  '{term}':")
            found = collect(site_name=term, details=details,
                            detail_limit=detail_limit, delay=delay,
                            verbose=verbose, checkpoint=checkpoint, **kwargs)
        except Exception as exc:
            print(f"  ERROR '{term}': {str(exc)[:120]}")
            continue
        if found.empty:
            continue
        keep = found[found["Site Name"].apply(lambda n: matches_term(n, term))].copy()
        if verbose:
            print(f"    {len(keep)} of {len(found)} are word-boundary matches")
        keep["matched_term"] = term
        frames.append(keep)
    if not frames:
        return pd.DataFrame(columns=GRID_COLUMNS + ["detail_url", "matched_term"])
    return (pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["Auth #"]).reset_index(drop=True))


def collect(sic=None, county=None, details=True, detail_limit=None,
            delay=0.4, verbose=True, checkpoint=None, **kwargs):
    """Search and enrich in one pass. Returns a DataFrame ready for ingest.

    This is the entry point worth using. Two things it gets right that are easy
    to get wrong: it keeps the cookie jar the detail links depend on, and it
    enriches each half of the record *before* running the next search — a second
    search replaces the session's held result set, which silently invalidates
    every detail link the first one handed back.
    """
    halves = (("authorizations", {"permit_status": "ALL"}),
              ("applications", {"app_status": "ALL", "permit_status": None}))
    frames = []
    for label, status in halves:
        jar = net.new_jar()
        try:
            if verbose:
                print(f"  {label}:")
            found = search(sic=sic, county=county, delay=delay, verbose=verbose,
                           jar=jar, **dict(kwargs, **status))
        except Exception as exc:
            print(f"  ERROR {label}: {str(exc)[:140]}")
            continue
        if found.empty:
            continue
        if details:
            if verbose:
                print(f"    details for {len(found)} authorizations...")
            found = enrich(found, delay=delay, limit=detail_limit,
                           verbose=verbose, jar=jar, checkpoint=checkpoint)
        frames.append(found)

    if not frames:
        return pd.DataFrame(columns=GRID_COLUMNS + ["detail_url"])
    return (pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["Auth #"]).reset_index(drop=True))


def search_url(county=None, sic=None):
    """The form URL, for a human who wants to run the query by hand."""
    params = {"fuseaction": "home.permit_info_search"}
    if county:
        params["cnty_name"] = county.upper()
    if sic:
        params["sic_code"] = sic[0] if isinstance(sic, (list, tuple)) else sic
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
            sic=base.get(row, resolved, "sic"),
            status=base.get(row, resolved, "status"),
            decision_date=base.get(row, resolved, "issued_date"),
            received_date=base.get(row, resolved, "start_date")
            or base.get(row, resolved, "issued_date"),
            construction_start=base.get(row, resolved, "issued_date"),
            construction_end=base.get(row, resolved, "end_date"),
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


def search_for_project(project_name, operator=None, county=None, limit=3,
                       delay=0.4, verbose=False, jar=None, **kwargs):
    """NOIs that plausibly belong to one ERCOT project.

    Driven by the project's name rather than its operator. ERCOT names the
    single-purpose entity that signed the interconnection agreement and TCEQ
    names whoever filed -- usually the parent or the contractor -- so an
    operator search found 2 of 10 queue projects. A token search finds 9.

    Recall is the easy half. The app matches substrings, so "Trent" returns
    TRENTON VIEW CENTER, "Pinta" returns PINTAILHOMES, and "Sampson" returns
    SAMPSON HOWARD ELEMENTARY SCHOOL. Two filters do the work, and neither
    needs a detail fetch because both fields are in the results grid:

      * `matches_term` re-checks the hit on word boundaries, which is what
        separates Trent from Trenton.
      * The county has to agree. A wind project in Glasscock and a school in
        Harris share nothing but a word.

    Returns a DataFrame with `matched_term` recording which token found each
    row, so any match can be traced back to why it is here.
    """
    tokens = normalize.project_tokens(project_name, operator, limit=limit)
    jar = jar or net.new_jar()
    frames = []
    for token in tokens:
        try:
            found = search(site_name=token, verbose=verbose, jar=jar,
                           delay=delay, **kwargs)
        except Exception as exc:
            if verbose:
                print(f"  ERROR '{token}': {str(exc)[:120]}")
            continue
        if found.empty:
            continue
        keep = found[found["Site Name"].apply(
            lambda name: matches_term(name, token))].copy()
        if county is not None:
            wanted = normalize.norm_county(county)
            keep = keep[keep["County"].apply(normalize.norm_county) == wanted]
        if keep.empty:
            continue
        keep["matched_term"] = token
        frames.append(keep)
    if not frames:
        return pd.DataFrame(columns=GRID_COLUMNS + ["detail_url", "matched_term"])
    return (pd.concat(frames, ignore_index=True)
            .drop_duplicates(subset=["Auth #"]).reset_index(drop=True))
