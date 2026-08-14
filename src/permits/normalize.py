"""
normalize.py — text, name, geography and unit normalization shared by all sources.

Every source adapter emits raw strings that differ in spelling, casing, corporate
suffixes and units. Matching across TCEQ and ERCOT only works if those are
collapsed to a canonical form first, so all of that lives here rather than being
re-invented per adapter.
"""

import json
import math
import os
import re
import unicodedata
from datetime import datetime, date

import pandas as pd

# Corporate suffixes stripped before company-name comparison. Order matters:
# longer forms first so "L L C" does not leave a stray "L".
_COMPANY_SUFFIXES = [
    "limited liability company", "limited partnership", "incorporated",
    "corporation", "company", "holdings", "holding", "partners", "partnership",
    "l l c", "l p", "llc", "lllp", "llp", "lp", "ltd", "inc", "corp", "co",
    "plc", "gp", "pllc", "trust", "usa", "us", "na",
]

# Noise words in project names that carry no matching signal.
_PROJECT_NOISE = [
    "project", "facility", "site", "phase", "unit", "units", "the",
]

_ROMAN = {
    " i": " 1", " ii": " 2", " iii": " 3", " iv": " 4", " v": " 5",
    " vi": " 6", " vii": " 7", " viii": " 8", " ix": " 9", " x": " 10",
}

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def norm_text(value):
    """Lowercase, strip accents/punctuation, collapse whitespace. '' for null."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value)
    if text.strip().lower() in ("nan", "none", "null", "n/a", "na", "-", "--"):
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = text.replace("&", " and ")
    text = _NON_ALNUM_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def norm_company(value):
    """Canonical company key: normalized text minus corporate suffixes.

    'Redbud Digital Holdings, LLC' and 'REDBUD DIGITAL HOLDINGS L.L.C.' both
    collapse to 'redbud digital'.
    """
    text = norm_text(value)
    if not text:
        return ""
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
            elif text == suffix:
                return ""
    return text.strip()


def norm_project(value):
    """Canonical project key: normalized text minus filler words, roman numerals
    folded to digits so 'Llano Mesa Solar II' matches 'Llano Mesa Solar 2'."""
    text = norm_text(value)
    if not text:
        return ""
    padded = " " + text + " "
    for roman, digit in _ROMAN.items():
        padded = padded.replace(roman + " ", digit + " ")
    tokens = [t for t in padded.split() if t not in _PROJECT_NOISE]
    return " ".join(tokens)


def name_similarity(a, b):
    """Token-set similarity in [0, 1]. Uses rapidfuzz when available."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    try:
        from rapidfuzz import fuzz

        return fuzz.token_set_ratio(a, b) / 100.0
    except ImportError:
        import difflib

        set_a, set_b = set(a.split()), set(b.split())
        if set_a and set_b:
            jaccard = len(set_a & set_b) / len(set_a | set_b)
        else:
            jaccard = 0.0
        ratio = difflib.SequenceMatcher(None, a, b).ratio()
        return max(jaccard, ratio)


