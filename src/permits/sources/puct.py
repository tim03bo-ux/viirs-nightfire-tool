"""
puct.py — Public Utility Commission of Texas filings (Interchange).

The third leg of a Texas project's paperwork. TCEQ authorises what it emits,
ERCOT queues its interconnection, and the PUCT is where the utility-side
proceedings live: certificates of convenience and necessity for the transmission
that reaches a site, economic-development rate riders written for a single large
customer, and — since SB 6 — the large-load interconnection rulemakings that
govern how data centers connect.

Why this matters here specifically: ERCOT publishes its large-load queue in
aggregate, by zone, with no customer named. That is the one thing the colocation
question actually needs. PUCT dockets name them. A rate-rider application says
which utility is building for which customer, and a docket's filing list names
every party that showed up — EdgeConneX, Cholla, TCPA and the rest are on the
record in the SB 6 docket even though no ERCOT report will ever name them.

Two endpoints, both plain GET:

    /search/search/    docket search  -> Control | Filings | Utility | Case Style
    /search/filings/   one docket     -> Item | File Stamp | Party | Type | Description

The search form has two text fields that are easy to confuse. `Description`
matches the *case style* (the docket's title) and is the useful one;
`FilingDescription` matches the text of individual filings inside a docket.

A control number is a docket, not a plant, so what this yields is
proceeding-level context: who is party to what, when it was filed, and — for
transmission and CCN matters — which county the line runs through, which is
enough to place it on the map at county precision.
"""

import re

import pandas as pd

from . import base
from .. import net
from ..classify import KIND_DATA_CENTER, KIND_UNKNOWN
from ..normalize import clean_str, norm_text, parse_date

SOURCE = "puct"

SEARCH_URL = "https://interchange.puc.texas.gov/search/search/"
FILINGS_URL = "https://interchange.puc.texas.gov/search/filings/"
DOCKET_URL = FILINGS_URL + "?ControlNumber={}"

# The form posts single-letter codes, not the labels it displays. Sending
# "Electric" is accepted and silently matches nothing, so names are mapped here.
UTILITY_TYPES = {
    "all": "A", "electric": "E", "water": "W", "telephone": "T", "others": "O",
}

# Document types are codes too, right-padded to four characters in the markup.
DOCUMENT_TYPES = {
    "all": "ALL", "comments": "COM ", "public comments": "PC  ",
    "pleadings": "PL  ", "project": "PRJ ", "testimony": "TEST",
    "tariff": "TARF", "letters": "LTRS", "briefs": "BR  ",
    "registrations": "REG ", "exhibits": "EX  ", "miscellaneous": "MISC",
}


def _utility_code(value):
    if not value:
        return None
    text = str(value).strip()
    return UTILITY_TYPES.get(text.lower(), text)


def _document_code(value):
    if not value:
        return None
    text = str(value).strip()
    return DOCUMENT_TYPES.get(text.lower(), text)

# Case-style phrases, checked in this order — the first hit wins, so the
# narrowest categories are listed before the broadest.
_RELEVANCE_PHRASES = [
    ("data_center", [
        "data center", "data centre", "datacenter", "hyperscale",
        "cryptocurrency", "virtual currency", "bitcoin", "digital asset mining",
    ]),
    ("large_load", [
        "large load", "large customer", "load interconnection", "37.0561",
        "senate bill 6", "sb 6", "economic development rate",
    ]),
    ("generation", [
        "generating", "generation", "power plant", "energy storage",
        "solar", "wind farm", "combined cycle", "cogeneration",
        "electric generating facility",
    ]),
    ("transmission", [
        "transmission line", "kv transmission", "certificate of convenience",
        "ccn", "switching station", "substation",
    ]),
]

# "... IN ELLIS COUNTY", and the surprisingly common unspaced "MONTGOMERYCOUNTY".
_COUNTY_RE = re.compile(
    r"\bIN\s+([A-Z][A-Z .'-]{2,30}?)\s*COUNT(?:Y|IES)\b", re.I
)
_VOLTAGE_RE = re.compile(r"\b(\d{2,3})\s*-?\s*KV\b", re.I)

