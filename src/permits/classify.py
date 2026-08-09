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
DATA_CENTER_OPERATORS = [
    "aligned data centers", "aligned energy", "applied digital", "cloudhq",
    "compass datacenters", "corescientific", "core scientific", "crusoe",
    "cyrusone", "dataBank", "digital realty", "edgeconnex", "edged energy",
    "equinix", "flexential", "fermi", "prime data centers", "provident data",
    "qts", "quantum loophole", "sabey", "skybox datacenters", "stack infrastructure",
    "switch", "t5 data centers", "tract", "vantage data centers", "yondr",
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