# --- Geography ---------------------------------------------------------------

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km. Returns None if any coordinate is missing."""
    for v in (lat1, lon1, lat2, lon2):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


_COUNTY_SUFFIX_RE = re.compile(r"\s+(county|co)$")


def norm_county(value):
    """'EL PASO COUNTY' -> 'el paso'. Multi-county strings keep only the first.

    The split happens on the raw string: norm_text drops punctuation, so by the
    time it has run "Ward, Winkler" is indistinguishable from a two-word county.
    """
    if value is None:
        return ""
    # ERCOT lists multi-county POIs as "Ward, Winkler" or "Ward/Winkler".
    first = re.split(r"[,/;&]|\band\b", str(value), maxsplit=1)[0]
    text = norm_text(first)
    if not text:
        return ""
    return _COUNTY_SUFFIX_RE.sub("", text).strip()


# Approximate geographic centers of Texas counties with meaningful generation or
# large-load activity. Used ONLY as a map fallback when a record has no site
# coordinates; anything placed this way is tagged geo_precision='county' so it is
# never mistaken for a surveyed location.
TX_COUNTY_CENTROIDS = {
    "anderson": (31.81, -95.65), "andrews": (32.30, -102.64), "angelina": (31.25, -94.61),
    "atascosa": (28.89, -98.53), "bastrop": (30.10, -97.31), "bee": (28.42, -97.74),
    "bell": (31.04, -97.48), "bexar": (29.45, -98.52), "borden": (32.74, -101.43),
    "bosque": (31.90, -97.63), "bowie": (33.44, -94.42), "brazoria": (29.17, -95.43),
    "brazos": (30.66, -96.30), "brewster": (29.80, -103.25), "brown": (31.77, -98.99),
    "burleson": (30.49, -96.62), "caldwell": (29.84, -97.62), "calhoun": (28.44, -96.59),
    "callahan": (32.30, -99.37), "cameron": (26.15, -97.51), "carson": (35.40, -101.35),
    "cass": (33.08, -94.34), "castro": (34.53, -102.26), "chambers": (29.71, -94.68),
    "cherokee": (31.84, -95.16), "childress": (34.53, -100.20), "coke": (31.89, -100.53),
    "coleman": (31.77, -99.45), "collin": (33.19, -96.57), "colorado": (29.62, -96.53),
    "comal": (29.81, -98.28), "concho": (31.32, -99.86), "cooke": (33.64, -97.21),
    "coryell": (31.39, -97.80), "cottle": (34.08, -100.28), "crane": (31.43, -102.52),
    "crockett": (30.72, -101.41), "culberson": (31.45, -104.52), "dallam": (36.28, -102.60),
    "dallas": (32.77, -96.78), "dawson": (32.74, -101.95), "deaf smith": (34.97, -102.60),
    "delta": (33.39, -95.67), "denton": (33.20, -97.12), "dewitt": (29.08, -97.35),
    "dickens": (33.62, -100.78), "dimmit": (28.42, -99.76), "duval": (27.68, -98.51),
    "eastland": (32.33, -98.83), "ector": (31.87, -102.54), "el paso": (31.77, -106.24),
    "ellis": (32.35, -96.79), "erath": (32.24, -98.22), "falls": (31.25, -96.94),
    "fannin": (33.59, -96.11), "fayette": (29.88, -96.92), "fisher": (32.74, -100.40),
    "floyd": (34.07, -101.30), "fort bend": (29.53, -95.77), "franklin": (33.18, -95.22),
    "freestone": (31.71, -96.15), "frio": (28.87, -99.11), "galveston": (29.39, -94.89),
    "garza": (33.18, -101.30), "gillespie": (30.32, -98.95), "glasscock": (31.87, -101.52),
    "goliad": (28.66, -97.42), "gonzales": (29.46, -97.49), "gray": (35.40, -100.81),
    "grayson": (33.63, -96.68), "gregg": (32.48, -94.82), "grimes": (30.54, -95.99),
    "guadalupe": (29.58, -97.95), "hale": (34.07, -101.83), "hansford": (36.28, -101.35),
    "hardeman": (34.29, -99.75), "hardin": (30.33, -94.39), "harris": (29.86, -95.39),
    "harrison": (32.55, -94.37), "hartley": (35.84, -102.60), "haskell": (33.18, -99.73),
    "hays": (30.06, -98.03), "hemphill": (35.84, -100.27), "henderson": (32.21, -95.85),
    "hidalgo": (26.40, -98.18), "hill": (31.99, -97.13), "hockley": (33.61, -102.34),
    "hood": (32.43, -97.83), "hopkins": (33.15, -95.56), "howard": (32.31, -101.44),
    "hunt": (33.12, -96.09), "hutchinson": (35.84, -101.35), "jack": (33.23, -98.17),
    "jackson": (28.95, -96.58), "jasper": (30.74, -94.03), "jefferson": (29.86, -94.15),
    "jim hogg": (27.04, -98.70), "jim wells": (27.73, -98.09), "johnson": (32.38, -97.37),
    "jones": (32.74, -99.88), "karnes": (28.90, -97.86), "kaufman": (32.60, -96.29),
    "kenedy": (26.93, -97.65), "kent": (33.18, -100.78), "kerr": (30.06, -99.35),
    "kimble": (30.49, -99.75), "king": (33.62, -100.25), "kleberg": (27.43, -97.72),
    "knox": (33.61, -99.74), "lamar": (33.67, -95.57), "lamb": (34.07, -102.35),
    "lampasas": (31.19, -98.24), "lasalle": (28.35, -99.10), "lavaca": (29.38, -96.93),
    "lee": (30.31, -96.97), "leon": (31.30, -95.99), "liberty": (30.15, -94.81),
    "limestone": (31.55, -96.58), "live oak": (28.35, -98.12), "llano": (30.71, -98.68),
    "loving": (31.85, -103.58), "lubbock": (33.61, -101.82), "lynn": (33.18, -101.82),
    "martin": (32.31, -101.95), "matagorda": (28.83, -96.00), "maverick": (28.74, -100.32),
    "mcculloch": (31.20, -99.35), "mclennan": (31.55, -97.20), "mcmullen": (28.35, -98.57),
    "medina": (29.36, -99.11), "menard": (30.89, -99.82), "midland": (31.87, -102.03),
    "milam": (30.79, -96.98), "mills": (31.50, -98.59), "mitchell": (32.31, -100.92),
    "montague": (33.67, -97.72), "montgomery": (30.30, -95.50), "moore": (35.84, -101.89),
    "morris": (33.11, -94.73), "motley": (34.07, -100.78), "nacogdoches": (31.62, -94.62),
    "navarro": (32.05, -96.47), "newton": (30.79, -93.75), "nolan": (32.30, -100.41),
    "nueces": (27.73, -97.52), "ochiltree": (36.28, -100.81), "oldham": (35.40, -102.60),
    "orange": (30.12, -93.89), "palo pinto": (32.75, -98.31), "panola": (32.16, -94.31),
    "parker": (32.78, -97.80), "parmer": (34.53, -102.78), "pecos": (30.78, -102.72),
    "polk": (30.79, -94.83), "potter": (35.40, -101.89), "presidio": (29.98, -104.24),
    "rains": (32.87, -95.79), "randall": (34.97, -101.90), "reagan": (31.37, -101.52),
    "real": (29.83, -99.82), "red river": (33.62, -95.05), "reeves": (31.32, -103.69),
    "refugio": (28.32, -97.16), "roberts": (35.84, -100.81), "robertson": (31.03, -96.51),
    "rockwall": (32.90, -96.41), "runnels": (31.83, -99.98), "rusk": (32.11, -94.76),
    "sabine": (31.35, -93.85), "san patricio": (28.01, -97.52), "san saba": (31.14, -98.82),
    "schleicher": (30.90, -100.54), "scurry": (32.74, -100.92), "shackelford": (32.74, -99.35),
    "shelby": (31.79, -94.15), "sherman": (36.28, -101.89), "smith": (32.37, -95.27),
    "somervell": (32.22, -97.77), "starr": (26.56, -98.74), "stephens": (32.74, -98.83),
    "sterling": (31.83, -101.05), "stonewall": (33.18, -100.25), "sutton": (30.50, -100.54),
    "swisher": (34.53, -101.74), "tarrant": (32.77, -97.29), "taylor": (32.30, -99.89),
    "terry": (33.17, -102.33), "throckmorton": (33.18, -99.21), "titus": (33.22, -94.97),
    "tom green": (31.40, -100.46), "travis": (30.33, -97.78), "trinity": (31.09, -95.14),
    "tyler": (30.77, -94.38), "upshur": (32.74, -94.94), "upton": (31.37, -102.04),
    "uvalde": (29.36, -99.76), "val verde": (29.89, -101.15), "van zandt": (32.56, -95.84),
    "victoria": (28.80, -96.97), "walker": (30.74, -95.57), "waller": (30.01, -95.99),
    "ward": (31.51, -103.10), "washington": (30.21, -96.40), "webb": (27.76, -99.33),
    "wharton": (29.28, -96.22), "wheeler": (35.40, -100.27), "wichita": (33.99, -98.70),
    "wilbarger": (34.08, -99.23), "willacy": (26.48, -97.73), "williamson": (30.65, -97.60),
    "wilson": (29.17, -98.09), "winkler": (31.85, -103.05), "wise": (33.22, -97.65),
    "wood": (32.79, -95.38), "yoakum": (33.17, -102.83), "young": (33.18, -98.69),
    "zapata": (26.99, -99.17), "zavala": (28.87, -99.76),
}


# ZIP centroids from the Census 2023 ZCTA gazetteer, clipped to the Texas
# bounding box. TCEQ publishes no coordinates at all — only a county, a nearest
# city, a ZIP and driving directions — so a ZIP centroid is the best real
# geography available for a permit, and it is roughly an order of magnitude
# tighter than a county centroid.
_ZIP_CENTROIDS = None


def zip_centroid(zip_code):
    """Approximate (lat, lon) for a Texas ZIP, or (None, None)."""
    global _ZIP_CENTROIDS
    if _ZIP_CENTROIDS is None:
        path = os.path.join(os.path.dirname(__file__), "data",
                            "tx_zip_centroids.json")
        try:
            with open(path) as handle:
                _ZIP_CENTROIDS = json.load(handle)
        except (OSError, ValueError):
            _ZIP_CENTROIDS = {}
    digits = re.sub(r"[^0-9]", "", str(zip_code or ""))[:5]
    if len(digits) != 5:
        return (None, None)
    found = _ZIP_CENTROIDS.get(digits)
    return tuple(found) if found else (None, None)


def county_centroid(county):
    """Approximate (lat, lon) for a Texas county name, or (None, None)."""
    key = norm_county(county)
    if key in TX_COUNTY_CENTROIDS:
        return TX_COUNTY_CENTROIDS[key]
    return (None, None)


# --- Scalars -----------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def parse_number(value):
    """Pull the first number out of a messy cell ('~250 MW' -> 250.0). None if absent."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    match = _NUM_RE.search(str(value).replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_mw(value):
    """Capacity in MW, converting a kW-labelled cell down. None if unparseable."""
    number = parse_number(value)
    if number is None:
        return None
    text = str(value).lower()
    if "kw" in text and "mw" not in text:
        return number / 1000.0
    if "gw" in text:
        return number * 1000.0
    return number


