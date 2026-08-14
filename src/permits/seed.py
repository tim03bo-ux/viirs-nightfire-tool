"""
seed.py — synthetic demo dataset.

SYNTHETIC DATA. Every project, company, permit number and coordinate below is
invented. It exists so the pipeline, linker and dashboard can be exercised and
tested without network access to ERCOT or TCEQ. Do not read anything here as a
real filing, and do not mix these files into a database holding real records —
`--demo` writes to its own directory and the dashboard labels the result.

The generated files use the real upstream column headers, so they flow through
the same adapters as genuine exports; that is the point, it keeps the demo path
and the production path identical.
"""

import os

import pandas as pd

DEMO_BANNER = "SYNTHETIC DEMO DATA — invented records, not real TCEQ/ERCOT filings"

# One entry per invented development. Records are emitted into whichever feeds
# the development is marked as appearing in, which is what gives the linker
# cross-source clusters to resolve.
#
# fields: key, display name, operator, county, lat, lon
DEVELOPMENTS = [
    # (key, name_stem, operator, county, lat, lon)
    ("llano_mesa", "Llano Mesa", "Llano Mesa Renewables LLC", "Pecos", 30.9214, -102.8871),
    ("brazos_ridge", "Brazos Ridge", "Brazos Ridge Development LLC", "Milam", 30.8012, -96.9944),
    ("redbud", "Redbud Flats", "Redbud Digital LLC", "Hood", 32.4471, -97.8188),
    ("caprock", "Caprock Compute", "Caprock Compute Partners LP", "Lubbock", 33.5701, -101.7742),
    ("sabine_point", "Sabine Point Energy Center", "Sabine Point Power LLC", "Jefferson", 29.9012, -94.0771),
    ("verde_fork", "Verde Fork Wind", "Verde Fork Wind Holdings LLC", "Nolan", 32.3311, -100.3812),
    ("twin_buttes", "Twin Buttes Storage", "Twin Buttes Storage LLC", "Tom Green", 31.3712, -100.4901),
    ("panhandle_nexus", "Panhandle Nexus", "Panhandle Nexus Infrastructure LLC", "Potter", 35.3812, -101.8412),
    ("san_saba", "San Saba Solar", "San Saba Solar Project LLC", "San Saba", 31.1701, -98.8012),
    ("deer_hollow", "Deer Hollow", "Deer Hollow Digital Holdings LLC", "Ellis", 32.3312, -96.7712),
    ("concho_peak", "Concho Peak", "Concho Peak Generation LLC", "Concho", 31.3401, -99.8412),
    ("gulf_terrace", "Gulf Terrace", "Gulf Terrace Cogeneration LP", "Brazoria", 29.1912, -95.4412),
    ("whitethorn", "Whitethorn Ranch", "Whitethorn Land Company LLC", "Ward", 31.5312, -103.1201),
    ("bluff_creek", "Bluff Creek Hydrogen", "Bluff Creek Hydrogen LLC", "Calhoun", 28.4612, -96.6012),
    ("ocotillo", "Ocotillo Flats Solar", "Ocotillo Flats Solar LLC", "Reeves", 31.3401, -103.7012),
    ("marlin_bend", "Marlin Bend", "Marlin Bend Data Works LLC", "Falls", 31.2712, -96.9212),
    ("stonewall_gap", "Stonewall Gap Solar", "Stonewall Gap Solar LLC", "Stonewall", 33.1912, -100.2612),
    ("kiowa_draw", "Kiowa Draw", "Kiowa Draw Mining Company LLC", "Childress", 34.5412, -100.2212),
]

DEV_BY_KEY = {row[0]: row for row in DEVELOPMENTS}


def _dev(key):
    _, name, operator, county, lat, lon = DEV_BY_KEY[key]
    return name, operator, county, lat, lon