# "APPLICATION OF X TO AMEND...", "PETITION OF X FOR...", "JOINT APPLICATION OF
# X AND Y FOR...". The Utility column holds whoever the Commission indexed the
# docket under, which for a joint filing or a compliance docket is not the
# applicant — the case style is.
_APPLICANT_RE = re.compile(
    r"\b(?:JOINT\s+)?(?:APPLICATION|PETITION|REQUEST|NOTICE|COMPLAINT)\s+OF\s+"
    r"(.+?)(?=\s+(?:TO|FOR|AND\s+THE\s+CITY|SEEKING|REGARDING|PURSUANT|UNDER)\b|,\s*$|$)",
    re.I,
)

# "...FOR THE OLD COUNTRY SWITCH 345-KV TAP TRANSMISSION LINE IN ELLIS COUNTY"
_FACILITY_RE = re.compile(
    r"\bFOR\s+(?:THE\s+)?(.+?)\s+(?:\d{2,3}\s*-?\s*KV\s+)?"
    r"(?:TRANSMISSION\s+LINE|SWITCHING\s+STATION|SUBSTATION|GENERATING\s+"
    r"(?:STATION|FACILITY|PLANT)|POWER\s+PLANT|ENERGY\s+CENTER)\b",
    re.I,
)

# Dockets that belong to the Commission's own docket machinery rather than to a
# project: rulemakings, statewide projects, compliance filings.
_GENERIC_FILERS = ("puc ", "commission staff", "state office of administrative")
_GENERIC_STYLES = (
    "rulemaking", "project no", "compliance docket", "report ",
    "data collection", "generic", "investigation of", "inquiry",
)

_TAG_RE = re.compile(r"<[^>]+>")
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)

_ENTITIES = {
    "&amp;": "&", "&nbsp;": " ", "&#8217;": "'", "&#8216;": "'",
    "&#8220;": '"', "&#8221;": '"', "&quot;": '"', "&#39;": "'",
    "&sect;": "S", "&#167;": "S", "&lt;": "<", "&gt;": ">",
}


def _text(html):
    text = _TAG_RE.sub("", html)
    for code, char in _ENTITIES.items():
        text = text.replace(code, char)
    return re.sub(r"\s+", " ", text).strip()


def _cells(row_html):
    return [_text(cell) for cell in _CELL_RE.findall(row_html)]


def _rows(html, min_cells, first_is_number=True):
    out = []
    for row in _ROW_RE.findall(html):
        cells = _cells(row)
        if len(cells) < min_cells:
            continue
        # Header rows lead with a label ("Control", "Item"); data rows lead with
        # a docket or item number.
        if first_is_number and not cells[0].isdigit():
            continue
        out.append(cells)
    return out


def parse_results(html):
    """Parse the Interchange docket-search table into a DataFrame."""
    records = [
        {"control_number": c[0], "filings": c[1], "utility": c[2], "case_style": c[3]}
        for c in _rows(html, 4)
    ]
    return pd.DataFrame(
        records,
        columns=["control_number", "filings", "utility", "case_style"],
    )


def parse_docket(html):
    """Parse one docket's filing list into a DataFrame."""
    records = [
        {"item": c[0], "file_stamp": c[1], "party": c[2],
         "item_type": c[3], "filing_description": c[4]}
        for c in _rows(html, 5)
    ]
    return pd.DataFrame(
        records,
        columns=["item", "file_stamp", "party", "item_type", "filing_description"],
    )


def search(case_style=None, filing_description=None, control_number=None,
           utility_name=None, filing_party=None, date_from=None, date_to=None,
           utility_type="Electric", document_type=None, timeout=120):
    """Run an Interchange docket search. Dates are MM/DD/YYYY.

    `case_style` matches the docket title (the Interchange field is confusingly
    named `Description`); `filing_description` matches text inside individual
    filings. `utility_type` defaults to Electric because the same CCN machinery
    serves water and sewer utilities, which outnumber the electric dockets.
    """
    fields = {}
    if control_number:
        fields["ControlNumber"] = str(control_number)
    if case_style:
        fields["Description"] = case_style
    if filing_description:
        fields["FilingDescription"] = filing_description
    if utility_name:
        fields["UtilityName"] = utility_name
    if filing_party:
        fields["FilingParty"] = filing_party
    if date_from:
        fields["DateFiledFrom"] = date_from
    if date_to:
        fields["DateFiledTo"] = date_to
    code = _utility_code(utility_type)
    if code and code != "A":
        fields["UtilityType"] = code
    if document_type:
        fields["DocumentType"] = _document_code(document_type)
    if not fields:
        raise ValueError("give at least one search criterion")

    return parse_results(net.get(SEARCH_URL, params=fields, timeout=timeout))


