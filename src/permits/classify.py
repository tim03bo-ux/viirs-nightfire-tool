"""
classify.py — decide what a permit or queue record actually *is*.

Two independent questions, answered separately:

  1. project_kind   — generation / data_center / crypto_mining / industrial_load /
                      construction / unknown
  2. fuel + technology — for generation records, a canonical fuel class

Both return a confidence and the evidence that drove the call, so the dashboard
can show *why* something was tagged a data center rather than asking the user to
trust an opaque label.
"""

import re

from .normalize import norm_text, clean_str, parse_mw

# --- Project kinds -----------------------------------------------------------

KIND_GENERATION = "generation"
KIND_DATA_CENTER = "data_center"
KIND_CRYPTO = "crypto_mining"
KIND_INDUSTRIAL_LOAD = "industrial_load"
KIND_CONSTRUCTION = "construction"
KIND_UNKNOWN = "unknown"

# Kinds that count as "load" when deciding whether a site is colocated.
LOAD_KINDS = {KIND_DATA_CENTER, KIND_CRYPTO, KIND_INDUSTRIAL_LOAD}

DATA_CENTER_KEYWORDS = [
    "data center", "data centre", "datacenter", "data campus", "hyperscale",
    "colocation", "co location facility", "server farm", "compute campus",
    "computing campus", "ai campus", "ai factory", "digital campus",
    "digital infrastructure", "cloud campus", "high performance computing",
    "hpc facility", "internet data", "gpu cluster",
]

CRYPTO_KEYWORDS = [
    "bitcoin", "crypto", "cryptocurrency", "blockchain", "hashrate", "hash rate",
    "digital mining", "asic mining", "mining facility", "mining campus",
    "immersion mining", "digital asset",
]

GENERATION_KEYWORDS = [
    "generating station", "generation station", "generating facility",
    "power plant", "power station", "energy center", "energy centre",
    "peaking facility", "peaker", "combustion turbine", "gas turbine",
    "combined cycle", "simple cycle", "reciprocating engine", "recip engine",
    "cogeneration", "cogen", "solar farm", "solar project", "photovoltaic",
    "wind farm", "wind project", "battery energy storage", "energy storage",
    "bess", "electric generating", "electricity generation", "genset",
    "emergency generator", "backup generator", "standby generator",
]

# Load-side keywords that are neither DC nor crypto but still large industrial.
INDUSTRIAL_LOAD_KEYWORDS = [
    "hydrogen", "electrolyzer", "air separation", "steel mill", "smelter",
    "electric arc furnace", "lng", "liquefaction", "desalination",
    "direct air capture", "chlor alkali", "ammonia plant",
]

# NAICS / SIC codes that settle the question on their own when present.
NAICS_KIND = {
    "518210": KIND_DATA_CENTER,   # Computing infrastructure, data processing, hosting
    "541513": KIND_DATA_CENTER,   # Computer facilities management
    "221111": KIND_GENERATION, "221112": KIND_GENERATION, "221113": KIND_GENERATION,
    "221114": KIND_GENERATION, "221115": KIND_GENERATION, "221116": KIND_GENERATION,
    "221117": KIND_GENERATION, "221118": KIND_GENERATION, "221121": KIND_GENERATION,
    "221122": KIND_GENERATION,
    "518": KIND_DATA_CENTER, "2211": KIND_GENERATION,
}

SIC_KIND = {
    "4911": KIND_GENERATION, "4931": KIND_GENERATION, "4939": KIND_GENERATION,
    "7374": KIND_DATA_CENTER,
}

# Operators whose name alone is strong evidence of a data center development.
# Matched on the normalized company key, as a whole-token phrase.
# Multi-word only. Single generic tokens produce false positives on real filings:
# a bare "switch" matched FRAME SWITCH ENERGY INC (a Williamson County place
# name) and a bare "tract" matches land descriptions. If an operator needs one
# word to identify it, it is not distinctive enough to classify on.
DATA_CENTER_OPERATORS = [
    "aligned data centers", "aligned energy", "applied digital", "cloudhq",
    "compass datacenters", "core scientific", "corescientific", "crusoe energy",
    "cyrusone", "databank", "digital realty", "edgeconnex", "edged energy",
    "equinix", "flexential", "fermi america", "prime data centers",
    "provident data", "qts data centers", "quantum loophole", "sabey data",
    "skybox datacenters", "stack infrastructure", "switch inc", "switch ltd",
    "switch data", "t5 data centers", "vantage data centers", "yondr group",
    "nscale", "lancium", "poolside", "crusoe",
]