# --- ERCOT GIS ---------------------------------------------------------------
# (dev_key, inr, name_suffix, fuel, technology, mw, cod, phase, ia_signed)
_GIS_ROWS = [
    ("llano_mesa", "24INR0101", "Solar", "SOL", "Photovoltaic Solar", 250, "2027-06-01", "SS Complete", "2025-11-14"),
    ("llano_mesa", "24INR0102", "BESS", "OTH", "Other - Battery Energy Storage", 150, "2027-06-01", "SS Complete", "2025-11-14"),
    ("brazos_ridge", "24INR0210", "Energy Center", "GAS", "Gas Turbine or Engine", 300, "2028-05-01", "FIS Approved", "2026-02-02"),
    ("sabine_point", "23INR0044", "Unit 1", "GAS", "Combined-Cycle Gas Turbine with Duct Burner", 1120, "2029-01-15", "IA Signed", "2025-08-19"),
    ("verde_fork", "22INR0311", "", "WIN", "Wind Turbine", 201, "2026-12-01", "IA Signed", "2024-09-30"),
    ("twin_buttes", "24INR0455", "", "OTH", "Other - Battery Energy Storage", 200, "2027-03-01", "SS Complete", None),
    ("panhandle_nexus", "25INR0512", "Power Block", "GAS", "Reciprocating Engine", 264, "2028-09-01", "FIS Requested", None),
    ("san_saba", "24INR0620", "", "SOL", "Photovoltaic Solar", 180, "2027-09-01", "SS Complete", None),
    ("concho_peak", "24INR0701", "Peaking Facility", "GAS", "Gas Turbine or Engine", 186, "2027-11-01", "IA Signed", "2025-12-11"),
    ("gulf_terrace", "23INR0808", "Cogeneration", "GAS", "Combined-Cycle Gas Turbine with Duct Burner", 320, "2028-02-01", "FIS Approved", "2026-01-20"),
    ("ocotillo", "25INR0910", "", "SOL", "Photovoltaic Solar", 300, "2028-06-01", "Screening Study Started", None),
    ("stonewall_gap", "25INR0977", "", "SOL", "Photovoltaic Solar", 145, "2028-04-01", "Screening Study Started", None),
    ("marlin_bend", "25INR1020", "Generation", "GAS", "Reciprocating Engine", 220, "2029-03-01", "Screening Study Started", None),
    ("kiowa_draw", "24INR1101", "Wind Repower", "WIN", "Wind Turbine", 98, "2027-05-01", "SS Complete", None),
]

# --- ERCOT large load --------------------------------------------------------
# (dev_key, request_id, name_suffix, load_mw, load_type, status, energization, colocated)
_LARGE_LOAD_ROWS = [
    ("brazos_ridge", "LIR-2025-0031", "Digital Campus", 400, "Data center", "Approved for energization", "2028-06-01", "Yes"),
    ("redbud", "LIR-2024-0088", "Mining Facility", 120, "Cryptocurrency mining", "Energized", "2025-04-01", "No"),
    ("caprock", "LIR-2025-0104", "Data Center", 250, "Data center", "Study in progress", "2028-01-01", "No"),
    ("panhandle_nexus", "LIR-2025-0140", "Data Center Campus", 500, "Data center", "Study in progress", "2029-01-01", "Yes"),
    ("deer_hollow", "LIR-2025-0166", "AI Campus", 300, "Data center", "Screening", "2029-06-01", "No"),
    ("bluff_creek", "LIR-2024-0177", "Electrolyzer Facility", 90, "Hydrogen electrolyzer", "Study in progress", "2028-03-01", "No"),
    ("marlin_bend", "LIR-2025-0188", "Data Works Campus", 350, "Data center", "Screening", "2029-09-01", "Yes"),
    ("kiowa_draw", "LIR-2024-0199", "Digital Mining Site", 75, "Cryptocurrency mining", "Energized", "2025-08-01", "No"),
]