def fetch_docket(control_number, timeout=120):
    """Fetch one docket's filing list. Returns a DataFrame, empty when unknown."""
    html = net.get(FILINGS_URL, params={"ControlNumber": str(control_number)},
                   timeout=timeout)
    return parse_docket(html)


def classify_docket(case_style):
    """Coarse relevance label for a docket, from its case style."""
    text = norm_text(case_style)
    if not text:
        return "other"
    for label, phrases in _RELEVANCE_PHRASES:
        if any(norm_text(phrase) in text for phrase in phrases):
            return label
    return "other"


def extract_county(case_style):
    """The county a transmission or CCN docket names, if it names one."""
    match = _COUNTY_RE.search(case_style or "")
    if not match:
        return None
    county = clean_str(match.group(1))
    if not county or len(county) < 3:
        return None
    # "IN THE CITY OF X COUNTY" style false positives carry stop words.
    if norm_text(county) in {"the", "and", "part", "portion"}:
        return None
    return county.title()


def extract_voltage_kv(case_style):
    match = _VOLTAGE_RE.search(case_style or "")
    return int(match.group(1)) if match else None


def extract_applicant(case_style):
    """The company the case style says filed, which is who to match against.

    For a generation docket this is the project company itself — "APPLICATION OF
    BRAES BAYOU GENERATING, LLC" — which is the same legal entity ERCOT lists as
    the interconnecting entity and TCEQ lists as the permit holder. That is the
    join.
    """
    match = _APPLICANT_RE.search(case_style or "")
    if not match:
        return None
    applicant = clean_str(re.sub(r"\s+", " ", match.group(1)))
    if not applicant or len(applicant) < 4:
        return None
    # A joint application names several; the first is the lead filer.
    applicant = re.split(r"\s+AND\s+(?=[A-Z])", applicant, maxsplit=1)[0]
    return clean_str(applicant.rstrip(" ,."))


def extract_facility(case_style):
    """The named line, station or plant a docket is about, if it names one."""
    match = _FACILITY_RE.search(case_style or "")
    if not match:
        return None
    facility = clean_str(re.sub(r"\s+", " ", match.group(1)))
    if not facility or len(facility) < 4:
        return None
    # "...TO AMEND ITS CERTIFICATE OF CONVENIENCE AND NECESSITY FOR THE X LINE"
    # can capture the boilerplate when the name sits before it.
    if norm_text(facility).startswith(("certificate", "a certificate", "its cert")):
        return None
    return facility


def is_generic_proceeding(utility, case_style):
    """True for rulemakings and Commission-run projects, which have no site."""
    filer = norm_text(utility)
    style = norm_text(case_style)
    if any(filer.startswith(prefix.strip()) for prefix in _GENERIC_FILERS):
        return True
    return any(style.startswith(marker.strip()) for marker in _GENERIC_STYLES)


def docket_dates(filings):
    """(first, last) file-stamp dates from a docket's filing list."""
    if filings is None or len(filings) == 0:
        return None, None
    stamps = sorted(
        stamp for stamp in (parse_date(x) for x in filings["file_stamp"]) if stamp
    )
    return (stamps[0], stamps[-1]) if stamps else (None, None)


def docket_parties(filings, exclude_staff=True):
    """Distinct filing parties in a docket, most active first.

    Commission staff and the docket-management offices file in everything, so
    they are dropped by default — what is wanted is who *came to* the docket.
    """
    if filings is None or len(filings) == 0:
        return []
    counts = {}
    for party in filings["party"]:
        name = clean_str(party)
        if not name:
            continue
        if exclude_staff and norm_text(name).startswith(("puc", "commission staff")):
            continue
        counts[name] = counts.get(name, 0) + 1
    return [name for name, _ in sorted(counts.items(), key=lambda kv: -kv[1])]


