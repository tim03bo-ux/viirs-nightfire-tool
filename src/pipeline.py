"""
pipeline.py — End-to-end processing pipeline.

Wires together reader -> detect -> planck -> output.
"""

import argparse
import os
import sys
import glob
import re
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from .reader import VIIRSGranule, NIGHTFIRE_BANDS, CONUS_BBOX, classify_basin
from .detect import (
    compute_background_stats,
    detect_hot_pixels_swir,
    detect_hot_pixels_mwir,
    extract_detections,
    ZONE_BOUNDARIES,
)
from .planck import (
    fit_single_source,
    fit_two_component,
    classify_source,
    BAND_WAVELENGTHS,
)


def parse_filename_datetime(path):
    """Extract datetime from VJ102MOD/VJ103MOD filename.

    Format: VJ10xMOD.A{YYYY}{DOY}.{HHMM}.021.{timestamp}.nc
    """
    basename = os.path.basename(path)
    m = re.match(r'VJ10[23]MOD\.A(\d{4})(\d{3})\.(\d{2})(\d{2})', basename)
    if m:
        year = int(m.group(1))
        doy = int(m.group(2))
        hour = int(m.group(3))
        minute = int(m.group(4))
        dt = datetime(year, 1, 1) + timedelta(days=doy - 1, hours=hour, minutes=minute)
        return dt
    return None


def process_granule_pair(radiance_path, geolocation_path, bbox=None, verbose=True):
    """
    Process a single VJ102MOD + VJ103MOD granule pair.

    Returns:
        pandas DataFrame of detections with fitted parameters
    """
    if bbox is None:
        bbox = CONUS_BBOX

    granule_dt = parse_filename_datetime(radiance_path)

    if verbose:
        dt_str = granule_dt.strftime('%Y-%m-%d %H:%M UTC') if granule_dt else '?'
        print(f"Processing: {os.path.basename(radiance_path)} ({dt_str})")

    with VIIRSGranule(radiance_path, geolocation_path) as granule:
        # Step 0: Get geolocation and masks
        lat, lon = granule.get_geolocation()
        night_mask = granule.get_night_mask(sza_threshold=100.0)
        bbox_mask = granule.get_bbox_mask(
            bbox['lat_min'], bbox['lon_min'],
            bbox['lat_max'], bbox['lon_max']
        )

        roi_mask = night_mask & bbox_mask
        n_roi = roi_mask.sum()

        if verbose:
            print(f"  Night pixels in ROI: {n_roi:,}")

        if n_roi == 0:
            if verbose:
                print("  No nighttime pixels in ROI — skipping")
            return pd.DataFrame()

        # Step 1: Load M10 and detect hot pixels
        if verbose:
            print("  Detecting hot pixels in M10...")
        m10 = granule.get_radiance('M10')

        zone_stats = compute_background_stats(m10, roi_mask, ZONE_BOUNDARIES)
        hot_m10 = detect_hot_pixels_swir(m10, roi_mask, zone_stats)
        n_hot = hot_m10.sum()

        if verbose:
            print(f"  M10 hot pixels: {n_hot}")

        if n_hot == 0:
            if verbose:
                print("  No hot pixels — skipping")
            return pd.DataFrame()

        # Step 2: Load remaining bands
        if verbose:
            print("  Loading additional bands...")

        radiance_dict = {'M10': m10}
        for band in ['M07', 'M08', 'M11', 'M12', 'M13', 'M14']:
            try:
                radiance_dict[band] = granule.get_radiance(band)
            except KeyError as e:
                if verbose:
                    print(f"    Warning: {band} not found — {e}")

        # M11 confirmation detection
        additional_masks = {}
        if 'M11' in radiance_dict:
            stats_m11 = compute_background_stats(radiance_dict['M11'], roi_mask, ZONE_BOUNDARIES)
            additional_masks['M11'] = detect_hot_pixels_swir(
                radiance_dict['M11'], roi_mask, stats_m11
            )

        # MWIR detection for M12/M13
        bg_m12 = bg_m13 = None
        if 'M12' in radiance_dict and 'M13' in radiance_dict:
            mwir_hot, bg_m12, bg_m13 = detect_hot_pixels_mwir(
                radiance_dict['M12'], radiance_dict['M13'],
                roi_mask, hot_m10
            )
            additional_masks['M12'] = mwir_hot

        # Extract detection records
        detections = extract_detections(
            hot_m10, radiance_dict, lat, lon, additional_masks
        )

        if verbose:
            print(f"  Fitting Planck curves for {len(detections)} detections...")

        # Step 3: Planck fitting
        results = []
        for det in detections:
            swir_radiances = {}
            for band in ['M07', 'M08', 'M10', 'M11']:
                key = f'radiance_{band}'
                if key in det and np.isfinite(det[key]) and det[key] > 0:
                    swir_radiances[band] = det[key]

            # Check if MWIR data available for two-component fit
            mwir_radiances = {}
            mwir_background = {}
            row, col = det['row'], det['col']
            if bg_m12 is not None and np.isfinite(bg_m12[row, col]):
                for band, bg_arr in [('M12', bg_m12), ('M13', bg_m13)]:
                    key = f'radiance_{band}'
                    if key in det and np.isfinite(det[key]) and det[key] > 0:
                        mwir_radiances[band] = det[key]
                        if np.isfinite(bg_arr[row, col]):
                            mwir_background[band] = bg_arr[row, col]

            if mwir_radiances and mwir_background:
                fit = fit_two_component(swir_radiances, mwir_radiances, mwir_background)
            else:
                fit = fit_single_source(swir_radiances)

            record = {
                'datetime_utc': granule_dt.isoformat() if granule_dt else '',
                'latitude': det['latitude'],
                'longitude': det['longitude'],
                'basin': classify_basin(det['latitude'], det['longitude']),
                'row': det['row'],
                'col': det['col'],
                'granule': os.path.basename(radiance_path),
            }

            for band in NIGHTFIRE_BANDS:
                key = f'radiance_{band}'
                record[key] = det.get(key, np.nan)

            if fit['success']:
                record['temperature_K'] = fit['temperature_K']
                record['esf'] = fit['esf']
                record['source_area_m2'] = fit['source_area_m2']
                record['radiant_heat_MW'] = fit['radiant_heat_MW']
                record['fit_cost'] = fit['cost']
                record['classification'] = classify_source(fit['temperature_K'])
                record['n_bands_used'] = len(fit.get('bands_used', []))
            else:
                record['temperature_K'] = np.nan
                record['esf'] = np.nan
                record['source_area_m2'] = np.nan
                record['radiant_heat_MW'] = np.nan
                record['fit_cost'] = np.nan
                record['classification'] = 'fit_failed'
                record['n_bands_used'] = 0

            # Add confirmation flags
            for band in additional_masks:
                record[f'detected_{band}'] = det.get(f'detected_{band}', False)

            results.append(record)

        df = pd.DataFrame(results)

        if verbose and len(df) > 0:
            n_flares = (df['classification'] == 'gas_flare').sum()
            print(f"  Results: {len(df)} detections, {n_flares} gas flares")
            if n_flares > 0:
                flares = df[df['classification'] == 'gas_flare']
                print(f"    T range: {flares['temperature_K'].min():.0f}-{flares['temperature_K'].max():.0f} K")
                print(f"    RH range: {flares['radiant_heat_MW'].min():.2f}-{flares['radiant_heat_MW'].max():.2f} MW")

        return df