CRYPTO_OPERATORS = [
    "argo blockchain", "bitdeer", "bitfarms", "cipher mining", "cleanspark",
    "core scientific", "galaxy digital", "greenidge", "hut 8", "iren",
    "iris energy", "marathon digital", "mara holdings", "riot platforms",
    "riot blockchain", "rhodium enterprises", "terawulf", "whinstone",
]

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _contains_phrase(haystack_norm, phrase):
    """Whole-phrase containment on normalized text, avoiding substring accidents
    like 'mining' matching inside 'determining'."""
    phrase_norm = norm_text(phrase)
    if not phrase_norm or not haystack_norm:
        return False
    return f" {phrase_norm} " in f" {haystack_norm} "


def _any_phrase(haystack_norm, phrases):
    for phrase in phrases:
        if _contains_phrase(haystack_norm, phrase):
            return phrase
    return None


def classify_kind(
    name=None,
    operator=None,
    description=None,
    naics=None,
    sic=None,
    source=None,
    load_mw=None,
    capacity_mw=None,
    permit_type=None,
):
    """Return (kind, confidence, evidence_string).

    Confidence is a coarse 0-1 band, not a calibrated probability:
        0.95  structural — the source itself only carries this kind
        0.85  industry code match
        0.75  operator name match
        0.65  keyword match in project name / description
        0.30  weak inference from context (e.g. a big load with no other signal)
        0.10  unknown
    """
    haystack = " ".join(
        norm_text(part) for part in (name, operator, description, permit_type) if part
    ).strip()
    evidence = []

    # 1. Source is structural: an ERCOT GIS row is generation by construction.
    if source == "ercot_gis":
        return KIND_GENERATION, 0.95, "ERCOT generator interconnection queue entry"
    if source == "tceq_swnoi":
        # Stormwater NOIs describe earthwork; the *kind* comes from keywords, and
        # falls back to plain construction.
        pass

    # 2. Industry codes.
    for code_value, table, label in ((naics, NAICS_KIND, "NAICS"), (sic, SIC_KIND, "SIC")):
        code = re.sub(r"[^0-9]", "", str(code_value or ""))
        if not code:
            continue
        for length in (6, 4, 3):
            prefix = code[:length]
            if prefix in table:
                kind = table[prefix]
                return kind, 0.85, f"{label} {prefix}"

    # 3. Operator names.
    operator_norm = norm_text(operator)
    if operator_norm:
        hit = _any_phrase(operator_norm, CRYPTO_OPERATORS)
        if hit:
            return KIND_CRYPTO, 0.75, f"known crypto-mining operator '{hit}'"
        hit = _any_phrase(operator_norm, DATA_CENTER_OPERATORS)
        if hit:
            return KIND_DATA_CENTER, 0.75, f"known data-center operator '{hit}'"

    # 4. Keywords. Crypto is checked before data center because crypto sites are
    #    frequently described as data centers too, and the narrower label wins.
    hit = _any_phrase(haystack, CRYPTO_KEYWORDS)
    if hit:
        evidence.append(f"keyword '{hit}'")
        return KIND_CRYPTO, 0.65, "; ".join(evidence)

    hit = _any_phrase(haystack, DATA_CENTER_KEYWORDS)
    if hit:
        evidence.append(f"keyword '{hit}'")
        return KIND_DATA_CENTER, 0.65, "; ".join(evidence)

    hit = _any_phrase(haystack, GENERATION_KEYWORDS)
    if hit:
        evidence.append(f"keyword '{hit}'")
        return KIND_GENERATION, 0.65, "; ".join(evidence)

    hit = _any_phrase(haystack, INDUSTRIAL_LOAD_KEYWORDS)
    if hit:
        evidence.append(f"keyword '{hit}'")
        return KIND_INDUSTRIAL_LOAD, 0.65, "; ".join(evidence)

    # 5. Context fallbacks.
    if source == "ercot_large_load":
        mw = parse_mw(load_mw)
        if mw and mw >= 75:
            return KIND_DATA_CENTER, 0.30, (
                f"large-load queue entry ({mw:.0f} MW) with no descriptive keyword; "
                "data center is the modal large load but unconfirmed"
            )
        return KIND_INDUSTRIAL_LOAD, 0.30, "large-load queue entry, type not stated"

    if capacity_mw and parse_mw(capacity_mw):
        return KIND_GENERATION, 0.30, "record carries a generation capacity value"

    if source == "tceq_swnoi":
        return KIND_CONSTRUCTION, 0.40, "stormwater construction NOI, end use not stated"

    return KIND_UNKNOWN, 0.10, "no classifying signal"