def parse_date(value):
    """Best-effort date -> 'YYYY-MM-DD' string. None if unparseable.

    Kept as ISO text rather than datetime so values round-trip through SQLite
    unchanged and sort correctly as strings.
    """
    if value is None or value == "":
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text or text.lower() in ("nan", "nat", "none", "tbd", "n/a"):
        return None
    try:
        parsed = pd.to_datetime(text, errors="coerce")
    except (ValueError, TypeError):
        return None
    if parsed is None or pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def clean_str(value, max_len=None):
    """Trim a display string, preserving original casing. None for empty."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = _WS_RE.sub(" ", str(value)).strip()
    if not text or text.lower() in ("nan", "none", "null", "n/a"):
        return None
    if max_len:
        text = text[:max_len]
    return text


# Words that appear across the ERCOT queue and so identify nothing. Measured by
# counting every word in 1,827 project names: "solar", "storage" and "bess" lead
# the list, and "TEF"/"due diligence" is an ERCOT process annotation rather than
# part of any project's name.
_GENERIC_PROJECT_WORDS = frozenset({
    "solar", "storage", "bess", "battery", "energy", "wind", "windpower",
    "gas", "power", "project", "renewable", "renewables", "generation",
    "generating", "farm", "plant", "station", "center", "centre", "facility",
    "phase", "repower", "expansion", "addition", "unit", "units", "grid",
    "slf", "brp", "tef", "due", "diligence", "flexible", "hybrid",
    "north", "south", "east", "west", "texas", "tx", "county",
    "the", "and", "of", "llc", "lp", "inc", "ltd", "corp", "company",
    "i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
    "new", "old", "big", "little",
})

# Two letters is not a searchable name; four is. "Elk" and "Zeus" are real
# project names, so the floor sits at three.
_MIN_TOKEN = 3


def project_tokens(*names, limit=3):
    """Distinctive words from a project name, most identifying first.

    ERCOT and TCEQ almost never agree on an operator -- ERCOT lists the
    single-purpose entity that signed the interconnection agreement, TCEQ lists
    whoever filed, which is usually the parent or the contractor. Searching
    TCEQ by ERCOT's operator found one match in seven.

    What the two do share is the project's own name, and it survives in odd
    places: "Harald (BearKat Wind B)" pairs ERCOT's internal codename with the
    real one, and the parenthetical is the half that matches "BEARKAT WIND".
    So parentheses are opened rather than stripped, and every word is a
    candidate until the generic ones are removed.

    Longer words are tried first: they are rarer, so they narrow a site-name
    search faster than a short one does.
    """
    seen, tokens = set(), []
    for name in names:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9'-]*", str(name or "")):
            lowered = word.lower()
            if (len(lowered) < _MIN_TOKEN or lowered in _GENERIC_PROJECT_WORDS
                    or lowered in seen):
                continue
            seen.add(lowered)
            tokens.append(word)
    tokens.sort(key=len, reverse=True)
    return tokens[:limit]
