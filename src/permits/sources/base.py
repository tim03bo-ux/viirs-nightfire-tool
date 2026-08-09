"""
base.py — shared machinery for source adapters.

The upstream files are not stable. ERCOT renames GIS columns between monthly
releases ("Capacity (MW)" vs "Capacity (MW)*" vs "Summer Capacity MW"), and TCEQ
exports differ by query form. Rather than hard-coding exact headers, each adapter
declares a *column map* of canonical field -> list of candidate header patterns,
and `resolve_columns` picks the best available match. A rename upstream then
degrades to a missing optional field instead of a crash.
"""

import re

import pandas as pd

from ..normalize import (
    clean_str, county_centroid, norm_company, norm_county, norm_project,
    parse_date, parse_mw, parse_number,
)
from ..classify import classify_kind, classify_fuel
from ..status import classify_lifecycle, classify_program, classify_action
from ..db import ENTITY_COLUMNS, make_entity_id, to_json


def _header_key(header):
    """Loose header key: lowercase alphanumerics only."""
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def resolve_columns(df, column_map, required=()):
    """Map canonical field names to actual DataFrame columns.

    column_map: {field: [candidate header, ...]} where candidates are matched
    first exactly (on the loose key), then as a substring of the loose key. The
    first candidate that hits wins, so list the most specific first.

    Raises KeyError if a `required` field cannot be resolved.
    """
    keys = {_header_key(column): column for column in df.columns}
    resolved = {}
    for field, candidates in column_map.items():
        match = None
        for candidate in candidates:
            candidate_key = _header_key(candidate)
            if candidate_key in keys:
                match = keys[candidate_key]
                break
        if match is None:
            for candidate in candidates:
                candidate_key = _header_key(candidate)
                if not candidate_key:
                    continue
                hits = [col for key, col in keys.items() if candidate_key in key]
                if hits:
                    match = hits[0]
                    break
        if match is not None:
            resolved[field] = match

    missing = [field for field in required if field not in resolved]
    if missing:
        raise KeyError(
            f"could not resolve required column(s) {missing}; "
            f"available headers: {list(df.columns)[:40]}"
        )
    return resolved


def get(row, resolved, field, default=None):
    """Read a canonical field from a row using a resolved column map."""
    column = resolved.get(field)
    if column is None:
        return default
    value = row.get(column, default)
    if isinstance(value, float) and pd.isna(value):
        return default
    return value


def find_header_row(df, expected_tokens, max_scan=12):
    """Locate the real header row in a sheet with title/banner rows above it.

    ERCOT and TCEQ workbooks routinely carry a title block before the table. The
    row whose cells match the most expected tokens wins; returns its index or
    None when the first row already looks like the header.
    """
    tokens = {_header_key(token) for token in expected_tokens}
    best_index, best_hits = None, 0
    for index in range(min(max_scan, len(df))):
        cells = {_header_key(cell) for cell in df.iloc[index].tolist() if pd.notna(cell)}
        hits = sum(
            1 for token in tokens
            if any(token and token in cell for cell in cells)
        )
        if hits > best_hits:
            best_index, best_hits = index, hits
    if best_hits >= 2:
        return best_index
    return None


def read_table(path, sheet=None, expected_tokens=()):
    """Read a csv/xlsx/xls into a DataFrame, skipping banner rows when present."""
    lower = str(path).lower()
    if lower.endswith((".csv", ".txt")):
        df = pd.read_csv(path, dtype=object, on_bad_lines="skip")
    elif lower.endswith((".xlsx", ".xlsm", ".xls")):
        df = pd.read_excel(path, sheet_name=sheet if sheet is not None else 0,
                           dtype=object, header=None)
        if expected_tokens is not None:
            header_index = find_header_row(df, expected_tokens)
            if header_index is not None:
                df.columns = [clean_str(c) or f"col_{i}"
                              for i, c in enumerate(df.iloc[header_index].tolist())]
                df = df.iloc[header_index + 1:].reset_index(drop=True)
            else:
                df.columns = [clean_str(c) or f"col_{i}"
                              for i, c in enumerate(df.iloc[0].tolist())]
                df = df.iloc[1:].reset_index(drop=True)
    elif lower.endswith((".json",)):
        df = pd.read_json(path, dtype=object)
    else:
        raise ValueError(f"unsupported file type: {path}")

    df = df.dropna(axis=0, how="all").dropna(axis=1, how="all")
    return df


def pick_sheet(path, preferred_names):
    """Return the first sheet name matching any preferred substring, else None."""
    try:
        book = pd.ExcelFile(path)
    except (ValueError, OSError):
        return None
    keys = {_header_key(name): name for name in book.sheet_names}
    for preferred in preferred_names:
        preferred_key = _header_key(preferred)
        for key, name in keys.items():
            if preferred_key and preferred_key in key:
                return name
    return None


