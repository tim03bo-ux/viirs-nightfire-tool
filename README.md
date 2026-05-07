# VIIRS Nightfire Analysis Tool

## Overview

A Python tool to detect and characterize natural gas flaring in the Permian Basin using raw VIIRS satellite data from NASA LAADS DAAC. This replicates the core methodology of the EOG VIIRS Nightfire (VNF) algorithm, which is now behind a commercial paywall.

The tool processes Level 1B radiance data from NOAA-20 VIIRS, applies Planck curve fitting to nighttime thermal anomalies, and estimates flare temperature (K), source area (m²), and radiant heat (MW) for each detected combustion source.

## Background & Motivation

The Earth Observation Group (EOG) at Colorado School of Mines developed the VIIRS Nightfire (VNF) product, which is the gold standard for satellite-based gas flare monitoring. As of January 2025, VNF data requires a commercial license. However, the underlying VIIRS Level 1B data is freely available from NASA LAADS DAAC, and the VNF algorithm is published in peer-reviewed literature.

This tool implements the VNF methodology using free NASA data to enable independent flare monitoring, particularly for the Permian Basin in West Texas.

### Key References

- Elvidge et al. (2013) "VIIRS Nightfire: Satellite Pyrometry at Night" - Remote Sensing 5(9):4423-4449
- Elvidge et al. (2016) "Methods for Global Survey of Natural Gas Flaring from VIIRS Data" - Energies 9:14
- Elvidge et al. (2019) "Extending Nighttime Combustion Source Detection Limits with Short Wavelength VIIRS Data"
- Zhizhin et al. (2021) "Night-Time Detection of Subpixel Emitters with VIIRS Mid-Wave Infrared Bands M12-M13"
- Elvidge et al. (2025) "An Improved Calibration for Satellite Estimation of Flared Gas Volumes from VIIRS Nighttime Data" - Energies 18(17):4765

## Input Data

### Required Products (from NASA LAADS DAAC, Collection 2, Archive Set 5200)

| Product | Description | Contents |
|---------|-------------|----------|
| **VJ102MOD** | VIIRS/JPSS1 (NOAA-20) Moderate Resolution 6-Min L1B Swath 750m | Calibrated radiances for all 16 M-bands (NetCDF/HDF5) |
| **VJ103MOD** | VIIRS/JPSS1 M-band Terrain-Corrected Geolocation 6-Min L1 Swath 750m | Lat, lon, solar/lunar zenith, land/water mask per pixel |

Each file pair covers a 6-minute orbital swath. For the Permian Basin (30.0-33.5°N, 100.5-104.5°W), nighttime overpasses occur at approximately 0700-0900 UTC (1-3 AM Central).

### File Naming Convention

```
VJ102MOD.A{YYYY}{DOY}.{HHMM}.002.{ProcessingTimestamp}.nc
VJ103MOD.A{YYYY}{DOY}.{HHMM}.002.{ProcessingTimestamp}.nc
```

- YYYY = year, DOY = day of year (001-365), HHMM = UTC acquisition time
- The VJ102MOD and VJ103MOD files with matching YYYY, DOY, and HHMM are paired

### VIIRS Spectral Bands Used

| Band | Center λ (µm) | Region | Role in Algorithm |
|------|---------------|--------|-------------------|
| M7   | 0.865 | NIR | Confirmation; noise-floor at night, hot sources stand out |
| M8   | 1.24  | NIR | Confirmation; same as M7 |
| M10  | 1.61  | SWIR | **Primary detection band**; nighttime noise + hot pixel outliers |
| M11  | 2.25  | SWIR | Second SWIR detection; critical for Wien's peak positioning |
| M12  | 3.70  | MWIR | Mixed signal; requires background subtraction (M12-M13 diagonal) |
| M13  | 4.05  | MWIR | Paired with M12 for MWIR thermal anomaly detection |
| M14  | 8.55  | LWIR | VNF v4.0 secondary emitter confirmation |

## Algorithm Overview

### Step 1: Hot Pixel Detection (SWIR)

1. Read nighttime M10 (1.61 µm) radiance array
2. Filter to nighttime pixels: solar zenith angle > 100° (from VJ103MOD)
3. Calculate background statistics per detector aggregation zone (3 zones across-track)
4. Detection threshold = mean + 4σ of background noise per zone
5. Pixels exceeding threshold in M10 are flagged as hot pixel candidates
6. Repeat for M11 (2.25 µm) independently

### Step 2: Multi-band Radiance Extraction

For each M10 hot pixel:
1. Extract corresponding radiances from M7, M8, M11, M12, M13, M14
2. For M7 and M8 (NIR): apply same noise-threshold detection (mean + 4σ)
3. For M12 and M13 (MWIR): compute background using 10×10 pixel window around each hot pixel
   - Exclude other hot pixels from background calculation
   - Background threshold = window mean + 3σ
   - Hot source radiance = observed - background mean
4. Record detection flags for each band

### Step 3: Planck Curve Fitting

