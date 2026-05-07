"""
VIIRS Nightfire Gas Flare Dashboard
Interactive analysis of detected gas flares across CONUS oil & gas basins.
Run: streamlit run dashboard.py
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, date, timedelta
import os
import sys

# Add project root to path for imports
sys.path.insert(0, os.path.dirname(__file__))

st.set_page_config(
    page_title="VIIRS Nightfire - CONUS Oil & Gas",
    page_icon="🔥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Data Loading ---
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')
CSV_PATH = os.path.join(OUTPUT_DIR, 'nightfire_detections.csv')


def load_data():
    if not os.path.exists(CSV_PATH):
        return pd.DataFrame()
    df = pd.read_csv(CSV_PATH, on_bad_lines='skip')
    df['datetime_utc'] = pd.to_datetime(df['datetime_utc'])
    df['date'] = df['datetime_utc'].dt.date
    df['hour_utc'] = df['datetime_utc'].dt.hour
    # Classify basin from lat/lon (handles old CSVs without basin column)
    from src.reader import classify_basin
    if 'basin' not in df.columns:
        df['basin'] = classify_basin(df['latitude'].values, df['longitude'].values)
    else:
        missing = df['basin'].isna()
        if missing.any():
            df.loc[missing, 'basin'] = classify_basin(
                df.loc[missing, 'latitude'].values,
                df.loc[missing, 'longitude'].values,
            )
    return df


# Use session state to track when data should be reloaded
if 'data_version' not in st.session_state:
    st.session_state.data_version = 0

df = load_data()

# --- Sidebar: Fetch New Data ---
st.sidebar.title("Fetch Data")

# Check for token
token_available = False
try:
    from src.fetcher import load_token
    _token = load_token()
    token_available = True
except Exception as e:
    st.sidebar.error(f"Token load failed: {e}")

if token_available:
    fetch_col1, fetch_col2 = st.sidebar.columns(2)
    with fetch_col1:
        fetch_start = st.date_input("From", value=date.today() - timedelta(days=7))
    with fetch_col2:
        fetch_end = st.date_input("To", value=date.today() - timedelta(days=1))

    if st.sidebar.button("Fetch & Process", type="primary", use_container_width=True):
        from src.fetcher import fetch_and_process
        status = st.sidebar.empty()
        progress = st.sidebar.progress(0)

        def update_progress(current, total, message):
            if total > 0:
                progress.progress(current / total, text=message)
            else:
                status.text(message)

        status.text("Searching LAADS DAAC...")
        try:
            new_df = fetch_and_process(
                fetch_start, fetch_end,
                output_dir=OUTPUT_DIR,
                verbose=False,
                progress_callback=update_progress,
            )
            progress.progress(1.0, text="Complete!")
            if len(new_df) > 0:
                n_flares = (new_df['classification'] == 'gas_flare').sum()
                status.success(f"Added {len(new_df)} detections ({n_flares} flares)")
                st.session_state.data_version += 1
                st.cache_data.clear()
                st.rerun()
            else:
                status.info("No new data found (already processed or no nighttime passes)")
        except Exception as e:
            status.error(f"Error: {e}")

    # Show processed count
    try:
        from src.fetcher import load_processed_log
        processed = load_processed_log(OUTPUT_DIR)
        st.sidebar.caption(f"{len(processed)} granules processed")
    except Exception:
        pass
else:
    st.sidebar.warning(
        "Set LAADS_TOKEN in .env to enable auto-fetching.\n\n"
        "[Get a token](https://ladsweb.modaps.eosdis.nasa.gov/profiles/#generate-token-modal)"
    )

st.sidebar.divider()

# --- Sidebar Filters ---
st.sidebar.title("Filters")

# Handle empty data
if len(df) == 0:
    st.title("VIIRS Nightfire Gas Flare Analysis")
    st.markdown("**CONUS Oil & Gas Basins** | NOAA-20 VIIRS Level 1B Satellite Data")
    st.info("No detection data yet. Use **Fetch Data** in the sidebar to download and process satellite passes.")
    st.stop()

# Date filter
dates = sorted(df['date'].unique())
date_range = st.sidebar.select_slider(
    "Date Range",
    options=dates,
    value=(dates[0], dates[-1]),
)
mask = (df['date'] >= date_range[0]) & (df['date'] <= date_range[1])

# Classification filter
classifications = st.sidebar.multiselect(
    "Classification",
    options=df['classification'].unique().tolist(),
    default=df['classification'].unique().tolist(),
)
mask &= df['classification'].isin(classifications)

# Temperature range
temp_min, temp_max = float(df['temperature_K'].min()), float(df['temperature_K'].max())
temp_range = st.sidebar.slider(
    "Temperature (K)",
    min_value=int(temp_min),
    max_value=int(temp_max),
    value=(int(temp_min), int(temp_max)),
)
mask &= (df['temperature_K'] >= temp_range[0]) & (df['temperature_K'] <= temp_range[1])

# Radiant heat filter
rh_max = float(df['radiant_heat_MW'].max())
rh_range = st.sidebar.slider(
    "Radiant Heat (MW)",
    min_value=0.0,
    max_value=min(rh_max, 20.0),
    value=(0.0, min(rh_max, 20.0)),
    step=0.1,
)
mask &= (df['radiant_heat_MW'] >= rh_range[0]) & (df['radiant_heat_MW'] <= rh_range[1])

# Basin filter
basin_options = sorted(df['basin'].unique().tolist())
basin_filter = st.sidebar.multiselect(
    "Basin",
    options=basin_options,
    default=basin_options,
)
mask &= df['basin'].isin(basin_filter)

# Compute persistence before applying persistence filter
pre_filtered = df[mask].copy()
n_nights = pre_filtered['date'].nunique()
pre_filtered['site_lat'] = (pre_filtered['latitude'] / 0.0075).round() * 0.0075
pre_filtered['site_lon'] = (pre_filtered['longitude'] / 0.0075).round() * 0.0075
pre_filtered['site_key'] = pre_filtered['site_lat'].astype(str) + ',' + pre_filtered['site_lon'].astype(str)
site_days = pre_filtered.groupby('site_key')['date'].nunique().rename('days_seen')
pre_filtered = pre_filtered.merge(site_days, on='site_key', how='left')
pre_filtered['persistence'] = (pre_filtered['days_seen'] / max(n_nights, 1) * 100).round(1)

# Persistence filter
persist_range = st.sidebar.slider(
    "Persistence (%)",
    min_value=0, max_value=100,
    value=(0, 100),
    help="% of nights in date range a site was detected. 100% = every night.",
)
filtered = pre_filtered[
    (pre_filtered['persistence'] >= persist_range[0])
    & (pre_filtered['persistence'] <= persist_range[1])
].copy()

# --- Header ---
st.title("VIIRS Nightfire Gas Flare Analysis")
st.markdown("**CONUS Oil & Gas Basins** | NOAA-20 VIIRS Level 1B Satellite Data")

# --- Key Metrics ---
col1, col2, col3, col4, col5 = st.columns(5)

n_flares = (filtered['classification'] == 'gas_flare').sum()
total_rh = filtered['radiant_heat_MW'].sum()
mean_temp = filtered['temperature_K'].mean()
# Avg daily flaring rate: total RH / days, converted to MMscf/d
# 1 BCM = 35,314.667 MMscf; calibration_slope default = 0.029353 BCM per MW-sum
BCM_TO_MMSCF = 35_314.667
default_cal_slope = 0.029353
avg_mmscfd = (default_cal_slope * total_rh / max(n_nights, 1)) * BCM_TO_MMSCF

col1.metric("Total Detections", f"{len(filtered):,}")
col2.metric("Gas Flares", f"{n_flares:,}")
col3.metric("Total Radiant Heat", f"{total_rh:.1f} MW")
col4.metric("Mean Temperature", f"{mean_temp:.0f} K")
col5.metric("Avg Flaring Rate", f"{avg_mmscfd:,.0f} MMscf/d")

st.divider()

# --- Row 1: Map + Summary ---
map_col, summary_col = st.columns([3, 1])

with map_col:
    st.subheader("Flare Detection Map")

    # Auto-center and auto-zoom based on filtered data extent
    map_center = {'lat': filtered['latitude'].mean(), 'lon': filtered['longitude'].mean()}
    lat_span = filtered['latitude'].max() - filtered['latitude'].min()
    lon_span = filtered['longitude'].max() - filtered['longitude'].min()
    span = max(lat_span, lon_span)
    auto_zoom = 7 if span < 3 else 6 if span < 8 else 5 if span < 15 else 3

    map_hover = ['temperature_K', 'radiant_heat_MW', 'source_area_m2',
                  'classification', 'basin', 'persistence']

    map_controls_l, map_controls_r = st.columns([4, 1])
    with map_controls_l:
        color_by = st.radio(
            "Color by", ["Temperature (K)", "Radiant Heat (MW)", "Persistence",
                          "Classification", "Basin", "Date"],
            horizontal=True, label_visibility="collapsed",
        )
    with map_controls_r:
        animate = st.checkbox("Animate", help="Step through days with play/pause controls")
        export_gif = st.button("Export GIF", disabled=not animate,
                               help="Render each day as a frame and download as animated GIF")

    # Prepare date_str column for animation frames
    filtered['date_str'] = filtered['date'].astype(str)

    # For animation: fill in every date in the filter range so no days are skipped,
    # and fix size scale to the full data range so bubbles don't jump between frames.
    if animate:
        from datetime import timedelta as _td
        all_range_dates = []
        d = date_range[0]
        while d <= date_range[1]:
            all_range_dates.append(str(d))
            d += _td(days=1)
        # Add placeholder rows for dates with no detections
        existing_dates = set(filtered['date_str'].unique())
        missing_dates = [d for d in all_range_dates if d not in existing_dates]
        if missing_dates:
            placeholder = pd.DataFrame({
                'date_str': missing_dates,
                'latitude': [map_center['lat']] * len(missing_dates),
                'longitude': [map_center['lon']] * len(missing_dates),
                'radiant_heat_MW': [0.0] * len(missing_dates),
                'temperature_K': [np.nan] * len(missing_dates),
                'persistence': [0.0] * len(missing_dates),
                'source_area_m2': [0.0] * len(missing_dates),
                'classification': [''] * len(missing_dates),
                'basin': [''] * len(missing_dates),
            })
            filtered = pd.concat([filtered, placeholder], ignore_index=True)
        # Sort so animation frames are in chronological order
        filtered = filtered.sort_values('date_str')

    map_common = dict(
        lat='latitude', lon='longitude',
        size='radiant_heat_MW', size_max=18,
        zoom=auto_zoom, center=map_center, opacity=0.8,
    )

    if animate:
        map_common['animation_frame'] = 'date_str'

    if color_by == "Temperature (K)":
        fig_map = px.scatter_mapbox(
            filtered, **map_common,
            color='temperature_K',
            color_continuous_scale='Inferno',
            range_color=[1400, 2500],
            hover_data=map_hover,
        )
        fig_map.update_layout(coloraxis_colorbar_title="Temp (K)")
    elif color_by == "Radiant Heat (MW)":
        rh_range_max = max(filtered['radiant_heat_MW'].quantile(0.98), 1.0)
        fig_map = px.scatter_mapbox(
            filtered, **map_common,
            color='radiant_heat_MW',
            color_continuous_scale='YlOrRd',
            range_color=[0, rh_range_max],
            hover_data=map_hover,
        )
        fig_map.update_layout(coloraxis_colorbar_title="RH (MW)")
    elif color_by == "Persistence":
        fig_map = px.scatter_mapbox(
            filtered, **map_common,
            color='persistence',
            color_continuous_scale='Viridis',
            range_color=[0, 100],
            hover_data=map_hover,
        )
        fig_map.update_layout(coloraxis_colorbar_title="Persistence %")
    elif color_by == "Classification":
        class_color_map = {
            'gas_flare': '#ff4444',
            'industrial_or_large_fire': '#ff8800',
            'biomass_burning': '#ffcc00',
            'smoldering': '#886600',
            'low_temperature_anomaly': '#666666',
            'fit_failed': '#cccccc',
        }
        fig_map = px.scatter_mapbox(
            filtered, **map_common,
            color='classification',
            color_discrete_map=class_color_map,
            hover_data=map_hover,
        )
    elif color_by == "Basin":
        basin_colors = {
            'Permian': '#ff4444', 'Eagle Ford': '#00cc88', 'Bakken': '#ff8800',
            'Other': '#ffcc44', 'Appalachian': '#66bbff', 'Marcellus/Utica': '#ff66aa',
            'Anadarko/SCOOP/STACK': '#4466cc', 'Haynesville': '#44ddaa',
            'DJ/Niobrara': '#dd66ff', 'Powder River': '#88cc44',
            'Williston': '#cc8844', 'Uinta': '#44cccc', 'San Juan': '#aa8866',
        }
        fig_map = px.scatter_mapbox(
            filtered, **map_common,
            color='basin',
            color_discrete_map=basin_colors,
            hover_data=map_hover,
        )
        fig_map.update_layout(legend_title="Basin")
    else:
        fig_map = px.scatter_mapbox(
            filtered, **map_common,
            color='date_str',
            hover_data=map_hover,
        )
        fig_map.update_layout(legend_title="Date")

    map_height = 600 if animate else 550
    fig_map.update_layout(
        mapbox_style="carto-darkmatter",
        height=map_height,
        margin=dict(l=0, r=0, t=0, b=0),
    )
    if animate:
        fig_map.layout.updatemenus[0].buttons[0].args[1]['frame']['duration'] = 800
        fig_map.layout.updatemenus[0].buttons[0].args[1]['transition']['duration'] = 300
        fig_map.layout.sliders[0].currentvalue = dict(prefix="Date: ", font_size=14)
    st.plotly_chart(fig_map, use_container_width=True)

    # --- GIF Export ---
    if animate and export_gif:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.colors import Normalize
        from PIL import Image
        import io
        import cartopy.crs as ccrs
        import cartopy.feature as cfeature

        gif_status = st.empty()
        gif_progress = st.progress(0)

        # Use the real detections (exclude placeholder rows added for Plotly animation)
        gif_data = filtered[filtered['radiant_heat_MW'] > 0].copy()

        # Build list of ALL dates in the filter range
        from datetime import timedelta as _gif_td
        all_gif_dates = []
        d_cursor = date_range[0]
        while d_cursor <= date_range[1]:
            all_gif_dates.append(d_cursor)
            d_cursor += _gif_td(days=1)

        # Fixed spatial extent from full dataset
        lat_min_g, lat_max_g = gif_data['latitude'].min(), gif_data['latitude'].max()
        lon_min_g, lon_max_g = gif_data['longitude'].min(), gif_data['longitude'].max()
        lat_pad = max((lat_max_g - lat_min_g) * 0.08, 0.5)
        lon_pad = max((lon_max_g - lon_min_g) * 0.08, 0.5)

        # Fixed size scale from full dataset
        size_max_val = max(gif_data['radiant_heat_MW'].quantile(0.98), 1.0)

        # Fixed color scale from full dataset
        if color_by in ("Temperature (K)", "Persistence", "Radiant Heat (MW)"):
            if color_by == "Temperature (K)":
                c_col, cmap, vmin, vmax, clabel = 'temperature_K', 'inferno', 1400, 2500, 'Temp (K)'
            elif color_by == "Persistence":
                c_col, cmap, vmin, vmax, clabel = 'persistence', 'viridis', 0, 100, 'Persistence %'
            else:
                c_col, cmap, vmin, vmax, clabel = 'radiant_heat_MW', 'YlOrRd', 0, float(size_max_val), 'RH (MW)'
            norm = Normalize(vmin=vmin, vmax=vmax)
        else:
            c_col = None

        frames = []
        proj = ccrs.PlateCarree()
        for i, d in enumerate(all_gif_dates):
            gif_status.text(f"Rendering frame {i+1}/{len(all_gif_dates)} ({d})...")
            gif_progress.progress((i + 1) / len(all_gif_dates))
            day_df = gif_data[gif_data['date'] == d]

            fig_g, ax_g = plt.subplots(figsize=(10, 6), dpi=120,
                                        subplot_kw={'projection': proj})
            ax_g.set_facecolor('#1a1a2e')
            fig_g.patch.set_facecolor('#0d1117')

            # Map features: state borders, coastlines, country borders
            ax_g.add_feature(cfeature.STATES.with_scale('50m'),
                             edgecolor='#555555', linewidth=0.5, facecolor='none')
            ax_g.add_feature(cfeature.COASTLINE.with_scale('50m'),
                             edgecolor='#666666', linewidth=0.6)
            ax_g.add_feature(cfeature.BORDERS.with_scale('50m'),
                             edgecolor='#666666', linewidth=0.6)
            ax_g.add_feature(cfeature.LAND.with_scale('50m'),
                             facecolor='#1a1a2e')
            ax_g.add_feature(cfeature.OCEAN.with_scale('50m'),
                             facecolor='#0d1117')
            ax_g.add_feature(cfeature.LAKES.with_scale('50m'),
                             facecolor='#0d1117', edgecolor='#555555', linewidth=0.3)

            n_det = len(day_df)
            if n_det > 0:
                sizes = (day_df['radiant_heat_MW'] / size_max_val).clip(0.02, 1.0) * 80

                if c_col:
                    ax_g.scatter(
                        day_df['longitude'].values, day_df['latitude'].values,
                        c=day_df[c_col].values, s=sizes.values, cmap=cmap, norm=norm,
                        alpha=0.85, edgecolors='white', linewidth=0.3,
                        transform=proj, zorder=5,
                    )
                else:
                    ax_g.scatter(
                        day_df['longitude'].values, day_df['latitude'].values,
                        c='#ff4444', s=sizes.values,
                        alpha=0.85, edgecolors='white', linewidth=0.3,
                        transform=proj, zorder=5,
                    )

            # Fixed colorbar
            if c_col:
                import matplotlib.cm as cm
                sm = cm.ScalarMappable(cmap=cmap, norm=norm)
                sm.set_array([])
                cb = plt.colorbar(sm, ax=ax_g, shrink=0.7, pad=0.02)
                cb.set_label(clabel, color='white', fontsize=9)
                cb.ax.yaxis.set_tick_params(color='white')
                plt.setp(cb.ax.yaxis.get_ticklabels(), color='white', fontsize=8)

            ax_g.set_extent([lon_min_g - lon_pad, lon_max_g + lon_pad,
                             lat_min_g - lat_pad, lat_max_g + lat_pad], crs=proj)
            ax_g.set_title(f"{d}  |  {n_det} detections", color='white', fontsize=12, pad=10)
            # Style gridlines
            gl = ax_g.gridlines(draw_labels=True, linewidth=0.3, color='#333',
                                alpha=0.5, linestyle='--')
            gl.top_labels = False
            gl.right_labels = False
            gl.xlabel_style = {'color': 'white', 'fontsize': 8}
            gl.ylabel_style = {'color': 'white', 'fontsize': 8}

            buf = io.BytesIO()
            fig_g.savefig(buf, format='png', bbox_inches='tight', facecolor=fig_g.get_facecolor())
            plt.close(fig_g)
            buf.seek(0)
            frames.append(Image.open(buf).copy())

        # Stitch into GIF
        gif_buf = io.BytesIO()
        frames[0].save(
            gif_buf, format='GIF', save_all=True, append_images=frames[1:],
            duration=800, loop=0, optimize=True,
        )
        gif_buf.seek(0)
        gif_size_mb = len(gif_buf.getvalue()) / 1024 / 1024

        gif_progress.progress(1.0)
        gif_status.success(f"GIF ready ({gif_size_mb:.1f} MB, {len(frames)} frames)")
        st.download_button(
            f"Download GIF ({gif_size_mb:.1f} MB)",
            data=gif_buf.getvalue(),
            file_name=f"nightfire_{all_gif_dates[0]}_{all_gif_dates[-1]}.gif",
            mime="image/gif",
        )

with summary_col:
    st.subheader("Summary by Date")
    daily = filtered.groupby('date').agg(
        detections=('temperature_K', 'count'),
        flares=('classification', lambda x: (x == 'gas_flare').sum()),
        total_rh_MW=('radiant_heat_MW', 'sum'),
        mean_temp_K=('temperature_K', 'mean'),
    ).round(1)
    st.dataframe(daily, use_container_width=True, height=250)

    st.subheader("By Basin")
    basin_stats = filtered.groupby('basin').agg(
        detections=('temperature_K', 'count'),
        total_rh_MW=('radiant_heat_MW', 'sum'),
        mean_temp_K=('temperature_K', 'mean'),
    ).round(1).sort_values('total_rh_MW', ascending=False)
    st.dataframe(basin_stats, use_container_width=True)

    st.subheader("Classification")
    class_counts = filtered['classification'].value_counts()
    fig_pie = px.pie(
        values=class_counts.values,
        names=class_counts.index,
        color=class_counts.index,
        color_discrete_map={
            'gas_flare': '#ff4444',
            'industrial_or_large_fire': '#ff8800',
            'biomass_burning': '#ffcc00',
        },
    )
    fig_pie.update_layout(height=200, margin=dict(l=0, r=0, t=0, b=0), showlegend=False)
    st.plotly_chart(fig_pie, use_container_width=True)

st.divider()

# --- Row 2: Distribution Charts ---
st.subheader("Distribution Analysis")
dist_col1, dist_col2, dist_col3 = st.columns(3)

with dist_col1:
    fig_temp = px.histogram(
        filtered, x='temperature_K', nbins=40,
        color='classification',
        color_discrete_map={
            'gas_flare': '#ff4444',
            'industrial_or_large_fire': '#ff8800',
            'biomass_burning': '#ffcc00',
        },
        title="Temperature Distribution",
        labels={'temperature_K': 'Temperature (K)', 'count': 'Count'},
    )
    fig_temp.update_layout(height=350, showlegend=False)
    st.plotly_chart(fig_temp, use_container_width=True)

with dist_col2:
    fig_rh = px.histogram(
        filtered[filtered['radiant_heat_MW'] < 5],
        x='radiant_heat_MW', nbins=40,
        color='basin',
        title="Radiant Heat Distribution (< 5 MW)",
        labels={'radiant_heat_MW': 'Radiant Heat (MW)', 'count': 'Count'},
    )
    fig_rh.update_layout(height=350)
    st.plotly_chart(fig_rh, use_container_width=True)

with dist_col3:
    fig_area = px.histogram(
        filtered[filtered['source_area_m2'] < 10],
        x='source_area_m2', nbins=40,
        title="Source Area Distribution (< 10 m\u00b2)",
        labels={'source_area_m2': 'Source Area (m\u00b2)', 'count': 'Count'},
        color_discrete_sequence=['#ff6644'],
    )
    fig_area.update_layout(height=350)
    st.plotly_chart(fig_area, use_container_width=True)

st.divider()

# --- Row 3: Time Series + Scatter ---
st.subheader("Temporal & Correlation Analysis")
ts_col, scatter_col = st.columns(2)

with ts_col:
    daily_ts = filtered.groupby('date').agg(
        total_rh_MW=('radiant_heat_MW', 'sum'),
        n_flares=('classification', lambda x: (x == 'gas_flare').sum()),
        mean_temp_K=('temperature_K', 'mean'),
    ).reset_index()

    fig_ts = make_subplots(specs=[[{"secondary_y": True}]])
    fig_ts.add_trace(
        go.Bar(x=daily_ts['date'], y=daily_ts['total_rh_MW'],
               name='Total RH (MW)', marker_color='#ff4444', opacity=0.7),
        secondary_y=False,
    )
    fig_ts.add_trace(
        go.Scatter(x=daily_ts['date'], y=daily_ts['n_flares'],
                   name='Flare Count', mode='lines+markers',
                   line=dict(color='#44aaff', width=2)),
        secondary_y=True,
    )
    fig_ts.update_layout(
        title="Daily Flaring Activity",
        height=400,
        yaxis_title="Total Radiant Heat (MW)",
        yaxis2_title="Flare Count",
    )
    st.plotly_chart(fig_ts, use_container_width=True)

with scatter_col:
    fig_scatter = px.scatter(
        filtered,
        x='temperature_K', y='radiant_heat_MW',
        color='basin',
        size='source_area_m2',
        size_max=15,
        hover_data=['latitude', 'longitude', 'datetime_utc'],
        title="Temperature vs Radiant Heat",
        labels={'temperature_K': 'Temperature (K)', 'radiant_heat_MW': 'Radiant Heat (MW)'},
        opacity=0.6,
    )
    fig_scatter.update_layout(height=400)
    st.plotly_chart(fig_scatter, use_container_width=True)

st.divider()

# --- Row 4: Volume Estimation ---
st.subheader("Flared Gas Volume Estimation")
vol_col1, vol_col2 = st.columns([2, 1])

with vol_col1:
    vol_controls_l, vol_controls_r = st.columns([2, 3])
    with vol_controls_l:
        calibration_slope = st.slider(
            "Calibration slope (BCM/MW-sum)",
            min_value=0.01, max_value=0.06, value=0.029353, step=0.001,
            help="EOG global calibration: 0.029353 BCM/MW-sum. "
                 "This constant is published in BCM; all displays are converted to MMscf/d.",
        )
    with vol_controls_r:
        vol_color_by = st.radio(
            "Color by", ["None", "Basin", "Classification"],
            horizontal=True, key="vol_color",
        )

    # Explicit colors for all 13 basins + Other so nothing gets lost
    basin_colors = {
        'Permian': '#ff4444',
        'Eagle Ford': '#00cc88',
        'Bakken': '#ff8800',
        'Other': '#ffcc44',
        'Appalachian': '#66bbff',
        'Marcellus/Utica': '#ff66aa',
        'Anadarko/SCOOP/STACK': '#4466cc',
        'Haynesville': '#44ddaa',
        'DJ/Niobrara': '#dd66ff',
        'Powder River': '#88cc44',
        'Williston': '#cc8844',
        'Uinta': '#44cccc',
        'San Juan': '#aa8866',
    }

    if vol_color_by == "Basin":
        daily_vol = filtered.groupby(['date', 'basin']).agg(
            total_rh_MW=('radiant_heat_MW', 'sum'),
        ).reset_index()
        daily_vol['mmscfd'] = daily_vol['total_rh_MW'] * calibration_slope * BCM_TO_MMSCF
        fig_vol = px.bar(
            daily_vol, x='date', y='mmscfd', color='basin',
            color_discrete_map=basin_colors,
            category_orders={'basin': list(basin_colors.keys())},
            title="Estimated Daily Flaring Rate",
            labels={'date': 'Date', 'mmscfd': 'Flaring Rate (MMscf/d)'},
        )
    elif vol_color_by == "Classification":
        daily_vol = filtered.groupby(['date', 'classification']).agg(
            total_rh_MW=('radiant_heat_MW', 'sum'),
        ).reset_index()
        daily_vol['mmscfd'] = daily_vol['total_rh_MW'] * calibration_slope * BCM_TO_MMSCF
        class_colors = {
            'gas_flare': '#ff4444',
            'industrial_or_large_fire': '#ff8800',
            'biomass_burning': '#ffcc00',
            'smoldering': '#886600',
            'low_temperature_anomaly': '#666666',
            'fit_failed': '#cccccc',
        }
        fig_vol = px.bar(
            daily_vol, x='date', y='mmscfd', color='classification',
            color_discrete_map=class_colors,
            title="Estimated Daily Flaring Rate",
            labels={'date': 'Date', 'mmscfd': 'Flaring Rate (MMscf/d)'},
        )
    else:
        daily_vol = filtered.groupby('date').agg(
            total_rh_MW=('radiant_heat_MW', 'sum'),
        ).reset_index()
        daily_vol['mmscfd'] = daily_vol['total_rh_MW'] * calibration_slope * BCM_TO_MMSCF
        fig_vol = px.bar(
            daily_vol, x='date', y='mmscfd',
            title="Estimated Daily Flaring Rate",
            labels={'date': 'Date', 'mmscfd': 'Flaring Rate (MMscf/d)'},
            color_discrete_sequence=['#ff8844'],
        )

    fig_vol.update_layout(height=350, barmode='stack')
    st.plotly_chart(fig_vol, use_container_width=True)

with vol_col2:
    total_rh_sum = filtered['radiant_heat_MW'].sum()
    avg_daily_rh = total_rh_sum / max(n_nights, 1)
    avg_mmscfd_cal = avg_daily_rh * calibration_slope * BCM_TO_MMSCF
    # Compute peak from per-day totals (regardless of color grouping)
    daily_totals = filtered.groupby('date')['radiant_heat_MW'].sum()
    peak_day_rh = daily_totals.max() if len(daily_totals) > 0 else 0
    peak_mmscfd = peak_day_rh * calibration_slope * BCM_TO_MMSCF

    st.metric("Total Radiant Heat Sum", f"{total_rh_sum:,.1f} MW")
    st.metric("Avg Flaring Rate", f"{avg_mmscfd_cal:,.0f} MMscf/d")
    st.metric("Peak Day Rate", f"{peak_mmscfd:,.0f} MMscf/d")
    st.metric("Days Observed", f"{n_nights}")
    if n_nights > 1:
        annual_bcf = avg_mmscfd_cal * 365 / 1000  # MMscf/d * 365 / 1000 = Bcf/yr
        st.metric("Annualized Est.", f"{annual_bcf:,.0f} Bcf/yr")

st.divider()

# --- Row 5: Brightest Flares Table ---
st.subheader("Top Flare Detections")
top_n = st.slider("Show top N", 10, 100, 25)
top_flares = filtered.nlargest(top_n, 'radiant_heat_MW')[
    ['datetime_utc', 'latitude', 'longitude', 'temperature_K',
     'radiant_heat_MW', 'source_area_m2', 'classification', 'basin',
     'radiance_M10', 'radiance_M11', 'n_bands_used']
].reset_index(drop=True)
top_flares.index += 1
st.dataframe(top_flares, use_container_width=True, height=400)

# --- Sidebar Info ---
st.sidebar.divider()
st.sidebar.markdown("### About")
st.sidebar.markdown(
    "Analyzes VIIRS Nightfire detections from NOAA-20 satellite passes "
    "over CONUS oil & gas producing basins. Data is fetched from NASA "
    "LAADS DAAC, processed through Planck curve fitting, then raw files "
    "are deleted to save disk space."
)
st.sidebar.markdown(f"**Data period:** {dates[0]} to {dates[-1]}")
st.sidebar.markdown(f"**Total detections:** {len(df):,}")
st.sidebar.markdown(f"**Granules processed:** {df['granule'].nunique()}")