# --- TCEQ air permits and applications ----------------------------------------
# Deliberately spans every air authorization family and both lifecycle states:
# issued permits and applications still sitting in review.
# (dev_key, project_no, permit_no, rn, cn, name_suffix, permit_type, status,
#  received, issued, description, naics, nox_tpy, lat_offset, lon_offset)
_AIR_ROWS = [
    ("brazos_ridge", "PROJ-318842", "182204", "RN112900431", "CN605511902", "Energy Center",
     "New Source Review - Air Quality Permit", "Pending - Technical Review", "2026-01-12", None,
     "Construction of 8 x 37.5 MW natural gas simple cycle combustion turbines serving a "
     "colocated data center campus, with SCR and oxidation catalyst", "221112", 84.2, 0.006, -0.004),
    ("brazos_ridge", "PROJ-331902", "O-4488", "RN112900431", "CN605511902", "Energy Center",
     "Federal Operating Permit - Title V - Initial", "Pending - Public Notice", "2026-05-04", None,
     "Initial site operating permit application for the generating facility", "221112",
     None, 0.006, -0.004),
    ("sabine_point", "PROJ-301220", "179115", "RN110044822", "CN604221730", "",
     "New Source Review - PSD", "Issued", "2024-06-11", "2025-03-04",
     "1,120 MW combined cycle gas turbine generating station with duct burners and auxiliary boiler",
     "221112", 212.5, -0.003, 0.005),
    ("sabine_point", "PROJ-327740", "179115A", "RN110044822", "CN604221730", "",
     "New Source Review - Amendment", "Pending - Technical Review", "2026-04-02", None,
     "Amendment to add a 45 MW auxiliary combustion turbine", "221112", 9.8, -0.003, 0.005),
    ("caprock", "PROJ-322104", "PBR-166051", "RN113408877", "CN606012244", "Data Center",
     "Permit by Rule - 30 TAC 106.512", "Registered", "2026-01-30", "2026-02-20",
     "Forty-eight 3.0 MW diesel emergency standby generators for a data center campus, "
     "each limited to 100 hours per year of non-emergency operation", "518210", 46.4, 0.004, 0.004),
    ("concho_peak", "PROJ-315507", "181002", "RN112455018", "CN605880114", "Peaking Facility",
     "New Source Review - Air Quality Permit", "Issued", "2025-02-14", "2025-10-08",
     "Two 93 MW natural gas combustion turbines in simple cycle peaking service", "221112",
     31.7, 0.002, -0.006),
    ("gulf_terrace", "PROJ-309981", "180334", "RN111788203", "CN605120988", "Cogeneration Plant",
     "New Source Review - PSD", "Pending - Public Notice", "2025-12-01", None,
     "320 MW combined cycle cogeneration unit supplying process steam to an adjacent chemical plant",
     "221112", 74.0, -0.005, 0.003),
    ("panhandle_nexus", "PROJ-326610", "183990", "RN114002115", "CN606440871", "Power Block",
     "New Source Review - Air Quality Permit", "Pending - Technical Review", "2026-04-14", None,
     "Twenty-two 12 MW natural gas reciprocating internal combustion engines providing "
     "behind-the-meter power to a data center campus", "221112", 118.6, 0.003, 0.006),
    ("bluff_creek", "PROJ-324455", "PBR-167220", "RN113900654", "CN606201533", "Hydrogen Facility",
     "Permit by Rule", "Registered", "2026-02-10", "2026-03-02",
     "Hydrogen electrolyzer facility with two 8 MMBtu/hr natural gas fired process heaters",
     "325120", 3.1, 0.002, 0.002),
    ("redbud", "PROJ-317003", "PBR-165330", "RN112700889", "CN605440217", "Mining Facility",
     "Permit by Rule", "Registered", "2025-05-22", "2025-06-18",
     "Immersion-cooled bitcoin mining facility, six 2 MW diesel standby generators", "518210",
     8.7, -0.004, 0.003),
    ("marlin_bend", "PROJ-329871", "184551", "RN114330902", "CN606771040", "Generation Facility",
     "New Source Review - Air Quality Permit", "Pending - Administrative Review", "2026-05-27", None,
     "Twenty 11 MW natural gas reciprocating engines serving a colocated data center campus",
     "221112", 97.3, 0.005, 0.005),
    ("twin_buttes", "PROJ-320115", "PBR-166702", "RN113100447", "CN605990312", "Facility",
     "Permit by Rule", "Registered", "2025-09-02", "2025-09-22",
     "Battery energy storage facility, one 1.5 MW diesel emergency generator", "221118",
     1.2, 0.001, -0.002),
    ("deer_hollow", "PROJ-330554", "SP-77120", None, None, "AI Campus",
     "Air Quality Standard Permit - Electric Generating Units", "Pending - Technical Review",
     "2026-06-08", None,
     "Standard permit registration for thirty 2.5 MW diesel emergency standby generators "
     "at a computing campus", "518210", 24.9, 0.002, 0.004),
    ("gulf_terrace", "PROJ-298110", "O-2071", "RN111788203", "CN605120988", "Cogeneration Plant",
     "Federal Operating Permit - Title V - Renewal", "Issued", "2024-09-15", "2025-06-30",
     "Renewal of the site operating permit for the cogeneration facility", "221112",
     None, -0.005, 0.003),
    ("whitethorn", "PROJ-333001", "PBR-168440", None, None, "Ranch Substation",
     "Permit by Rule", "Withdrawn", "2026-04-20", None,
     "Rock crushing and concrete batch plant for site preparation", "327320", None, 0.0, 0.0),
]