For pixels detected in SWIR bands (M7, M8, M10, M11):
1. The nighttime radiance in these bands is entirely attributable to the hot source (no solar, no surface emission at these wavelengths)
2. Fit a Planck blackbody curve: `L(λ,T) = ESF × B(λ,T)`
   - Where `B(λ,T) = (2hc²/λ⁵) × 1/(exp(hc/λkT) - 1)` is Planck's function
   - T = temperature (K), fitting variable
   - ESF = emission scaling factor (dimensionless), fitting variable
   - ESF accounts for the source being sub-pixel (source area / pixel area)
3. Use scipy least_squares to minimize residuals across available SWIR bands
4. For pixels also detected in MWIR (M12, M13): fit a two-component model
   - Hot source Planck curve + background Planck curve
   - Background temperature estimated from surrounding pixels

### Step 4: Derived Quantities

From the Planck fit results:
- **Temperature (K)**: directly from fit
- **Source area (m²)**: ESF × pixel_footprint_area (footprint depends on scan angle from VJ103MOD)
- **Radiant heat intensity (W/m²)**: σT⁴ (Stefan-Boltzmann)
- **Radiant heat (MW)**: source_area × σT⁴ / 1e6

### Step 5: Flare Classification

- Gas flares: T > 1400 K (typically 1600-2200 K), persistent at same location
- Biomass burning: T typically 800-1200 K, transient
- Industrial (non-flare): T varies, persistent, typically lower than flares

### Step 6: Volume Estimation (Optional Calibration)

Radiant heat (MW) can be converted to flared gas volume using a calibration slope:
- `BCM = slope × Σ(radiant_heat_MW)` over time period
- EOG calibration slope ≈ 0.029353 (from World Bank datasets)
- Texas-specific calibration possible using RRC H-10/G-10 reported volumes

## Project Structure

```
viirs-nightfire-tool/
├── README.md              # This file
├── requirements.txt       # Python dependencies
├── src/
│   ├── __init__.py
│   ├── reader.py          # NetCDF/HDF5 file reader for VJ102MOD/VJ103MOD
│   ├── detect.py          # Hot pixel detection (Step 1-2)
│   ├── planck.py          # Planck curve fitting (Step 3-4)
│   ├── classify.py        # Flare classification and clustering (Step 5)
│   ├── calibrate.py       # Volume estimation calibration (Step 6)
│   ├── pipeline.py        # End-to-end processing pipeline
│   └── viz.py             # Visualization and output generation
├── data/
│   ├── raw/               # Place VJ102MOD and VJ103MOD .nc files here
│   └── processed/         # Intermediate outputs (CSVs, pickles)
├── output/                # Final outputs (CSVs, maps, plots)
└── tests/
    └── test_planck.py     # Unit tests for Planck fitting
```

## Target Area

**Permian Basin, West Texas**
- Bounding box: 30.0°N to 33.5°N, 100.5°W to 104.5°W
- Includes Delaware Basin (west of -103°) and Midland Basin (east of -103°)
- McCamey/BLU Kelton site: approximately 31.14°N, 102.22°W

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Get a NASA Earthdata bearer token

The fetcher downloads VIIRS L1B granules from NASA LAADS DAAC, which requires a free Earthdata Login account and a bearer token.

1. **Create an Earthdata Login account** at <https://urs.earthdata.nasa.gov/users/new> (free, no affiliation required).
2. **Sign in**, then go to your **profile menu → Generate Token** (or directly to <https://urs.earthdata.nasa.gov/profile> and click the "Generate Token" tab).
3. Click **Generate Token**. Copy the long string it produces — this is your bearer token. Tokens are valid for 60 days; regenerate when expired.
4. **Authorize the LAADS app** for your account: visit <https://ladsweb.modaps.eosdis.nasa.gov/> and sign in with the same Earthdata credentials at least once. This links your token to LAADS download permissions.

### 3. Save the token locally

Copy `.env.example` to `.env` and paste your token:

```
LAADS_TOKEN=eyJ0eXAiOi...your_token_here...
```

The `.env` file is gitignored — it stays on your machine and is never pushed.

### 4. Verify

```bash
python -c "from src.fetcher import load_token; print('OK' if load_token() else 'No token found')"
```

## Usage

```bash
# Process a single night's granule pair
python -m src.pipeline --radiance data/raw/VJ102MOD.A2026090.0812.002.*.nc \
                       --geolocation data/raw/VJ103MOD.A2026090.0812.002.*.nc \
                       --bbox 30.0,-104.5,33.5,-100.5 \
                       --output output/

# Process all granule pairs in a directory
python -m src.pipeline --datadir data/raw/ \
                       --bbox 30.0,-104.5,33.5,-100.5 \
                       --output output/
```

## Output

For each processed granule, the tool produces:
1. **CSV**: One row per detected hot source with lat, lon, temperature, source area, radiant heat, classification, detection band flags
2. **Summary**: Aggregate statistics for the Permian Basin sub-regions
3. **Map**: Matplotlib scatter plot of detections colored by temperature/FRP
4. **KMZ** (optional): Google Earth compatible output for field verification

## Validation

Cross-reference outputs against:
- NASA FIRMS VIIRS active fire NRT data (same satellite, different algorithm)
- World Bank GFMR individual flare location data (2017-2024, freely available)
- Texas RRC H-10/G-10 reported flaring volumes (for volume calibration)
