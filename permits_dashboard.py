"""
TCEQ / ERCOT permit and interconnection dashboard.

Connects five feeds — ERCOT generation queue, ERCOT large load queue, TCEQ air
permits, TCEQ stormwater construction NOIs and PUCT Interchange dockets — into
resolved sites, and surfaces generation projects, data centers, and the
colocated gen+load developments that only become visible once the feeds are
joined. PUCT stays alongside rather than inside that join: a docket is a
proceeding, not a site, and it is where large-load customers are named at all,
since ERCOT publishes its large-load queue in aggregate.

Run:  streamlit run permits_dashboard.py
Build the database first:  python permits_cli.py demo   (or `ingest` + `link`)
"""

import json
import os
import sys

import pandas as pd
import plotly.express as px
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.permits import classify, db as dbmod, link as linkmod  # noqa: E402
from src.permits import status as statusmod  # noqa: E402

st.set_page_config(
    page_title="TCEQ / ERCOT Permit Intelligence",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = os.environ.get("PERMITS_DB", os.path.join("output", "permits.db"))

# Categorical hues, assigned to entities in fixed slot order so a filter that
# drops a series never repaints the survivors. Values are the validated
# reference palette's light-mode steps; Streamlit's plotly theme handles the
# surfaces and text for light/dark.
SLOT = {
    1: "#2a78d6",  # blue
    2: "#eb6834",  # orange
    3: "#1baf7a",  # aqua
    4: "#eda100",  # yellow
    5: "#e87ba4",  # magenta
    6: "#008300",  # green
    7: "#4a3aa7",  # violet
    8: "#e34948",  # red
}
NEUTRAL = "#9a9a94"

# Fuel keeps a fixed slot; the stack/legend order below matches slot order, so
# adjacent segments are the validated adjacent pairs.
FUEL_COLORS = {
    "Natural gas": SLOT[1],
    "Solar": SLOT[2],
    "Wind": SLOT[3],
    "Battery storage": SLOT[4],
    "Nuclear": SLOT[5],
    "Biomass": SLOT[6],
    "Coal": SLOT[7],
    "Petroleum": SLOT[8],
    "Other": NEUTRAL,
}
FUEL_ORDER = list(FUEL_COLORS)

SITE_CLASS_LABELS = {
    linkmod.SITE_COLOCATED: "Colocated gen + load",
    linkmod.SITE_GENERATION_ONLY: "Generation only",
    linkmod.SITE_LOAD_ONLY: "Load only",
    linkmod.SITE_CONSTRUCTION_ONLY: "Construction, end use unknown",
    linkmod.SITE_MIXED: "Mixed",
}
SITE_CLASS_COLORS = {
    "Colocated gen + load": SLOT[2],
    "Generation only": SLOT[1],
    "Load only": SLOT[3],
    "Construction, end use unknown": NEUTRAL,
    "Mixed": SLOT[7],
}
SITE_CLASS_ORDER = list(SITE_CLASS_COLORS)

KIND_COLORS = {
    "Generation": SLOT[1],
    "Data center": SLOT[2],
    "Crypto mining": SLOT[3],
    "Industrial load": SLOT[4],
    "Construction (end use unknown)": NEUTRAL,
    "Unclassified": NEUTRAL,
}

# Lifecycle is an ordered state, not an identity — but it is drawn as discrete
# categories, so it takes fixed slots too. Pending gets the attention colour
# because "still in process" is the thing being looked for.
LIFECYCLE_COLORS = {
    "Application pending": SLOT[2],
    "Approved / issued": SLOT[1],
    "Operating": SLOT[3],
    "Withdrawn / void": NEUTRAL,
    "Denied": SLOT[8],
    "Expired / terminated": NEUTRAL,
    "Status unknown": NEUTRAL,
}
LIFECYCLE_ORDER = [
    statusmod.summarize_lifecycle(value) for value in statusmod.LIFECYCLE_ORDER
]

PROGRAM_ORDER = [
    statusmod.PROGRAM_NSR, statusmod.PROGRAM_PSD, statusmod.PROGRAM_NNSR,
    statusmod.PROGRAM_PBR, statusmod.PROGRAM_STANDARD, statusmod.PROGRAM_TITLE_V,
    statusmod.PROGRAM_DE_MINIMIS, statusmod.PROGRAM_OTHER_AIR,
    statusmod.PROGRAM_STORMWATER, statusmod.PROGRAM_INTERCONNECTION,
]

SOURCE_LABELS = {
    "ercot_gis": "ERCOT generation queue",
    "ercot_large_load": "ERCOT large load queue",
    "tceq_air": "TCEQ air permit",
    "tceq_swnoi": "TCEQ stormwater NOI",
    "puct": "PUCT docket",
}


# --- Data loading ------------------------------------------------------------

@st.cache_data(show_spinner=False)
def load_all(db_path, mtime):
    """Read the whole database into DataFrames. `mtime` busts the cache."""
    conn = dbmod.connect(db_path)
    try:
        dbmod.init_db(conn)
        sites = dbmod.load_sites(conn)
        entities = dbmod.load_entities(conn)
        members = pd.read_sql_query("SELECT * FROM site_members", conn)
        links = pd.read_sql_query("SELECT * FROM entity_links", conn)
        info = dbmod.stats(conn)
    finally:
        conn.close()

    if not sites.empty:
        sites["site_class_label"] = (
            sites["site_class"].map(SITE_CLASS_LABELS).fillna("Mixed")
        )
        sites["fuel_labels"] = sites["fuels"].fillna("").apply(
            lambda value: ", ".join(
                classify.summarize_fuel(part) for part in value.split(",") if part
            )
        )
        sites["primary_fuel"] = sites["fuels"].fillna("").apply(
            lambda value: classify.summarize_fuel(value.split(",")[0])
            if value else "Unknown"
        )
        sites["total_mw"] = sites[["gen_mw", "load_mw"]].fillna(0).sum(axis=1)
        sites["cod_year"] = pd.to_datetime(
            sites["latest_date"], errors="coerce"
        ).dt.year

    if not entities.empty:
        entities["kind_label"] = entities["project_kind"].apply(classify.summarize_kind)
        entities["fuel_label"] = entities["fuel"].apply(
            lambda value: classify.summarize_fuel(value) if value else "Unknown"
        )
        entities["source_label"] = entities["source"].map(SOURCE_LABELS).fillna(
            entities["source"]
        )
        entities["cod_year"] = pd.to_datetime(
            entities["projected_cod"], errors="coerce"
        ).dt.year
        entities["lifecycle_label"] = entities["lifecycle"].apply(
            statusmod.summarize_lifecycle
        )
        entities["program_label"] = entities["permit_program"].apply(
            statusmod.summarize_program
        )
        entities["stage_label"] = entities["stage"].apply(statusmod.summarize_stage)
        entities["action_label"] = entities["permit_action"].apply(
            statusmod.summarize_action
        )
        # Recomputed on every load rather than stored: the answer moves daily.
        entities["days_sitting"] = entities["received_date"].apply(
            statusmod.days_sitting
        )
        entities["in_process"] = entities["lifecycle"].isin(statusmod.IN_PROCESS)
        # SQLite hands a column back as object dtype when every value it saw was
        # NULL, which is what a source of pure dockets or pure permits produces.
        # Coerce once, here, rather than at each use: pandas raises on nlargest
        # and plotly on marker sizing, and both failures are far from the cause.
        for column in ("capacity_mw", "load_mw", "acres", "latitude", "longitude",
                       "nox_tpy", "co_tpy", "voc_tpy", "pm_tpy", "so2_tpy",
                       "ghg_tpy"):
            if column in entities:
                entities[column] = pd.to_numeric(entities[column], errors="coerce")
        # One MW column regardless of which side of the meter a record sits on.
        entities["mw"] = entities["capacity_mw"].fillna(entities["load_mw"])
        entities["tech_label"] = entities["technology"].apply(
            lambda value: statusmod.summarize_stage(value) or "Not stated"
        )
        entities["submitted"] = entities["received_date"].fillna(
            entities["status_date"]
        )
        entities["submitted_year"] = pd.to_datetime(
            entities["submitted"], errors="coerce"
        ).dt.year

    if not sites.empty:
        sites["lifecycle_label"] = sites["lifecycle"].apply(
            statusmod.summarize_lifecycle
        )

    return sites, entities, members, links, info


def db_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def fuel_bucket(label):
    """Fold fuels outside the fixed slot list into 'Other'."""
    return label if label in FUEL_COLORS else "Other"


def mw(value):
    """Format a megawatt value for display."""
    if value is None or pd.isna(value):
        return "—"
    return f"{value:,.0f} MW"


# --- Empty state -------------------------------------------------------------

def render_empty_state():
    st.title("⚡ TCEQ / ERCOT Permit Intelligence")
    st.warning(f"No database found at `{DB_PATH}`.")
    st.markdown(
        """
Build one first. The fastest way to see the whole thing working, no network
needed:

```bash
python permits_cli.py demo
```

That writes synthetic exports and runs them through the real ingest pipeline.

For real data, download the source files and ingest them:

```bash
python permits_cli.py fetch                  # ERCOT MIS, where reachable
python permits_cli.py ingest --dir data/raw  # anything you downloaded by hand
python permits_cli.py link
```

Supported exports: ERCOT GIS report, ERCOT large load interconnection report,
TCEQ air NSR permit applications, TCEQ stormwater construction NOIs (TXR150000).
        """
    )


# --- Sidebar -----------------------------------------------------------------

def sidebar_filters(sites, info):
    st.sidebar.title("Filters")

    counties = sorted(sites["county"].dropna().unique().tolist())
    selected_counties = st.sidebar.multiselect("County", counties, default=[])

    classes = [
        label for label in SITE_CLASS_ORDER
        if label in set(sites["site_class_label"])
    ]
    selected_classes = st.sidebar.multiselect(
        "Site type", classes, default=classes
    )

    fuels = sorted(
        {
            classify.summarize_fuel(fuel)
            for value in sites["fuels"].dropna()
            for fuel in value.split(",")
            if fuel
        }
    )
    selected_fuels = st.sidebar.multiselect("Permitted fuel", fuels, default=[])

    max_mw = float(sites["total_mw"].max() or 0)
    min_mw = st.sidebar.slider(
        "Minimum size (gen + load MW)", 0.0, max(max_mw, 1.0), 0.0, step=25.0
    )

    search = st.sidebar.text_input("Search name / operator", "")

    st.sidebar.divider()
    st.sidebar.caption("Permit state")
    lifecycles = st.sidebar.multiselect(
        "Lifecycle", LIFECYCLE_ORDER, default=[],
        help="Empty means all states. Pick 'Application pending' to see only "
             "what is still sitting in process.",
    )
    programs = st.sidebar.multiselect(
        "Authorization program",
        [statusmod.summarize_program(value) for value in PROGRAM_ORDER],
        default=[],
    )

    st.sidebar.divider()
    st.sidebar.caption("Database")
    st.sidebar.caption(f"`{DB_PATH}`")
    st.sidebar.caption(f"{info['entities']} records · {info['sites']} sites")
    if info.get("last_ingest"):
        st.sidebar.caption(f"Last ingest {info['last_ingest'][:16].replace('T', ' ')} UTC")
    if st.sidebar.button("Reload data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.divider()
    offline_map = st.sidebar.checkbox(
        "Offline basemap",
        value=False,
        help="Draw the map from bundled US geometry instead of fetching map tiles. "
             "Use this when the machine has no internet access.",
    )

    filtered = sites.copy()
    if selected_counties:
        filtered = filtered[filtered["county"].isin(selected_counties)]
    if selected_classes:
        filtered = filtered[filtered["site_class_label"].isin(selected_classes)]
    if selected_fuels:
        pattern = "|".join(
            fuel for fuel in classify.FUEL_ORDER
            if classify.summarize_fuel(fuel) in selected_fuels
        )
        if pattern:
            filtered = filtered[
                filtered["fuels"].fillna("").str.contains(pattern, regex=True)
            ]
    if min_mw > 0:
        filtered = filtered[filtered["total_mw"] >= min_mw]
    if search.strip():
        needle = search.strip().lower()
        haystack = (
            filtered["site_name"].fillna("") + " " + filtered["operator"].fillna("")
        ).str.lower()
        filtered = filtered[haystack.str.contains(needle, regex=False)]

    return filtered, lifecycles, programs, offline_map, selected_counties


# --- Views -------------------------------------------------------------------

def render_headline(sites, entities):
    colocated = sites[sites["site_class"] == linkmod.SITE_COLOCATED]
    data_centers = entities[
        entities["project_kind"].isin([classify.KIND_DATA_CENTER, classify.KIND_CRYPTO])
    ]
    in_process = entities[entities["in_process"]]

    columns = st.columns(6)
    columns[0].metric("Sites", f"{len(sites):,}")
    columns[1].metric("Colocated gen + load", f"{len(colocated):,}")
    columns[2].metric(
        "Generation approved", mw(sites["gen_mw_approved"].sum(skipna=True))
    )
    columns[3].metric(
        "Generation pending", mw(sites["gen_mw_pending"].sum(skipna=True))
    )
    columns[4].metric("Requested load", mw(sites["load_mw"].sum(skipna=True)))
    columns[5].metric("Applications in process", f"{len(in_process):,}")
    st.caption(
        "Approved counts authorizations that have been issued or executed; "
        "pending counts what is still in review. Both are shown because a queue "
        "position and a granted permit are very different facts."
    )


GEO_LABELS = {
    "site": "Surveyed site coordinates",
    "zip": "ZIP centroid (approximate)",
    "county": "County centroid (coarse)",
    "none": "No geography",
}


def render_map(records, offline_map):
    """Clickable, filterable map of individual permit and queue records.

    Records, not resolved sites: the questions asked of this map are about
    permits — what fuel, which technology, how big, filed when — and collapsing
    them into sites would hide exactly that detail.
    """
    st.subheader("Permit and interconnection map")

    located = records[
        records["latitude"].notna() & records["longitude"].notna()
    ].copy()
    if located.empty:
        st.info("No records carry usable geography under the current filters.")
        return

    # Most TCEQ air permits are concrete plants, pipelines and refineries. The
    # question this map answers is about electric generation, so the default view
    # keeps records that are classified as generation or load, or that carry a
    # fuel — and says plainly how many it set aside.
    generation_related = (
        located["project_kind"].isin(
            [classify.KIND_GENERATION, *classify.LOAD_KINDS]
        )
        | located["fuel"].notna()
        | located["primary_business"].fillna("").str.contains(
            "POWER|ELECTRIC|GENERAT", case=False, regex=True
        )
    )
    total = len(located)
    only_generation = st.checkbox(
        "Electric-generation related only", value=True,
        help="Excludes air permits with no generation signal — concrete plants, "
             "pipelines, refineries and the like.",
    )
    if only_generation:
        located = located[generation_related]
        st.caption(
            f"Showing {len(located):,} generation-related records; "
            f"{total - len(located):,} other permits hidden."
        )

    controls = st.columns(4)
    with controls[0]:
        techs = sorted(located["tech_label"].dropna().unique().tolist())
        picked_tech = st.multiselect("Generation type", techs, default=[])
    with controls[1]:
        fuels = sorted(located["fuel_label"].dropna().unique().tolist())
        picked_fuel = st.multiselect("Fuel", fuels, default=[])
    with controls[2]:
        sized = located["mw"].dropna()
        cap = float(sized.max()) if not sized.empty else 0.0
        min_mw = st.slider("Minimum MW", 0.0, max(cap, 1.0), 0.0, step=25.0)
    with controls[3]:
        precisions = [
            GEO_LABELS.get(value, value)
            for value in ["site", "zip", "county"]
            if value in set(located["geo_precision"])
        ]
        picked_precision = st.multiselect(
            "Location precision", precisions, default=precisions,
            help="ZIP and county centroids are approximate. Neither ERCOT nor "
                 "TCEQ publishes surveyed coordinates.",
        )

    years = located["submitted_year"].dropna()
    if not years.empty:
        low, high = int(years.min()), int(years.max())
        if low < high:
            picked_years = st.slider(
                "Date submitted", low, high, (low, high), step=1,
                help="Filing date for TCEQ permits, queue entry year for ERCOT.",
            )
            located = located[
                located["submitted_year"].between(*picked_years)
                | located["submitted_year"].isna()
            ]

    if picked_tech:
        located = located[located["tech_label"].isin(picked_tech)]
    if picked_fuel:
        located = located[located["fuel_label"].isin(picked_fuel)]
    if min_mw > 0:
        located = located[located["mw"].fillna(0) >= min_mw]
    if picked_precision:
        wanted = {
            key for key, label in GEO_LABELS.items() if label in picked_precision
        }
        located = located[located["geo_precision"].isin(wanted)]

    if located.empty:
        st.info("Nothing matches those filters.")
        return

    MAX_POINTS = 6000
    dropped = 0
    if len(located) > MAX_POINTS:
        # Largest first, so a truncated view still shows what matters. Never
        # silently: the count of omitted records is printed below the map.
        located = located.sort_values("mw", ascending=False, na_position="last")
        dropped = len(located) - MAX_POINTS
        located = located.head(MAX_POINTS)

    located["Precision"] = located["geo_precision"].map(GEO_LABELS).fillna("Unknown")
    # Marker area carries MW where it is known; unsized records stay small and
    # uniform rather than pretending to a capacity nobody published.
    located["marker"] = (
        pd.to_numeric(located["mw"], errors="coerce").fillna(0).clip(lower=0) + 18
    )

    hover = {
        "operator": True, "county": True, "fuel_label": True, "tech_label": True,
        "mw": ":,.1f", "lifecycle_label": True, "program_label": True,
        "permit_number": True, "submitted": True, "Precision": True,
        "latitude": False, "longitude": False, "marker": False,
        "kind_label": False,
    }
    labels = {
        "kind_label": "Record type", "operator": "Operator", "county": "County",
        "fuel_label": "Fuel", "tech_label": "Technology", "mw": "MW",
        "lifecycle_label": "State", "program_label": "Program",
        "permit_number": "Permit / INR", "submitted": "Submitted",
    }

    if offline_map:
        figure = px.scatter_geo(
            located, lat="latitude", lon="longitude", scope="usa",
            color="kind_label", size="marker", size_max=30,
            color_discrete_map=KIND_COLORS, hover_name="project_name",
            hover_data=hover, labels=labels,
        )
        figure.update_geos(fitbounds="locations")
    else:
        figure = px.scatter_mapbox(
            located, lat="latitude", lon="longitude", zoom=4.6,
            center={"lat": 31.3, "lon": -99.0},
            color="kind_label", size="marker", size_max=30,
            color_discrete_map=KIND_COLORS, hover_name="project_name",
            hover_data=hover, labels=labels,
        )
        figure.update_layout(mapbox_style="open-street-map")

    figure.update_traces(marker=dict(opacity=0.8))
    figure.update_layout(
        height=600, margin=dict(l=0, r=0, t=0, b=0),
        legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, title=None),
    )
    selection = st.plotly_chart(
        figure, use_container_width=True, on_select="rerun",
        selection_mode=("points", "box", "lasso"), key="permit_map",
    )

    st.caption(
        (f"{dropped:,} smaller records not drawn — narrow the filters to see them. "
         if dropped else "")
        + f"{len(located):,} records plotted. Marker area scales with MW where a "
        "capacity is published — TCEQ never states one, so its records show at "
        "the base size. Click, box- or lasso-select on the map to inspect."
    )

    picked = (selection or {}).get("selection", {}).get("points", [])
    if picked:
        indices = [p["point_index"] for p in picked if "point_index" in p]
        # point_index is per-trace, so re-match on coordinates instead.
        coords = {(round(p["lat"], 5), round(p["lon"], 5)) for p in picked
                  if "lat" in p and "lon" in p}
        chosen = located[
            located.apply(
                lambda r: (round(r["latitude"], 5), round(r["longitude"], 5)) in coords,
                axis=1,
            )
        ] if coords else located.iloc[indices]
        st.markdown(f"**{len(chosen)} record(s) selected**")
        st.dataframe(
            chosen[[
                "project_name", "operator", "county", "kind_label", "fuel_label",
                "tech_label", "mw", "lifecycle_label", "program_label",
                "submitted", "permit_number", "Precision",
            ]].rename(columns={**labels, "project_name": "Record",
                               "Precision": "Location precision"}),
            use_container_width=True, hide_index=True,
        )
    else:
        st.caption("Select points on the map to list them here.")

    st.download_button(
        "Download mapped records (CSV)",
        located.to_csv(index=False).encode("utf-8"),
        file_name="mapped_permits.csv", mime="text/csv",
    )


def render_generation(entities, members):
    generation = entities[
        (entities["project_kind"] == classify.KIND_GENERATION)
        & entities["capacity_mw"].notna()
    ].copy()
    if generation.empty:
        st.info("No generation records with a capacity value.")
        return

    # Count each site's capacity once. Without this the charts would add a
    # plant's ERCOT queue entry to the same plant's TCEQ air permit and disagree
    # with the site totals in the header.
    counted = linkmod.select_capacity_records(generation, members)
    generation_all = generation.copy()
    generation_all["counted"] = generation_all["entity_id"].isin(counted)
    generation = generation[generation["entity_id"].isin(counted)].copy()
    generation["Fuel"] = generation["fuel_label"].apply(fuel_bucket)

    left, right = st.columns([3, 2])

    with left:
        st.subheader("Permitted capacity by fuel and projected COD")
        timeline = generation.dropna(subset=["cod_year"]).copy()
        if timeline.empty:
            st.info("No projected commercial operation dates on these records.")
        else:
            timeline["Year"] = timeline["cod_year"].astype(int).astype(str)
            grouped = (
                timeline.groupby(["Year", "Fuel"], as_index=False)["capacity_mw"]
                .sum()
                .rename(columns={"capacity_mw": "MW"})
            )
            figure = px.bar(
                grouped, x="Year", y="MW", color="Fuel",
                color_discrete_map=FUEL_COLORS,
                category_orders={"Fuel": FUEL_ORDER},
            )
            # 2px surface gap between stacked segments.
            figure.update_traces(
                marker_line_width=2, marker_line_color="rgba(255,255,255,0.9)"
            )
            figure.update_layout(
                height=420, bargap=0.32,
                yaxis_title="Nameplate MW", xaxis_title=None,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
                margin=dict(l=0, r=0, t=10, b=0),
            )
            st.plotly_chart(figure, use_container_width=True)

    with right:
        st.subheader("Capacity by fuel")
        by_fuel = (
            generation.groupby("Fuel", as_index=False)["capacity_mw"]
            .sum()
            .rename(columns={"capacity_mw": "MW"})
            .sort_values("MW", ascending=True)
        )
        figure = px.bar(
            by_fuel, x="MW", y="Fuel", orientation="h", color="Fuel",
            color_discrete_map=FUEL_COLORS, text="MW",
            category_orders={"Fuel": by_fuel["Fuel"].tolist()},
        )
        figure.update_traces(
            texttemplate="%{text:,.0f}", textposition="outside", cliponaxis=False,
            showlegend=False,
        )
        figure.update_layout(
            height=420, xaxis_title="Nameplate MW", yaxis_title=None,
            margin=dict(l=0, r=30, t=10, b=0),
        )
        st.plotly_chart(figure, use_container_width=True)

    st.subheader("Approved vs pending capacity by fuel")
    st.caption(
        "The same records split by authorization state — what is cleared to be "
        "built, against what is still being decided."
    )
    split = (
        generation.groupby(["Fuel", "lifecycle_label"], as_index=False)["capacity_mw"]
        .sum()
        .rename(columns={"capacity_mw": "MW", "lifecycle_label": "State"})
    )
    figure = px.bar(
        split, x="Fuel", y="MW", color="State", barmode="group",
        color_discrete_map=LIFECYCLE_COLORS,
        category_orders={"Fuel": FUEL_ORDER, "State": LIFECYCLE_ORDER},
    )
    figure.update_layout(
        height=340, bargap=0.3, yaxis_title="Nameplate MW", xaxis_title=None,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(figure, use_container_width=True)

    st.subheader("Project size distribution")
    figure = px.histogram(
        generation, x="capacity_mw", color="Fuel", nbins=30,
        color_discrete_map=FUEL_COLORS, category_orders={"Fuel": FUEL_ORDER},
    )
    figure.update_traces(marker_line_width=2, marker_line_color="rgba(255,255,255,0.9)")
    figure.update_layout(
        height=320, xaxis_title="Nameplate MW", yaxis_title="Projects",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(figure, use_container_width=True)

    st.subheader("Generation records")
    st.caption(
        "Every generation record, including the duplicate views of one plant that "
        "different feeds give. **Counted in totals** marks the rows the charts and "
        "site capacities are built from — one source per site, so a plant is never "
        "added to itself."
    )
    table = generation_all[[
        "project_name", "operator", "county", "fuel_label", "technology",
        "capacity_mw", "lifecycle_label", "program_label", "status",
        "projected_cod", "source_label", "permit_number", "counted",
    ]].rename(columns={
        "project_name": "Project", "operator": "Operator", "county": "County",
        "fuel_label": "Fuel", "technology": "Technology", "capacity_mw": "MW",
        "lifecycle_label": "State", "program_label": "Program",
        "status": "Agency status", "projected_cod": "Projected COD",
        "source_label": "Source", "permit_number": "Permit / INR",
        "counted": "Counted in totals",
    }).sort_values("MW", ascending=False)
    st.dataframe(table, use_container_width=True, hide_index=True)


def render_loads(entities):
    loads = entities[entities["project_kind"].isin(classify.LOAD_KINDS)].copy()
    if loads.empty:
        st.info("No load-side records yet. Ingest the ERCOT large load report.")
        return

    sized = loads[loads["load_mw"].notna()]
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Requested load by type and energization year")
        timeline = sized.dropna(subset=["cod_year"]).copy()
        if timeline.empty:
            st.info("No projected energization dates on these records.")
        else:
            timeline["Year"] = timeline["cod_year"].astype(int).astype(str)
            grouped = (
                timeline.groupby(["Year", "kind_label"], as_index=False)["load_mw"]
                .sum()
                .rename(columns={"load_mw": "MW", "kind_label": "Load type"})
            )
            figure = px.bar(
                grouped, x="Year", y="MW", color="Load type",
                color_discrete_map=KIND_COLORS,
            )
            figure.update_traces(
                marker_line_width=2, marker_line_color="rgba(255,255,255,0.9)"
            )
            figure.update_layout(
                height=420, bargap=0.32, yaxis_title="Requested MW", xaxis_title=None,
                legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
                margin=dict(l=0, r=0, t=10, b=0),
            )
            st.plotly_chart(figure, use_container_width=True)

    with right:
        st.subheader("Largest requested loads")
        top = sized.nlargest(12, "load_mw")[
            ["project_name", "load_mw", "kind_label"]
        ].rename(columns={
            "project_name": "Project", "load_mw": "MW", "kind_label": "Load type",
        }).sort_values("MW")
        figure = px.bar(
            top, x="MW", y="Project", orientation="h", color="Load type",
            color_discrete_map=KIND_COLORS, text="MW",
            # Colouring splits the bars across traces, which loses the sort; pin
            # the category order so the ranking survives.
            category_orders={"Project": top["Project"].tolist()},
        )
        figure.update_traces(
            texttemplate="%{text:,.0f}", textposition="outside", cliponaxis=False
        )
        figure.update_layout(
            height=420, xaxis_title="Requested MW", yaxis_title=None,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
            margin=dict(l=0, r=40, t=10, b=0),
        )
        st.plotly_chart(figure, use_container_width=True)

    st.subheader("Load records")
    table = loads[[
        "project_name", "operator", "county", "kind_label", "load_mw", "status",
        "projected_cod", "source_label", "kind_evidence",
    ]].rename(columns={
        "project_name": "Project", "operator": "Operator", "county": "County",
        "kind_label": "Type", "load_mw": "MW", "status": "Status",
        "projected_cod": "Energization", "source_label": "Source",
        "kind_evidence": "Why classified this way",
    }).sort_values("MW", ascending=False)
    st.dataframe(table, use_container_width=True, hide_index=True)


def render_colocated(sites):
    colocated = sites[sites["site_class"] == linkmod.SITE_COLOCATED].copy()
    st.subheader("Sites with both generation and load")
    st.caption(
        "A site qualifies when at least one member record is generation and at "
        "least one is a data center, crypto or industrial load, after the records "
        "have been resolved to the same location."
    )
    if colocated.empty:
        st.info("No colocated sites in the current filter.")
        return

    table = colocated[[
        "site_name", "operator", "county", "gen_mw", "load_mw", "fuel_labels",
        "dispatchable_mw", "sources", "n_members", "confidence",
    ]].rename(columns={
        "site_name": "Site", "operator": "Operator", "county": "County",
        "gen_mw": "Gen MW", "load_mw": "Load MW", "fuel_labels": "Permitted fuel",
        "dispatchable_mw": "Dispatchable MW", "sources": "Feeds",
        "n_members": "Records", "confidence": "Match confidence",
    }).sort_values("Load MW", ascending=False)
    st.dataframe(table, use_container_width=True, hide_index=True)

    st.subheader("Generation vs load at colocated sites")
    paired = colocated[["site_name", "gen_mw", "load_mw"]].fillna(0)
    melted = paired.melt(
        id_vars="site_name", var_name="Measure", value_name="MW"
    ).replace({"gen_mw": "Permitted generation", "load_mw": "Requested load"})
    figure = px.bar(
        melted.sort_values("MW"), x="MW", y="site_name", color="Measure",
        orientation="h", barmode="group",
        color_discrete_map={
            "Permitted generation": SLOT[1], "Requested load": SLOT[2],
        },
    )
    figure.update_layout(
        height=max(320, 46 * len(colocated)), xaxis_title="MW", yaxis_title=None,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
        margin=dict(l=0, r=0, t=10, b=0),
    )
    st.plotly_chart(figure, use_container_width=True)



def render_permits(entities):
    """Permits and applications: what is authorized, what is still sitting."""
    permits = entities[entities["permit_program"].notna()].copy()
    if permits.empty:
        st.info("No permit records in the current filter.")
        return

    in_process = permits[permits["in_process"]]

    columns = st.columns(4)
    columns[0].metric("Authorizations tracked", f"{len(permits):,}")
    columns[1].metric("Still in process", f"{len(in_process):,}")
    median_days = in_process["days_sitting"].median()
    columns[2].metric(
        "Median time sitting",
        f"{median_days:,.0f} days" if pd.notna(median_days) else "—",
    )
    oldest = in_process["days_sitting"].max()
    columns[3].metric(
        "Longest sitting", f"{oldest:,.0f} days" if pd.notna(oldest) else "—"
    )

    left, right = st.columns(2)

    with left:
        st.subheader("Authorizations by program and state")
        grouped = (
            permits.groupby(["program_label", "lifecycle_label"], as_index=False)
            .size()
            .rename(columns={"size": "Records", "program_label": "Program",
                             "lifecycle_label": "State"})
        )
        order = [
            statusmod.summarize_program(value) for value in PROGRAM_ORDER
            if statusmod.summarize_program(value) in set(grouped["Program"])
        ]
        figure = px.bar(
            grouped, x="Records", y="Program", color="State", orientation="h",
            color_discrete_map=LIFECYCLE_COLORS,
            category_orders={"Program": list(reversed(order)),
                             "State": LIFECYCLE_ORDER},
        )
        figure.update_traces(
            marker_line_width=2, marker_line_color="rgba(255,255,255,0.9)"
        )
        figure.update_layout(
            height=420, xaxis_title="Records", yaxis_title=None,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(figure, use_container_width=True)

    with right:
        st.subheader("Where pending applications are stuck")
        staged = in_process[in_process["stage_label"].notna()]
        if staged.empty:
            st.info("No review stage stated on the pending records.")
        else:
            grouped = (
                staged.groupby("stage_label", as_index=False)
                .size()
                .rename(columns={"size": "Records", "stage_label": "Stage"})
                .sort_values("Records")
            )
            figure = px.bar(
                grouped, x="Records", y="Stage", orientation="h", text="Records",
                category_orders={"Stage": grouped["Stage"].tolist()},
            )
            figure.update_traces(
                marker_color=SLOT[2], texttemplate="%{text}",
                textposition="outside", cliponaxis=False,
            )
            figure.update_layout(
                height=420, xaxis_title="Records", yaxis_title=None,
                margin=dict(l=0, r=30, t=10, b=0),
            )
            st.plotly_chart(figure, use_container_width=True)

    st.subheader("Applications sitting in process")
    st.caption(
        "Oldest filing first. **Days sitting** is measured from the application "
        "received date to today, so it moves on its own; blank means the export "
        "carried no received date."
    )
    sitting = in_process.sort_values(
        "days_sitting", ascending=False, na_position="last"
    )
    st.dataframe(
        sitting[[
            "project_name", "operator", "county", "program_label", "action_label",
            "stage_label", "received_date", "days_sitting", "capacity_mw",
            "load_mw", "fuel_label", "nox_tpy", "permit_number", "status",
        ]].rename(columns={
            "project_name": "Project", "operator": "Operator", "county": "County",
            "program_label": "Program", "action_label": "Action",
            "stage_label": "Stage", "received_date": "Filed",
            "days_sitting": "Days sitting", "capacity_mw": "Gen MW",
            "load_mw": "Load MW", "fuel_label": "Fuel", "nox_tpy": "NOx (tpy)",
            "permit_number": "Permit / INR", "status": "Agency status",
        }),
        use_container_width=True, hide_index=True,
    )
    st.download_button(
        "Download pending applications (CSV)",
        sitting.to_csv(index=False).encode("utf-8"),
        file_name="pending_applications.csv",
        mime="text/csv",
    )

    approved = permits[permits["lifecycle"].isin(statusmod.AUTHORIZED)]
    st.subheader("Approved authorizations")
    st.dataframe(
        approved[[
            "project_name", "operator", "county", "program_label", "action_label",
            "decision_date", "capacity_mw", "load_mw", "fuel_label", "nox_tpy",
            "permit_number", "status",
        ]].rename(columns={
            "project_name": "Project", "operator": "Operator", "county": "County",
            "program_label": "Program", "action_label": "Action",
            "decision_date": "Decided", "capacity_mw": "Gen MW",
            "load_mw": "Load MW", "fuel_label": "Fuel", "nox_tpy": "NOx (tpy)",
            "permit_number": "Permit / INR", "status": "Agency status",
        }).sort_values("Decided", ascending=False),
        use_container_width=True, hide_index=True,
    )

    emitting = permits[permits["nox_tpy"].notna()]
    if not emitting.empty:
        st.subheader("Permitted NOx by project")
        st.caption(
            "Allowable emission rates as authorized, not measured emissions. "
            "Only case-by-case permits and registrations that publish a rate "
            "appear here."
        )
        top = emitting.nlargest(15, "nox_tpy").sort_values("nox_tpy")
        figure = px.bar(
            top, x="nox_tpy", y="project_name", orientation="h",
            color="lifecycle_label", color_discrete_map=LIFECYCLE_COLORS,
            category_orders={"project_name": top["project_name"].tolist(),
                             "lifecycle_label": LIFECYCLE_ORDER},
            text="nox_tpy",
        )
        figure.update_traces(
            texttemplate="%{text:,.1f}", textposition="outside", cliponaxis=False
        )
        figure.update_layout(
            height=max(320, 34 * len(top)),
            xaxis_title="Permitted NOx, tons per year", yaxis_title=None,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None),
            margin=dict(l=0, r=40, t=10, b=0),
        )
        st.plotly_chart(figure, use_container_width=True)


def render_puct(entities, counties=None):
    """PUCT Interchange dockets, and who is on the record in them.

    This tab exists because of a hole in the ERCOT data. ERCOT publishes its
    large-load queue in aggregate — MW by zone, no customer named — so the
    question the rest of this tool is built around ("which data center, sited
    where, next to which plant?") has no answer on the ERCOT side. PUCT dockets
    name the parties. A docket is a proceeding rather than a site, so nothing
    here is joined to a plant automatically; it is the name list that matters.
    """
    dockets = entities[entities["source"] == "puct"].copy()
    if counties and not dockets.empty:
        # Only the county filter carries over: fuel, MW and site type are
        # properties of a plant, and a proceeding has none of them.
        dockets = dockets[dockets["county"].isin(counties)]
        st.caption(f"Filtered to {', '.join(counties)}.")
    if dockets.empty:
        st.info(
            "No PUCT dockets yet. Pull them with:\n\n"
            "`python permits_cli.py puct --parties`"
        )
        return

    dockets["Docket type"] = (
        dockets["permit_type"].fillna("PUCT docket")
        .str.replace("^PUCT ", "", regex=True)
        # Only the first letter — .capitalize() would lowercase the CCN acronym.
        .str.replace("^(.)", lambda m: m.group(1).upper(), regex=True)
    )

    counts = dockets["Docket type"].value_counts()
    columns = st.columns(min(len(counts), 4) or 1)
    for column, (label, count) in zip(columns, counts.items()):
        column.metric(label, f"{count:,}")

    st.caption(
        "A docket is a proceeding, not a plant: it carries no capacity, and it "
        "is placed on the map only where the case style itself names a county."
    )

    parties = _puct_parties(dockets)
    left, right = st.columns([3, 2])

    with left:
        st.subheader("Dockets")
        chosen = st.multiselect(
            "Docket type", sorted(dockets["Docket type"].unique()),
            default=sorted(dockets["Docket type"].unique()),
            key="puct_types",
        )
        view = dockets[dockets["Docket type"].isin(chosen)] if chosen else dockets
        table = view[[
            "permit_number", "Docket type", "operator", "county",
            "received_date", "status_date", "project_name", "url",
        ]].rename(columns={
            "permit_number": "Control", "operator": "Filed by",
            "county": "County", "received_date": "First filed",
            "status_date": "Last filed", "project_name": "Case style",
            "url": "Interchange",
        }).sort_values("First filed", ascending=False, na_position="last")
        # A docket that names no county has none; "None" printed in the cell
        # reads as a value rather than as an absence.
        table["County"] = table["County"].fillna("—")
        st.dataframe(
            table, hide_index=True, use_container_width=True, height=430,
            column_config={
                "Interchange": st.column_config.LinkColumn(
                    "Interchange", display_text="open"
                ),
                "Case style": st.column_config.TextColumn("Case style", width="large"),
            },
        )

    with right:
        st.subheader("Who files in these dockets")
        if parties.empty:
            st.info(
                "No party lists stored. Re-run with `--parties` to open each "
                "docket's filing list."
            )
        else:
            st.caption(
                "Commission staff and the docket-management offices file in "
                "everything and are excluded. Names are printed as filed — "
                "Interchange records a joint filing as one party."
            )
            figure = px.bar(
                parties.head(20).sort_values("Dockets"),
                x="Dockets", y="Party", orientation="h",
            )
            figure.update_layout(
                height=520, yaxis_title=None, xaxis_title="Dockets appeared in",
                margin=dict(l=0, r=0, t=10, b=0),
            )
            figure.update_traces(marker_color="#7c9ce8")
            st.plotly_chart(figure, use_container_width=True)
            st.dataframe(parties, hide_index=True, use_container_width=True,
                         height=240)


def _puct_parties(dockets):
    """Party -> docket count, from the payloads stored by `puct --parties`."""
    counts = {}
    for payload in dockets["raw_json"].dropna():
        try:
            names = json.loads(payload).get("parties") or []
        except (ValueError, TypeError):
            continue
        if isinstance(names, str):
            names = [part.strip() for part in names.split(";")]
        for name in {n.strip() for n in names if n and n.strip()}:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return pd.DataFrame(columns=["Party", "Dockets"])
    return (
        pd.DataFrame(sorted(counts.items(), key=lambda kv: -kv[1]),
                     columns=["Party", "Dockets"])
    )


def render_site_detail(sites, entities, members, links):
    st.subheader("Site detail")
    if sites.empty:
        st.info("No sites in the current filter.")
        return

    options = sites.sort_values("total_mw", ascending=False)
    labels = {
        row["site_id"]: f"{row['site_name']} — {row['county'] or 'unknown county'} "
                        f"({SITE_CLASS_LABELS.get(row['site_class'], row['site_class'])})"
        for _, row in options.iterrows()
    }
    site_id = st.selectbox(
        "Site", list(labels), format_func=lambda key: labels[key]
    )
    site = sites[sites["site_id"] == site_id].iloc[0]

    columns = st.columns(4)
    columns[0].metric("Permitted generation", mw(site["gen_mw"]))
    columns[1].metric("Requested load", mw(site["load_mw"]))
    columns[2].metric("Records joined", f"{int(site['n_members'])}")
    columns[3].metric("Match confidence", f"{site['confidence']:.2f}")

    st.markdown(
        f"**Operator:** {site['operator'] or '—'} · **County:** {site['county'] or '—'} "
        f"· **Permitted fuel:** {site['fuel_labels'] or '—'} "
        f"· **Permit state:** {site['lifecycle_label']} "
        f"· **Geolocation:** {site['geo_precision']}"
    )

    member_ids = set(members[members["site_id"] == site_id]["entity_id"])
    detail = entities[entities["entity_id"].isin(member_ids)]

    st.markdown("**Member records**")
    st.dataframe(
        detail[[
            "source_label", "project_name", "operator", "kind_label", "fuel_label",
            "capacity_mw", "load_mw", "acres", "lifecycle_label", "program_label",
            "stage_label", "status", "permit_number", "kind_evidence",
        ]].rename(columns={
            "source_label": "Feed", "project_name": "Record", "operator": "Operator",
            "kind_label": "Classified as", "fuel_label": "Fuel",
            "capacity_mw": "Gen MW", "load_mw": "Load MW", "acres": "Acres",
            "lifecycle_label": "State", "program_label": "Program",
            "stage_label": "Stage", "status": "Agency status",
            "permit_number": "Permit / INR",
            "kind_evidence": "Classification evidence",
        }),
        use_container_width=True, hide_index=True,
    )

    evidence = links[
        links["entity_a"].isin(member_ids) & links["entity_b"].isin(member_ids)
    ]
    st.markdown("**Why these records were joined**")
    if evidence.empty:
        st.caption("Single-record site — nothing was joined.")
    else:
        names = entities.set_index("entity_id")["project_name"].to_dict()
        shown = evidence.copy()
        shown["Record A"] = shown["entity_a"].map(names)
        shown["Record B"] = shown["entity_b"].map(names)
        st.dataframe(
            shown[[
                "Record A", "Record B", "score", "method", "distance_km",
                "name_score", "operator_score",
            ]].rename(columns={
                "score": "Score", "method": "Evidence", "distance_km": "Distance (km)",
                "name_score": "Name similarity", "operator_score": "Operator similarity",
            }).sort_values("Score", ascending=False),
            use_container_width=True, hide_index=True,
        )


def render_records(entities):
    st.subheader("All records")
    st.caption(
        "Every ingested row, before site resolution. `raw_json` keeps the "
        "untouched source record."
    )
    kinds = sorted(entities["kind_label"].unique().tolist())
    selected = st.multiselect("Classified as", kinds, default=kinds)
    sources = sorted(entities["source_label"].unique().tolist())
    selected_sources = st.multiselect("Feed", sources, default=sources)
    states = [
        label for label in LIFECYCLE_ORDER
        if label in set(entities["lifecycle_label"])
    ]
    selected_states = st.multiselect("Permit state", states, default=states)

    view = entities[
        entities["kind_label"].isin(selected)
        & entities["source_label"].isin(selected_sources)
        & entities["lifecycle_label"].isin(selected_states)
    ]
    st.dataframe(
        view[[
            "source_label", "project_name", "operator", "county", "kind_label",
            "kind_confidence", "fuel_label", "capacity_mw", "load_mw", "acres",
            "lifecycle_label", "program_label", "stage_label", "days_sitting",
            "status", "received_date", "decision_date", "projected_cod",
            "permit_number", "regulated_entity", "geo_precision", "kind_evidence",
        ]].rename(columns={
            "source_label": "Feed", "project_name": "Record", "operator": "Operator",
            "county": "County", "kind_label": "Classified as",
            "kind_confidence": "Confidence", "fuel_label": "Fuel",
            "capacity_mw": "Gen MW", "load_mw": "Load MW", "acres": "Acres",
            "lifecycle_label": "State", "program_label": "Program",
            "stage_label": "Stage", "days_sitting": "Days sitting",
            "status": "Agency status", "received_date": "Filed",
            "decision_date": "Decided", "projected_cod": "Projected date",
            "permit_number": "Permit / INR", "regulated_entity": "TCEQ RN",
            "geo_precision": "Geolocation", "kind_evidence": "Classification evidence",
        }),
        use_container_width=True, hide_index=True,
    )
    st.download_button(
        "Download filtered records (CSV)",
        view.to_csv(index=False).encode("utf-8"),
        file_name="permit_records.csv",
        mime="text/csv",
    )


# --- Main --------------------------------------------------------------------

def main():
    if not os.path.exists(DB_PATH):
        render_empty_state()
        return

    sites, entities, members, links, info = load_all(DB_PATH, db_mtime(DB_PATH))
    if sites.empty:
        render_empty_state()
        return

    st.title("⚡ TCEQ / ERCOT Permit Intelligence")
    st.caption(
        "ERCOT generation queue · ERCOT large load queue · TCEQ air NSR permits · "
        "TCEQ stormwater construction NOIs · PUCT Interchange dockets"
    )

    if info.get("is_demo"):
        st.info(
            "This database contains the **synthetic demo dataset** — invented "
            "projects, companies and permit numbers. Rebuild from real exports "
            "with `python permits_cli.py ingest --dir data/raw`.",
            icon="🧪",
        )

    (filtered_sites, lifecycles, programs, offline_map,
     selected_counties) = sidebar_filters(sites, info)

    member_ids = set(
        members[members["site_id"].isin(set(filtered_sites["site_id"]))]["entity_id"]
    )
    filtered_entities = entities[entities["entity_id"].isin(member_ids)]
    # Lifecycle and program filter records, not sites: a site can hold an issued
    # permit and a pending amendment at once, and dropping the whole site would
    # hide the half that matched.
    if lifecycles:
        filtered_entities = filtered_entities[
            filtered_entities["lifecycle_label"].isin(lifecycles)
        ]
    if programs:
        filtered_entities = filtered_entities[
            filtered_entities["program_label"].isin(programs)
        ]
    if lifecycles or programs:
        kept_sites = set(
            members[members["entity_id"].isin(set(filtered_entities["entity_id"]))]["site_id"]
        )
        filtered_sites = filtered_sites[filtered_sites["site_id"].isin(kept_sites)]

    render_headline(filtered_sites, filtered_entities)
    st.divider()

    tabs = st.tabs([
        "Map", "Colocated gen + load", "Generation", "Loads",
        "Permits & applications", "PUCT dockets", "Site detail", "All records",
    ])
    with tabs[0]:
        render_map(filtered_entities, offline_map)
    with tabs[1]:
        render_colocated(filtered_sites)
    with tabs[2]:
        render_generation(filtered_entities, members)
    with tabs[3]:
        render_loads(filtered_entities)
    with tabs[4]:
        render_permits(filtered_entities)
    with tabs[5]:
        # Dockets deliberately have no site, so the site-shaped filters cannot
        # reach them; this tab reads the unfiltered frame and filters itself.
        render_puct(entities, selected_counties)
    with tabs[6]:
        render_site_detail(filtered_sites, filtered_entities, members, links)
    with tabs[7]:
        render_records(filtered_entities)


main()