# --- Fuel and technology -----------------------------------------------------

FUEL_SOLAR = "solar"
FUEL_WIND = "wind"
FUEL_STORAGE = "battery_storage"
FUEL_GAS = "natural_gas"
FUEL_COAL = "coal"
FUEL_NUCLEAR = "nuclear"
FUEL_HYDRO = "hydro"
FUEL_BIOMASS = "biomass"
FUEL_GEOTHERMAL = "geothermal"
FUEL_PETROLEUM = "petroleum"
FUEL_HYDROGEN = "hydrogen"
FUEL_OTHER = "other"

FUEL_ORDER = [
    FUEL_GAS, FUEL_SOLAR, FUEL_WIND, FUEL_STORAGE, FUEL_NUCLEAR, FUEL_COAL,
    FUEL_PETROLEUM, FUEL_HYDRO, FUEL_BIOMASS, FUEL_GEOTHERMAL, FUEL_HYDROGEN,
    FUEL_OTHER,
]

# ERCOT GIS fuel codes as they appear in the monthly workbook.
# Verified against the July 2026 GIS report: the codes actually present are
# OTH, SOL, WIN, GAS, OIL, HYD, NUC and WAT.
ERCOT_FUEL_CODES = {
    "sol": FUEL_SOLAR, "win": FUEL_WIND, "wat": FUEL_HYDRO, "gas": FUEL_GAS,
    "coa": FUEL_COAL, "lig": FUEL_COAL, "nuc": FUEL_NUCLEAR, "pet": FUEL_PETROLEUM,
    "oil": FUEL_PETROLEUM, "bio": FUEL_BIOMASS, "geo": FUEL_GEOTHERMAL,
    "oth": FUEL_OTHER, "wds": FUEL_BIOMASS, "hyd": FUEL_HYDRO,
}

# ERCOT's Technology column holds two-letter codes, NOT prose. Half the queue is
# fuel OTH + technology BA (battery), so without this map 877 of 1,797 July 2026
# projects would classify as fuel "other" with no technology at all.
# (code -> (canonical technology, fuel implied when the fuel code is
#  absent or the catch-all OTH))
ERCOT_TECH_CODES = {
    "ba": ("battery", FUEL_STORAGE),
    "pv": ("photovoltaic", FUEL_SOLAR),
    "wt": ("wind_turbine", FUEL_WIND),
    "gt": ("combustion_turbine", None),
    "cc": ("combined_cycle", None),
    "ic": ("reciprocating_engine", None),
    "st": ("steam_turbine", None),
    "ot": (None, None),
}

# Ordered longest/most-specific first — 'combined cycle' must beat bare 'gas'.
_FUEL_PHRASES = [
    (FUEL_STORAGE, ["battery energy storage", "battery storage", "energy storage",
                    "bess", "lithium ion", "storage system"]),
    (FUEL_SOLAR, ["photovoltaic solar", "photovoltaic", "solar pv", "solar energy",
                  "solar farm", "solar project", "solar"]),
    (FUEL_WIND, ["wind turbine", "wind farm", "wind project", "wind energy", "wind"]),
    (FUEL_NUCLEAR, ["small modular reactor", "nuclear reactor", "nuclear"]),
    (FUEL_COAL, ["pulverized coal", "lignite", "coal"]),
    (FUEL_HYDROGEN, ["hydrogen fuel", "hydrogen turbine", "hydrogen"]),
    (FUEL_GAS, ["combined cycle gas turbine", "combined cycle", "simple cycle",
                "natural gas", "gas turbine", "reciprocating engine",
                "reciprocating internal combustion", "gas engine", "methane",
                "landfill gas", "flare gas", "gas"]),
    (FUEL_PETROLEUM, ["diesel", "fuel oil", "distillate", "petroleum", "kerosene"]),
    (FUEL_BIOMASS, ["biomass", "wood waste", "biogas", "digester"]),
    (FUEL_GEOTHERMAL, ["geothermal"]),
    (FUEL_HYDRO, ["hydroelectric", "hydro"]),
]