# --- TCEQ stormwater construction NOI ----------------------------------------
# (dev_key, permit_no, rn, name_suffix, acres, issued, start, end, description,
#  lat_offset, lon_offset)
_NOI_ROWS = [
    ("brazos_ridge", "TXR15A4471", "RN112900431", "Digital Campus", 344.0, "2025-09-15",
     "2025-10-01", "2028-12-31", "Grading and utility installation for a data center campus "
     "and adjacent generation site", 0.005, -0.003),
    ("caprock", "TXR15A4620", "RN113408877", "Data Center", 212.5, "2025-11-03", "2025-11-20",
     "2028-06-30", "Site development for a hyperscale data center campus", 0.003, 0.005),
    ("panhandle_nexus", "TXR15A4880", "RN114002115", "Campus", 511.0, "2026-01-22", "2026-02-15",
     "2029-12-31", "Mass grading for a data center campus and onsite generation", 0.004, 0.005),
    ("deer_hollow", "TXR15A4901", None, "AI Campus", 268.0, "2026-02-10", "2026-03-01",
     "2029-06-30", "Site preparation for a computing campus", 0.002, 0.004),
    ("ocotillo", "TXR15A4310", None, "", 1840.0, "2025-08-01", "2025-09-01", "2028-06-30",
     "Solar generation facility construction", -0.010, 0.012),
    ("twin_buttes", "TXR15A4402", "RN113100447", "", 41.0, "2025-08-19", "2025-09-05",
     "2027-03-31", "Battery energy storage facility construction", 0.002, -0.001),
    ("whitethorn", "TXR15A5011", None, "Ranch Substation", 88.0, "2026-03-05", "2026-04-01",
     "2027-09-30", "Substation and access road construction", 0.0, 0.0),
    ("redbud", "TXR15A3990", "RN112700889", "Flats", 63.0, "2024-11-12", "2024-12-01",
     "2025-08-31", "Site development for a digital mining facility", -0.003, 0.002),
    ("marlin_bend", "TXR15A5120", "RN114330902", "Campus", 402.0, "2026-04-18", "2026-05-15",
     "2029-12-31", "Data center campus and generation site earthwork", 0.004, 0.004),
    ("stonewall_gap", "TXR15A4790", None, "", 960.0, "2025-12-09", "2026-01-05",
     "2028-04-30", "Photovoltaic solar generation facility construction", 0.008, -0.009),
    ("sabine_point", "TXR15A4110", "RN110044822", "", 155.0, "2025-04-01",
     "2025-05-01", "2029-01-31", "Power generating station construction", -0.002, 0.004),
    ("kiowa_draw", "TXR15A4205", None, "Mining Site", 34.0, "2025-05-20", "2025-06-10",
     "2026-02-28", "Digital asset mining facility site work", 0.001, 0.001),
]