def find_granule_pairs(data_dir):
    """Find matching VJ102MOD + VJ103MOD file pairs in a directory."""
    rad_files = glob.glob(os.path.join(data_dir, 'VJ102MOD.*.nc'))
    geo_files = glob.glob(os.path.join(data_dir, 'VJ103MOD.*.nc'))

    def get_key(path):
        basename = os.path.basename(path)
        parts = basename.split('.')
        if len(parts) >= 3:
            return f"{parts[1]}.{parts[2]}"
        return None

    geo_by_key = {get_key(f): f for f in geo_files}

    pairs = []
    for rad_file in sorted(rad_files):
        key = get_key(rad_file)
        if key and key in geo_by_key:
            pairs.append((rad_file, geo_by_key[key]))

    return pairs


def run_pipeline(data_dir, bbox=None, output_dir='output', verbose=True):
    """Run the full pipeline on all granule pairs in a directory."""
    pairs = find_granule_pairs(data_dir)
    if verbose:
        print(f"Found {len(pairs)} granule pairs")

    os.makedirs(output_dir, exist_ok=True)

    all_results = []
    for rad_path, geo_path in pairs:
        try:
            df = process_granule_pair(rad_path, geo_path, bbox=bbox, verbose=verbose)
            if len(df) > 0:
                all_results.append(df)
        except Exception as e:
            print(f"  ERROR processing {os.path.basename(rad_path)}: {e}")

    if all_results:
        combined = pd.concat(all_results, ignore_index=True)
        output_csv = os.path.join(output_dir, 'nightfire_detections.csv')
        combined.to_csv(output_csv, index=False)
        if verbose:
            print(f"\nSaved {len(combined)} detections to {output_csv}")
            print(f"\nClassification summary:")
            print(combined['classification'].value_counts().to_string())
        return combined
    else:
        if verbose:
            print("\nNo detections found.")
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description='VIIRS Nightfire Analysis Tool')
    parser.add_argument('--radiance', help='Path to VJ102MOD .nc file')
    parser.add_argument('--geolocation', help='Path to VJ103MOD .nc file')
    parser.add_argument('--datadir', help='Directory containing VJ102MOD and VJ103MOD files')
    parser.add_argument('--bbox', default='27.5,-111.0,49.0,-76.0',
                       help='Bounding box: lat_min,lon_min,lat_max,lon_max (default: CONUS)')
    parser.add_argument('--output', default='output/', help='Output directory')
    parser.add_argument('--describe', action='store_true',
                       help='Just describe file structure and exit')

    args = parser.parse_args()

    bbox_parts = [float(x) for x in args.bbox.split(',')]
    bbox = {
        'lat_min': bbox_parts[0],
        'lon_min': bbox_parts[1],
        'lat_max': bbox_parts[2],
        'lon_max': bbox_parts[3],
    }

    os.makedirs(args.output, exist_ok=True)

    if args.describe and args.radiance and args.geolocation:
        with VIIRSGranule(args.radiance, args.geolocation) as g:
            g.describe()
        return

    if args.radiance and args.geolocation:
        df = process_granule_pair(args.radiance, args.geolocation, bbox=bbox)
        if len(df) > 0:
            output_csv = os.path.join(args.output, 'nightfire_detections.csv')
            df.to_csv(output_csv, index=False)
            print(f"\nSaved {len(df)} detections to {output_csv}")
    elif args.datadir:
        run_pipeline(args.datadir, bbox=bbox, output_dir=args.output)
    else:
        parser.error("Provide --radiance/--geolocation or --datadir")


if __name__ == '__main__':
    main()
