"""
detect.py — Hot pixel detection in nighttime VIIRS SWIR and MWIR bands.

Implements Steps 1-2 of the VNF algorithm:
1. M10 primary detection (noise threshold)
2. M11 secondary detection  
3. M7/M8 NIR confirmation
4. M12/M13 MWIR detection with background subtraction

This module is a scaffold — build it iteratively with Claude Code after
reader.py is working and you can see real data.
"""

import numpy as np
from typing import Dict, List, Tuple, Optional


# VIIRS M-band aggregation zone boundaries (sample indices across-track)
# Total across-track samples = 3200
# Zone 3: edge of scan (3x aggregation, largest pixels)
# Zone 2: intermediate (2x aggregation)
# Zone 1: nadir (no aggregation, smallest pixels ~750m)
ZONE_BOUNDARIES = [
    (0, 640),       # Zone 3 left edge
    (640, 1008),    # Zone 2 left
    (1008, 2192),   # Zone 1 center (nadir)
    (2192, 2560),   # Zone 2 right
    (2560, 3200),   # Zone 3 right edge
]


def compute_background_stats(radiance_array, night_mask, zone_boundaries=None):
    """
    Compute per-zone background statistics for detection thresholding.
    
    For SWIR bands (M10, M11, M7, M8), the nighttime image is dominated by
    instrument noise. Hot pixels are outliers against this noise floor.
    
    Args:
        radiance_array: 2D array of radiance values
        night_mask: 2D boolean array (True = nighttime)
        zone_boundaries: List of (start, end) sample index tuples per zone
        
    Returns:
        dict with per-zone mean, std, and threshold (mean + 4σ)
    """
    if zone_boundaries is None:
        # Single zone covering all samples
        n_samples = radiance_array.shape[1]
        zone_boundaries = [(0, n_samples)]
    
    zone_stats = []
    for zone_start, zone_end in zone_boundaries:
        zone_end = min(zone_end, radiance_array.shape[1])
        zone_raw = radiance_array[:, zone_start:zone_end]
        zone_data = np.ma.filled(zone_raw, np.nan) if hasattr(zone_raw, 'mask') else np.array(zone_raw, dtype=float)
        zone_night = night_mask[:, zone_start:zone_end]

        # Get valid nighttime pixels
        valid = zone_night & np.isfinite(zone_data) & (zone_data > 0)
        
        night_values = zone_data[valid]
        
        if len(night_values) == 0:
            zone_stats.append({
                'zone': (zone_start, zone_end),
                'mean': np.nan,
                'std': np.nan,
                'threshold': np.nan,
                'n_pixels': 0,
            })
            continue
        
        # Iterative sigma clipping to remove hot pixels from background
        for _ in range(3):
            mean = np.mean(night_values)
            std = np.std(night_values)
            if std == 0:
                break
            clip_mask = night_values < (mean + 5 * std)
            if clip_mask.sum() == len(night_values):
                break
            night_values = night_values[clip_mask]
        
        mean = float(np.mean(night_values))
        std = float(np.std(night_values))
        threshold = mean + 4.0 * std  # VNF uses mean + 4σ
        
        zone_stats.append({
            'zone': (zone_start, zone_end),
            'mean': mean,
            'std': std,
            'threshold': threshold,
            'n_pixels': len(night_values),
        })
    
    return zone_stats


def detect_hot_pixels_swir(radiance_array, night_mask, zone_stats):
    """
    Detect hot pixels in a SWIR band using noise threshold.

    Args:
        radiance_array: 2D radiance array (possibly masked)
        night_mask: 2D boolean nighttime mask
        zone_stats: Output of compute_background_stats()

    Returns:
        2D boolean array, True where hot pixel detected
    """
    # Work with filled array to avoid masked comparison issues
    data = np.ma.filled(radiance_array, np.nan) if hasattr(radiance_array, 'mask') else np.array(radiance_array, dtype=float)
    hot_mask = np.zeros(data.shape, dtype=bool)

    for stats in zone_stats:
        if np.isnan(stats['threshold']):
            continue
        zs, ze = stats['zone']
        ze = min(ze, data.shape[1])
        zone_data = data[:, zs:ze]
        zone_night = night_mask[:, zs:ze]

        # Hot pixels: nighttime, finite, above threshold
        zone_hot = zone_night & np.isfinite(zone_data) & (zone_data > stats['threshold'])
        hot_mask[:, zs:ze] = zone_hot

    return hot_mask