def to_entities(df, source_file_id=None, relevant_only=True):
    """Normalize docket rows into entity dicts.

    A docket is a proceeding, not a plant: there is no capacity and no address.
    County comes from the case style when the case style names one — that is
    real, stated geography, so it maps at county precision; everything else maps
    nowhere rather than somewhere invented.

    Rows carrying `first_filed` / `parties` (added by `enrich_dockets`) get the
    docket's opening date and its participant list, which is what makes a docket
    matchable to a developer by name.
    """
    records = []
    for _, row in df.iterrows():
        control = clean_str(row.get("control_number"))
        if not control:
            continue
        case_style = clean_str(row.get("case_style"))
        relevance = classify_docket(case_style)
        if relevant_only and relevance == "other":
            continue

        parties = row.get("parties")
        if isinstance(parties, str):
            parties = [p.strip() for p in parties.split(";") if p.strip()]
        parties = list(parties) if isinstance(parties, (list, tuple)) else []

        # The case style is the docket's own words; the party list is who showed
        # up. Both feed classification, and a data-center operator is usually
        # named only in the second.
        description = " | ".join(filter(None, [case_style, "; ".join(parties)]))

        voltage = extract_voltage_kv(case_style)
        utility = clean_str(row.get("utility"))
        applicant = extract_applicant(case_style)
        facility = extract_facility(case_style)
        generic = is_generic_proceeding(utility, case_style)

        payload = {k: v for k, v in row.to_dict().items() if k != "parties"}
        payload["relevance"] = relevance
        payload["generic_proceeding"] = generic
        if voltage:
            payload["voltage_kv"] = voltage
        if applicant:
            payload["applicant"] = applicant
        if facility:
            payload["facility"] = facility
        if parties:
            payload["parties"] = parties

        permit_type = "PUCT rulemaking / generic proceeding" if generic else (
            "PUCT " + {
                "transmission": "CCN/transmission docket",
                "data_center": "data-center docket",
                "large_load": "large-load docket",
                "generation": "generation docket",
            }.get(relevance, "docket")
        )

        records.append(
            base.build_entity(
                source=SOURCE,
                source_key=control,
                raw_row=payload,
                # The named facility identifies the site; the whole case style
                # is a sentence and matches nothing. Falling back to the case
                # style keeps the record readable when nothing was named.
                project_name=facility or applicant or case_style
                or f"PUCT docket {control}",
                # The applicant, not the Utility column: on a joint or
                # compliance filing the Commission indexes the docket under a
                # party that is not the one building anything. The applicant is
                # the same legal entity ERCOT lists as interconnecting entity
                # and TCEQ lists as permit holder, which is what makes a docket
                # joinable to the rest of the record at all.
                operator=applicant or utility,
                county=extract_county(case_style),
                description=description,
                permit_type=permit_type,
                permit_number=control,
                received_date=row.get("first_filed"),
                status_date=row.get("last_filed"),
                url=DOCKET_URL.format(control),
                source_file_id=source_file_id,
                # A transmission or rate proceeding is paperwork *about* a
                # project, never generation itself; left to the keyword
                # classifier, every "...FOR THE X SOLAR TAP LINE" would become a
                # plant. A docket the Commission itself titles for a data center
                # is the one case where the case style does assert what it is
                # about, and it is the case ERCOT's aggregate queue cannot give
                # us. What kind of docket this is stays in `permit_type`.
                kind_override=(KIND_DATA_CENTER if relevance == "data_center"
                               else KIND_UNKNOWN),
            )
        )
    return records


def enrich_dockets(df, delay=0.5, limit=None, verbose=True, timeout=120):
    """Add first/last filing dates and the party list to each docket row.

    One HTTP round trip per docket, so this is opt-in: the search result alone
    is enough to know a docket exists, but the parties are the reason to care.
    """
    import time

    rows = df.to_dict("records")
    if limit:
        rows = rows[:limit]
    out = []
    for index, row in enumerate(rows, 1):
        control = clean_str(row.get("control_number"))
        try:
            filings = fetch_docket(control, timeout=timeout)
            first, last = docket_dates(filings)
            row = dict(row, first_filed=first, last_filed=last,
                       parties="; ".join(docket_parties(filings)[:25]),
                       n_filings=len(filings))
        except Exception as exc:  # one unreachable docket must not lose the run
            row = dict(row, error=str(exc)[:120])
        out.append(row)
        if verbose and index % 25 == 0:
            print(f"    {index}/{len(rows)} dockets")
        time.sleep(delay)
    return pd.DataFrame(out)


def read_file(path, sheet=None):
    """Read a saved Interchange results page or an exported csv."""
    lower = str(path).lower()
    if lower.endswith((".html", ".htm")):
        with open(path, encoding="utf-8", errors="replace") as handle:
            return parse_results(handle.read())
    return base.read_table(
        path, sheet=sheet, expected_tokens=["control", "utility", "case_style"]
    )