def _gis_frame():
    rows = []
    for key, inr, suffix, fuel, tech, mw, cod, phase, ia in _GIS_ROWS:
        name, operator, county, _, _ = _dev(key)
        rows.append(
            {
                "INR": inr,
                "Project Name": f"{name} {suffix}".strip(),
                "Interconnecting Entity": operator,
                "POI Location": f"{county} County 345kV Substation",
                "County": county,
                "Fuel": fuel,
                "Technology": tech,
                "Capacity (MW)": mw,
                "Projected COD": cod,
                "GIM Study Phase": phase,
                "Screening Study Complete": "Yes" if "Complete" in (phase or "") else None,
                "IA Signed": ia,
                "CDR Reporting Zone": "ERCOT",
            }
        )
    return pd.DataFrame(rows)


def _large_load_frame():
    rows = []
    for key, request_id, suffix, mw, load_type, status, energization, colocated in _LARGE_LOAD_ROWS:
        name, operator, county, _, _ = _dev(key)
        rows.append(
            {
                "Request Number": request_id,
                "Project Name": f"{name} {suffix}".strip(),
                "Interconnecting Entity": operator,
                "County": county,
                "Requested Capacity (MW)": mw,
                "Load Type": load_type,
                "Status": status,
                "Projected Energization": energization,
                "Co-located": colocated,
                "POI": f"{county} County 345kV Substation",
            }
        )
    return pd.DataFrame(rows)


def _air_frame():
    rows = []
    for (key, project_no, permit_no, rn, cn, suffix, permit_type, status, received,
         issued, description, naics, nox, dlat, dlon) in _AIR_ROWS:
        name, operator, county, lat, lon = _dev(key)
        rows.append(
            {
                "Project Number": project_no,
                "Permit Number": permit_no,
                "Regulated Entity Name": f"{name} {suffix}".strip(),
                "Customer Name": operator,
                "RN Number": rn,
                "CN Number": cn,
                "County": county,
                "Physical Location": f"{county} County, Texas",
                "Latitude": round(lat + dlat, 6),
                "Longitude": round(lon + dlon, 6),
                "Permit Type": permit_type,
                "Application Status": status,
                "Received Date": received,
                "Issued Date": issued,
                "Project Description": description,
                "NAICS": naics,
                "NOx (TPY)": nox,
            }
        )
    return pd.DataFrame(rows)


def _noi_frame():
    rows = []
    for (key, permit_no, rn, suffix, acres, issued, start, end, description,
         dlat, dlon) in _NOI_ROWS:
        name, operator, county, lat, lon = _dev(key)
        rows.append(
            {
                "Permit Number": permit_no,
                "Site Name": f"{name} {suffix}".strip(),
                "Operator Name": operator,
                "RN Number": rn,
                "County": county,
                "Site Address": f"{county} County, Texas",
                "Latitude": round(lat + dlat, 6),
                "Longitude": round(lon + dlon, 6),
                "Acres Disturbed": acres,
                "Status": "Active",
                "Issued Date": issued,
                "Projected Start Date": start,
                "Projected End Date": end,
                "Project Description": description,
            }
        )
    return pd.DataFrame(rows)