# Prime-mover / technology detail, reported alongside fuel.
_TECH_PHRASES = [
    ("combined_cycle", ["combined cycle", "ccgt", "duct burner"]),
    ("combustion_turbine", ["combustion turbine", "gas turbine", "simple cycle",
                            "peaking turbine", "aeroderivative"]),
    ("reciprocating_engine", ["reciprocating engine", "reciprocating internal combustion",
                              "gas engine", "genset", "engine generator"]),
    ("steam_turbine", ["steam turbine", "boiler"]),
    ("photovoltaic", ["photovoltaic", "solar pv"]),
    ("wind_turbine", ["wind turbine"]),
    ("battery", ["battery", "bess", "energy storage"]),
    ("fuel_cell", ["fuel cell"]),
    ("reactor", ["reactor", "smr"]),
]


def classify_fuel(fuel_code=None, technology=None, name=None, description=None):
    """Return (fuel, technology, confidence).

    fuel_code and technology are the ERCOT short codes when the source supplies
    them; the free-text fields are searched otherwise.
    """
    code = norm_text(fuel_code).replace(" ", "")
    if code[:3] in ERCOT_FUEL_CODES:
        fuel = ERCOT_FUEL_CODES[code[:3]]
        confidence = 0.95
    else:
        fuel = None
        confidence = 0.0

    # A two-letter ERCOT technology code settles both fields at once, and is the
    # only thing that distinguishes a battery from any other "OTH" fuel row.
    tech_code = norm_text(technology).replace(" ", "")
    if tech_code in ERCOT_TECH_CODES:
        coded_tech, implied_fuel = ERCOT_TECH_CODES[tech_code]
        if implied_fuel and fuel in (None, FUEL_OTHER):
            fuel, confidence = implied_fuel, 0.95
        if coded_tech:
            return fuel, coded_tech, (confidence or 0.9)

    haystack = " ".join(
        norm_text(part) for part in (technology, name, description, fuel_code) if part
    ).strip()

    text_fuel = None
    for candidate, phrases in _FUEL_PHRASES:
        if _any_phrase(haystack, phrases):
            text_fuel = candidate
            break

    # ERCOT files a battery as fuel 'OTH' + technology 'Other - Battery Energy
    # Storage', so text detail refines an 'other' code rather than losing to it.
    if fuel is None:
        fuel = text_fuel
        confidence = 0.70 if text_fuel else 0.0
    elif fuel == FUEL_OTHER and text_fuel:
        fuel = text_fuel
        confidence = 0.75

    tech = None
    for candidate, phrases in _TECH_PHRASES:
        if _any_phrase(haystack, phrases):
            tech = candidate
            break

    if fuel is None:
        return None, tech, 0.0
    return fuel, tech, confidence


def is_dispatchable(fuel):
    """Whether a fuel can serve load on demand — the property that matters when
    judging whether colocated generation could actually back a data center."""
    return fuel in {
        FUEL_GAS, FUEL_COAL, FUEL_NUCLEAR, FUEL_PETROLEUM, FUEL_BIOMASS,
        FUEL_GEOTHERMAL, FUEL_HYDROGEN, FUEL_STORAGE,
    }


def summarize_kind(kind):
    """Human label for a kind code."""
    return {
        KIND_GENERATION: "Generation",
        KIND_DATA_CENTER: "Data center",
        KIND_CRYPTO: "Crypto mining",
        KIND_INDUSTRIAL_LOAD: "Industrial load",
        KIND_CONSTRUCTION: "Construction (end use unknown)",
        KIND_UNKNOWN: "Unclassified",
    }.get(kind, clean_str(kind) or "Unclassified")


def summarize_fuel(fuel):
    """Human label for a fuel code."""
    return {
        FUEL_GAS: "Natural gas", FUEL_SOLAR: "Solar", FUEL_WIND: "Wind",
        FUEL_STORAGE: "Battery storage", FUEL_COAL: "Coal", FUEL_NUCLEAR: "Nuclear",
        FUEL_HYDRO: "Hydro", FUEL_BIOMASS: "Biomass", FUEL_GEOTHERMAL: "Geothermal",
        FUEL_PETROLEUM: "Petroleum", FUEL_HYDROGEN: "Hydrogen", FUEL_OTHER: "Other",
    }.get(fuel, clean_str(fuel) or "Unknown")