def detect_hot_pixels_mwir(radiance_m12, radiance_m13, night_mask,
                            hot_mask_swir, window_size=5, sigma_threshold=3.0):
    """
    Detect MWIR thermal anomalies using local background subtraction.

    Only evaluates pixels already flagged as SWIR hot pixels.

    Args:
        radiance_m12: 2D M12 radiance array
        radiance_m13: 2D M13 radiance array
        night_mask: 2D boolean nighttime mask
        hot_mask_swir: 2D boolean mask of SWIR hot pixels to evaluate
        window_size: Context window half-size for background estimation
        sigma_threshold: Number of sigma above background mean for detection

    Returns:
        hot_mask: 2D boolean array
        background_m12: 2D array of background M12 radiance at hot pixel locations
        background_m13: 2D array of background M13 radiance at hot pixel locations
    """
    nrows, ncols = radiance_m12.shape
    hot_mask = np.zeros((nrows, ncols), dtype=bool)
    bg_m12 = np.full((nrows, ncols), np.nan)
    bg_m13 = np.full((nrows, ncols), np.nan)

    # Get M12/M13 as plain arrays for windowed operations
    m12 = np.ma.filled(radiance_m12, np.nan) if hasattr(radiance_m12, 'mask') else np.array(radiance_m12, dtype=float)
    m13 = np.ma.filled(radiance_m13, np.nan) if hasattr(radiance_m13, 'mask') else np.array(radiance_m13, dtype=float)

    hot_rows, hot_cols = np.where(hot_mask_swir)

    for row, col in zip(hot_rows, hot_cols):
        r0 = max(0, row - window_size)
        r1 = min(nrows, row + window_size + 1)
        c0 = max(0, col - window_size)
        c1 = min(ncols, col + window_size + 1)

        win_m12 = m12[r0:r1, c0:c1].ravel()
        win_m13 = m13[r0:r1, c0:c1].ravel()
        win_night = night_mask[r0:r1, c0:c1].ravel()
        win_hot = hot_mask_swir[r0:r1, c0:c1].ravel()

        # Background = nighttime, non-hot, finite pixels in window
        bg_valid = win_night & ~win_hot & np.isfinite(win_m12) & np.isfinite(win_m13)
        if bg_valid.sum() < 5:
            continue

        bg_mean_12 = np.mean(win_m12[bg_valid])
        bg_std_12 = np.std(win_m12[bg_valid])
        bg_mean_13 = np.mean(win_m13[bg_valid])

        bg_m12[row, col] = bg_mean_12
        bg_m13[row, col] = bg_mean_13

        obs_12 = m12[row, col]
        if np.isfinite(obs_12) and obs_12 > bg_mean_12 + sigma_threshold * bg_std_12:
            hot_mask[row, col] = True

    return hot_mask, bg_m12, bg_m13


def extract_detections(hot_mask_m10, radiance_dict, lat, lon, 
                       additional_masks=None):
    """
    Extract detection records from hot pixel mask.
    
    Args:
        hot_mask_m10: 2D boolean array of M10 detections
        radiance_dict: dict of {band: 2D_radiance_array} for all nightfire bands
        lat: 2D latitude array
        lon: 2D longitude array
        additional_masks: dict of {band: hot_mask} for confirmation bands
        
    Returns:
        List of detection dicts with pixel coordinates, radiances, and geolocation
    """
    detections = []
    
    hot_rows, hot_cols = np.where(hot_mask_m10)
    
    for row, col in zip(hot_rows, hot_cols):
        det = {
            'row': int(row),
            'col': int(col),
            'latitude': float(lat[row, col]),
            'longitude': float(lon[row, col]),
        }
        
        # Extract radiance from each band at this pixel
        for band, rad_array in radiance_dict.items():
            val = rad_array[row, col]
            if hasattr(val, 'mask') and val.mask:
                det[f'radiance_{band}'] = np.nan
            else:
                det[f'radiance_{band}'] = float(val)
        
        # Add detection flags for confirmation bands
        if additional_masks:
            for band, mask in additional_masks.items():
                det[f'detected_{band}'] = bool(mask[row, col])
        
        detections.append(det)
    
    return detections