# --- PUCT Interchange ---------------------------------------------------------
# (dev_key, control, filings, utility, style_template, parties)
# A docket is a proceeding about a project, not the project: the CCN rows name a
# transmission line reaching a site, and the large-load rows name the customer
# the utility is building for — which is the one thing ERCOT's aggregate
# large-load queue never states.
_PUCT_ROWS = [
    ("brazos_ridge", "56412", 34, "ONCOR ELECTRIC DELIVERY CO",
     "APPLICATION OF ONCOR ELECTRIC DELIVERY COMPANY LLC TO AMEND ITS CERTIFICATE "
     "OF CONVENIENCE AND NECESSITY FOR THE {name} 345-KV TRANSMISSION LINE IN "
     "{county} COUNTY", ""),
    ("marlin_bend", "57120", 12, "ONCOR ELECTRIC DELIVERY CO",
     "APPLICATION OF ONCOR ELECTRIC DELIVERY COMPANY LLC FOR AN ECONOMIC "
     "DEVELOPMENT RATE RIDER FOR A NEW DATA CENTER SERVING {operator}", ""),
    ("sabine_point", "56880", 21, "ENTERGY TEXAS, INC.",
     "APPLICATION OF ENTERGY TEXAS, INC. TO AMEND ITS CERTIFICATE OF CONVENIENCE "
     "AND NECESSITY FOR THE {name} 138-KV TRANSMISSION LINE IN {county} COUNTY", ""),
    (None, "58481", 203, "PUC RULES & PROJECTS",
     "RULEMAKING TO IMPLEMENT LARGE LOAD INTERCONNECTION STANDARDS UNDER "
     "PURA 37.0561",
     "Marlin Bend Data Works LLC; Redbud Digital Holdings LLC; Kiowa Draw Mining "
     "Company LLC"),
]


def _puct_frame():
    rows = []
    for key, control, filings, utility, template, parties in _PUCT_ROWS:
        if key:
            name, operator, county, _, _ = _dev(key)
        else:
            name = operator = county = ""
        rows.append(
            {
                "control_number": control,
                "filings": filings,
                "utility": utility,
                "case_style": template.format(
                    name=name.upper(), operator=operator.upper(),
                    county=county.upper(),
                ),
                "parties": parties,
            }
        )
    return pd.DataFrame(rows)


FRAME_BUILDERS = {
    "ercot-gis-report-demo.csv": _gis_frame,
    "ercot-large-load-demo.csv": _large_load_frame,
    "tceq-air-nsr-demo.csv": _air_frame,
    "tceq-stormwater-noi-demo.csv": _noi_frame,
    "puct-interchange-dockets-demo.csv": _puct_frame,
}


def write_demo_files(directory):
    """Write the synthetic exports, one per source. Returns the paths written."""
    os.makedirs(directory, exist_ok=True)
    paths = []
    for filename, builder in FRAME_BUILDERS.items():
        path = os.path.join(directory, filename)
        builder().to_csv(path, index=False)
        paths.append(path)

    readme = os.path.join(directory, "README.txt")
    with open(readme, "w") as handle:
        handle.write(
            DEMO_BANNER + "\n\n"
            "These CSVs imitate the column layout of the real ERCOT GIS, ERCOT\n"
            "large load, TCEQ air NSR, TCEQ stormwater NOI and PUCT Interchange\n"
            "exports so the ingest pipeline can be run end to end offline. Every\n"
            "project name, company, docket number, permit number and coordinate\n"
            "is fabricated.\n"
        )
    return paths


def build_demo_db(db_path, raw_dir=None, threshold=None, verbose=True):
    """Generate the demo files and build a complete database from them."""
    from . import db as dbmod
    from . import pipeline
    from . import link as linkmod

    raw_dir = raw_dir or os.path.join(os.path.dirname(os.path.abspath(db_path)), "demo_raw")
    write_demo_files(raw_dir)
    if verbose:
        print(f"{DEMO_BANNER}\nWrote demo exports to {raw_dir}")
    summary = pipeline.refresh(
        db_path,
        raw_dir=raw_dir,
        fetch=False,
        threshold=threshold or linkmod.DEFAULT_THRESHOLD,
        force=True,
        verbose=verbose,
    )

    # Tag the ingested files so `stats` — and the dashboard banner — can tell
    # this database apart from one built on real exports.
    conn = dbmod.open_db(db_path)
    try:
        dbmod.mark_demo_files(conn, os.path.abspath(raw_dir), DEMO_BANNER)
    finally:
        conn.close()
    return summary