# --- Engine family -----------------------------------------------------------
#
# "Recip or turbine" is the question the permit data is actually asked, and it
# is not answerable from the manufacturer alone. Several makers build both:
# General Electric sells the LM6000 aeroderivative *and* owned Jenbacher gas
# engines; Rolls-Royce sells aero turbines and Bergen reciprocating sets. So the
# model is consulted first and the maker is only a fallback.
#
# Wärtsilä is treated as reciprocating: its Texas plants — Pecos, Odessa — are
# banks of large gas engines, which is what makes them look like turbines by
# size while behaving like recips.

ENGINE_RECIP = "reciprocating"
ENGINE_TURBINE = "turbine"
ENGINE_UNKNOWN = "unknown"

# Model prefixes, checked before the manufacturer.
_TURBINE_MODELS = [
    "lm6000", "lm2500", "lms100", "sgt", "sst", "7fa", "7ha", "9ha", "6b",
    "501j", "501g", "501f", "m501", "ft4000", "ft8", "titan", "taurus",
    "centaur", "mars ", "solar t", "c65", "c200", "c1000", "gt ",
]
_RECIP_MODELS = [
    "g3520", "g3516", "g3606", "g3608", "g3612", "g3616", "3516", "3512",
    "qsk", "qsv", "qsx", "kta", "vhp", "l7042", "j920", "j620", "j420",
    "gg20", "20v4000", "16v4000", "12v4000", "w20v34", "w18v50", "w31",
]

_TURBINE_MAKERS = [
    "siemens", "mitsubishi", "solar turbines", "pratt & whitney", "kawasaki",
    "capstone", "ge vernova",
]
_RECIP_MAKERS = [
    "caterpillar", "cummins", "jenbacher", "innio", "waukesha", "guascor",
    "mtu", "generac", "doosan", "scania", "perkins", "kohler", "deutz",
    "yanmar", "volvo penta", "clarke", "detroit diesel", "baldor", "wartsila",
    "wärtsilä",
]


def classify_engine(manufacturer=None, model=None):
    """Return "reciprocating", "turbine" or "unknown" for one unit.

    The model decides where it is recognisable, because a maker's name does not:
    a GE LM6000 and a GE-era Jenbacher J620 are different machines entirely.
    """
    text = norm_text(model)
    if text:
        for prefix in _RECIP_MODELS:
            if prefix in text:
                return ENGINE_RECIP
        for prefix in _TURBINE_MODELS:
            if prefix in text:
                return ENGINE_TURBINE

    maker = norm_text(manufacturer)
    if maker:
        for name in _RECIP_MAKERS:
            if name in maker:
                return ENGINE_RECIP
        for name in _TURBINE_MAKERS:
            if name in maker:
                return ENGINE_TURBINE
    return ENGINE_UNKNOWN


def classify_engines(manufacturers, models=None):
    """Engine families present in a comma-separated maker/model pair of lists.

    A site can hold both — a peaker with black-start engines beside its
    turbines — so this returns a set rather than picking a winner.
    """
    makers = [part.strip() for part in str(manufacturers or "").split(",") if part.strip()]
    model_list = [part.strip() for part in str(models or "").split(",") if part.strip()]
    families = set()
    for model in model_list:
        family = classify_engine(model=model)
        if family != ENGINE_UNKNOWN:
            families.add(family)
    for maker in makers:
        families.add(classify_engine(manufacturer=maker))
    families.discard(ENGINE_UNKNOWN)
    return families


# Spellings that reach the extractor as distinct strings but name one company.
# Without this the manufacturer picker offers "wartsila" and "wärtsilä" as two
# separate makers and splits that company's sites across both.
_MAKER_ALIASES = {
    "wärtsilä": "wartsila", "warstila": "wartsila",
    "ge vernova": "general electric", "ge": "general electric",
    "innio": "jenbacher", "man energy": "man energy solutions",
    "rolls royce": "rolls-royce",
}


def canonical_maker(name):
    """One spelling per manufacturer, for grouping and for filter menus."""
    text = norm_text(name)
    if not text:
        return None
    return _MAKER_ALIASES.get(text, text)
