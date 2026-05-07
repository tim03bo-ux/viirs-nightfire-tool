"""
viz.py — Visualization and output generation.

Generates maps, charts, and export files from detection results.
"""

import os
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def plot_detections_map(df, output_path='output/detections_map.png',
                        bbox=None, title=None):
    """
    Plot detected thermal sources on a map of the Permian Basin.
    
    Args:
        df: DataFrame with latitude, longitude, temperature_K, radiant_heat_MW columns
        output_path: Where to save the plot
        bbox: Bounding box dict
        title: Plot title
    """
    if len(df) == 0:
        print("No detections to plot")
        return
    
    fig, ax = plt.subplots(1, 1, figsize=(12, 10))
    
    # Color by temperature
    valid = df['temperature_K'].notna()
    if valid.any():
        sc = ax.scatter(
            df.loc[valid, 'longitude'],
            df.loc[valid, 'latitude'],
            c=df.loc[valid, 'temperature_K'],
            s=df.loc[valid, 'radiant_heat_MW'].clip(0.1, 50) * 10,
            cmap='hot',
            vmin=800, vmax=2200,
            alpha=0.7,
            edgecolors='white',
            linewidth=0.5,
        )
        plt.colorbar(sc, ax=ax, label='Temperature (K)')
    
    # Plot failed fits as gray
    failed = ~valid
    if failed.any():
        ax.scatter(
            df.loc[failed, 'longitude'],
            df.loc[failed, 'latitude'],
            c='gray', s=10, alpha=0.3, marker='x',
        )
    
    if bbox:
        ax.set_xlim(bbox['lon_min'], bbox['lon_max'])
        ax.set_ylim(bbox['lat_min'], bbox['lat_max'])
    else:
        margin = 0.5
        ax.set_xlim(df['longitude'].min() - margin, df['longitude'].max() + margin)
        ax.set_ylim(df['latitude'].min() - margin, df['latitude'].max() + margin)

    ax.set_xlabel('Longitude')
    ax.set_ylabel('Latitude')
    ax.set_title(title or 'VIIRS Nightfire Detections — CONUS Oil & Gas Basins')
    ax.set_facecolor('#1a1a2e')
    fig.patch.set_facecolor('#0d1117')
    ax.tick_params(colors='white')
    ax.xaxis.label.set_color('white')
    ax.yaxis.label.set_color('white')
    ax.title.set_color('white')
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    plt.savefig(output_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close()
    print(f"Saved map: {output_path}")


def export_kmz(df, output_path='output/detections.kml'):
    """
    Export detections to KML for Google Earth viewing.
    """
    try:
        import simplekml
    except ImportError:
        print("simplekml not installed — skipping KMZ export")
        return
    
    kml = simplekml.Kml(name='VIIRS Nightfire Detections')
    
    for _, row in df.iterrows():
        if pd.isna(row.get('temperature_K')):
            continue
        
        pnt = kml.newpoint(
            name=f"{row['temperature_K']:.0f}K, {row.get('radiant_heat_MW', 0):.2f}MW",
            coords=[(row['longitude'], row['latitude'])],
        )
        pnt.description = (
            f"Temperature: {row['temperature_K']:.0f} K\n"
            f"Radiant Heat: {row.get('radiant_heat_MW', 0):.2f} MW\n"
            f"Source Area: {row.get('source_area_m2', 0):.1f} m²\n"
            f"Classification: {row.get('classification', 'unknown')}\n"
        )
        
        # Color by classification
        cls = row.get('classification', '')
        if cls == 'gas_flare':
            pnt.style.iconstyle.color = simplekml.Color.red
        elif cls == 'biomass_burning':
            pnt.style.iconstyle.color = simplekml.Color.orange
        else:
            pnt.style.iconstyle.color = simplekml.Color.yellow
    
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    kml.save(output_path)
    print(f"Saved KML: {output_path}")