def build_entity(
    source,
    source_key,
    raw_row=None,
    project_name=None,
    operator=None,
    county=None,
    address=None,
    latitude=None,
    longitude=None,
    description=None,
    fuel_code=None,
    technology=None,
    capacity_mw=None,
    load_mw=None,
    acres=None,
    status=None,
    status_date=None,
    received_date=None,
    decision_date=None,
    projected_cod=None,
    permit_type=None,
    permit_number=None,
    regulated_entity=None,
    customer_number=None,
    naics=None,
    sic=None,
    nox_tpy=None,
    co_tpy=None,
    voc_tpy=None,
    pm_tpy=None,
    so2_tpy=None,
    ghg_tpy=None,
    url=None,
    source_file_id=None,
    state="TX",
    kind_override=None,
    lifecycle_override=None,
):
    """Assemble one normalized entity row: classify, geocode-fallback, normalize.

    This is the single place where classification and county-centroid fallback
    are applied, so every source gets identical treatment.
    """
    if source_key is None or str(source_key).strip() == "":
        raise ValueError(f"{source}: source_key is required")
    source_key = str(source_key).strip()

    project_name = clean_str(project_name)
    operator = clean_str(operator)
    county = clean_str(county)

    latitude = parse_number(latitude)
    longitude = parse_number(longitude)
    # Texas sits in the western hemisphere; a positive longitude is a sign-drop.
    if longitude is not None and longitude > 0 and 93 <= longitude <= 107:
        longitude = -longitude
    if latitude is not None and not (25.0 <= latitude <= 37.0):
        latitude = None
    if longitude is not None and not (-107.0 <= longitude <= -93.0):
        longitude = None

    if latitude is not None and longitude is not None:
        geo_precision = "site"
    else:
        centroid_lat, centroid_lon = county_centroid(county)
        if centroid_lat is not None:
            latitude, longitude = centroid_lat, centroid_lon
            geo_precision = "county"
        else:
            latitude = longitude = None
            geo_precision = "none"

    capacity_mw = parse_mw(capacity_mw)
    load_mw = parse_mw(load_mw)
    acres = parse_number(acres)

    if kind_override:
        kind, kind_confidence, kind_evidence = kind_override, 0.95, "set by adapter"
    else:
        kind, kind_confidence, kind_evidence = classify_kind(
            name=project_name,
            operator=operator,
            description=description,
            naics=naics,
            sic=sic,
            source=source,
            load_mw=load_mw,
            capacity_mw=capacity_mw,
            permit_type=permit_type,
        )

    fuel, tech, fuel_confidence = classify_fuel(
        fuel_code=fuel_code,
        technology=technology,
        name=project_name,
        description=description,
    )

    received_date = parse_date(received_date)
    decision_date = parse_date(decision_date)
    program = classify_program(
        permit_type=permit_type, permit_number=permit_number, source=source
    )
    if lifecycle_override:
        lifecycle, stage = lifecycle_override, None
    else:
        lifecycle, stage = classify_lifecycle(
            status=status, program=program,
            decision_date=decision_date, received_date=received_date,
            source=source,
        )
    action = classify_action(permit_type=permit_type, description=description)

    # status_date is whichever date the status refers to: the decision if one
    # has been made, otherwise the filing.
    status_date = parse_date(status_date) or decision_date or received_date

    record = {
        "entity_id": make_entity_id(source, source_key),
        "source": source,
        "source_key": source_key,
        "project_name": project_name,
        "name_norm": norm_project(project_name),
        "operator": operator,
        "operator_norm": norm_company(operator),
        "county": county,
        "county_norm": norm_county(county),
        "state": state,
        "address": clean_str(address),
        "latitude": latitude,
        "longitude": longitude,
        "geo_precision": geo_precision,
        "project_kind": kind,
        "kind_confidence": kind_confidence,
        "kind_evidence": kind_evidence,
        "fuel": fuel,
        "technology": tech or clean_str(technology),
        "fuel_confidence": fuel_confidence,
        "capacity_mw": capacity_mw,
        "load_mw": load_mw,
        "acres": acres,
        "status": clean_str(status),
        "status_date": status_date,
        "lifecycle": lifecycle,
        "stage": stage,
        "received_date": received_date,
        "decision_date": decision_date,
        "projected_cod": parse_date(projected_cod),
        "permit_type": clean_str(permit_type),
        "permit_program": program,
        "permit_action": action,
        "permit_number": clean_str(permit_number),
        "regulated_entity": clean_str(regulated_entity),
        "customer_number": clean_str(customer_number),
        "nox_tpy": parse_number(nox_tpy),
        "co_tpy": parse_number(co_tpy),
        "voc_tpy": parse_number(voc_tpy),
        "pm_tpy": parse_number(pm_tpy),
        "so2_tpy": parse_number(so2_tpy),
        "ghg_tpy": parse_number(ghg_tpy),
        "url": clean_str(url),
        "first_seen": None,
        "last_seen": None,
        "source_file_id": source_file_id,
        "raw_json": to_json(raw_row) if raw_row is not None else None,
    }

    unknown = set(record) - set(ENTITY_COLUMNS)
    if unknown:
        raise AssertionError(f"build_entity produced unknown columns: {unknown}")
    return record
