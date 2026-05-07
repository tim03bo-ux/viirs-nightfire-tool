"""
planck.py — Planck curve fitting for VIIRS nighttime thermal anomaly characterization.

Implements the core physics of the VNF algorithm:
1. Planck blackbody radiance function
2. Least-squares fitting of temperature and emission scaling factor
3. Derived quantities: source area, radiant heat

References:
    Elvidge et al. (2013) "VIIRS Nightfire: Satellite Pyrometry at Night"
    Remote Sensing 5(9):4423-4449
"""

import numpy as np
from scipy.optimize import least_squares

# Physical constants
h = 6.62607015e-34    # Planck constant (J·s)
c = 2.99792458e8      # Speed of light (m/s)
k = 1.380649e-23      # Boltzmann constant (J/K)
sigma = 5.670374419e-8  # Stefan-Boltzmann constant (W/(m²·K⁴))

# VIIRS band center wavelengths (meters)
BAND_WAVELENGTHS = {
    'M07': 0.865e-6,
    'M08': 1.240e-6,
    'M10': 1.610e-6,
    'M11': 2.250e-6,
    'M12': 3.700e-6,
    'M13': 4.050e-6,
    'M14': 8.550e-6,
}

# VIIRS band spectral response widths (approximate FWHM, meters) for band-integrated radiance
BAND_WIDTHS = {
    'M07': 0.015e-6,
    'M08': 0.020e-6,
    'M10': 0.060e-6,
    'M11': 0.050e-6,
    'M12': 0.180e-6,
    'M13': 0.155e-6,
    'M14': 0.300e-6,
}


def planck_spectral_radiance(wavelength_m, temperature_K):
    """
    Planck blackbody spectral radiance B(λ, T).
    
    Args:
        wavelength_m: Wavelength in meters (scalar or array)
        temperature_K: Temperature in Kelvin (scalar)
        
    Returns:
        Spectral radiance in W/(m²·sr·m)
    """
    # Guard against overflow in exponential
    exponent = h * c / (wavelength_m * k * temperature_K)
    exponent = np.clip(exponent, 0, 500)  # Prevent overflow
    
    numerator = 2.0 * h * c**2 / wavelength_m**5
    denominator = np.exp(exponent) - 1.0
    
    return numerator / denominator


def planck_radiance_per_micron(wavelength_m, temperature_K):
    """
    Planck radiance in W/(m²·sr·µm) — the typical VIIRS radiance unit.
    
    This is B(λ,T) converted from per-meter to per-micron.
    """
    return planck_spectral_radiance(wavelength_m, temperature_K) * 1e-6


def wien_peak_wavelength(temperature_K):
    """
    Wien's displacement law: peak wavelength for a given temperature.
    
    Args:
        temperature_K: Temperature in Kelvin
        
    Returns:
        Peak wavelength in meters
    """
    b = 2.897771955e-3  # Wien's displacement constant (m·K)
    return b / temperature_K


def fit_single_source(observed_radiances, bands=None, pixel_area_m2=562500.0):
    """
    Fit a single Planck curve to observed nighttime SWIR radiances.
    
    This is the core VNF fit for the SWIR bands (M7, M8, M10, M11) where
    the nighttime radiance is entirely attributable to the sub-pixel hot source.
    
    Args:
        observed_radiances: dict of {band_name: radiance} 
                           Radiance in W/(m²·sr·µm) as recorded in VJ102MOD
        bands: list of bands to use in fit (default: all bands in observed_radiances)
        pixel_area_m2: Pixel footprint area (default 750m × 750m nadir)
        
    Returns:
        dict with keys:
            'temperature_K': Fitted temperature
            'esf': Emission scaling factor (source_area / pixel_area)
            'source_area_m2': Estimated source area
            'radiant_heat_MW': Radiant heat output
            'radiant_heat_intensity_W_m2': σT⁴
            'residuals': Fit residuals per band
            'success': Whether the fit converged
            'bands_used': List of bands used in fit
    """
    if bands is None:
        bands = sorted(observed_radiances.keys())
    else:
        bands = [b for b in bands if b in observed_radiances]
    
    if len(bands) < 2:
        return {'success': False, 'error': 'Need at least 2 bands for fitting'}
    
    wavelengths = np.array([BAND_WAVELENGTHS[b] for b in bands])
    obs = np.array([observed_radiances[b] for b in bands])
    
    # Filter out zero/negative/nan radiances
    valid = np.isfinite(obs) & (obs > 0)
    if valid.sum() < 2:
        return {'success': False, 'error': 'Insufficient valid radiance values'}
    
    wavelengths = wavelengths[valid]
    obs = obs[valid]
    bands = [b for b, v in zip(bands, valid) if v]
    
    def residuals(params):
        T, log_esf = params
        esf = np.exp(log_esf)
        model = esf * np.array([
            planck_radiance_per_micron(w, T) for w in wavelengths
        ])
        # Use relative residuals weighted by signal strength
        return (model - obs) / (obs + 1e-20)
    
    # Initial guesses based on Wien's law applied to brightest band
    brightest_idx = np.argmax(obs)
    T_init = wien_peak_wavelength.__wrapped__(wavelengths[brightest_idx]) \
        if hasattr(wien_peak_wavelength, '__wrapped__') else 1800.0
    T_init = 1800.0  # Default gas flare temperature
    
    # ESF initial guess from brightest band
    model_init = planck_radiance_per_micron(wavelengths[brightest_idx], T_init)
    esf_init = obs[brightest_idx] / max(model_init, 1e-30)
    log_esf_init = np.log(max(esf_init, 1e-15))
    
    try:
        result = least_squares(
            residuals,
            x0=[T_init, log_esf_init],
            bounds=([400, np.log(1e-15)], [4000, np.log(1.0)]),
            method='trf',
            max_nfev=200,
        )
        
        T_fit = result.x[0]
        esf_fit = np.exp(result.x[1])
        source_area = esf_fit * pixel_area_m2
        rhi = sigma * T_fit**4  # W/m² at the source
        radiant_heat = source_area * rhi / 1e6  # MW
        
        # Compute per-band residuals for diagnostics
        model_final = esf_fit * np.array([
            planck_radiance_per_micron(w, T_fit) for w in wavelengths
        ])
        band_residuals = {b: float(m - o) for b, m, o in zip(bands, model_final, obs)}
        
        return {
            'temperature_K': float(T_fit),
            'esf': float(esf_fit),
            'source_area_m2': float(source_area),
            'radiant_heat_MW': float(radiant_heat),
            'radiant_heat_intensity_W_m2': float(rhi),
            'residuals': band_residuals,
            'cost': float(result.cost),
            'success': result.success,
            'bands_used': bands,
        }
        
    except Exception as e:
        return {'success': False, 'error': str(e)}


def fit_two_component(observed_radiances_swir, observed_radiances_mwir,
                      background_radiances_mwir, pixel_area_m2=562500.0):
    """
    Fit a two-component model: hot source + background.
    
    Used when MWIR bands (M12, M13) also detect the source. The MWIR signal
    is a mixture of hot source emission and background (surface/cloud) emission.
    
    Args:
        observed_radiances_swir: dict of SWIR band radiances (attributed entirely to source)
        observed_radiances_mwir: dict of MWIR band total observed radiances
        background_radiances_mwir: dict of MWIR band background radiances (from context window)
        pixel_area_m2: Pixel footprint area
        
    Returns:
        dict with fit results (same format as fit_single_source, plus background_T)
    """
    # First: subtract background from MWIR
    source_mwir = {}
    for band in observed_radiances_mwir:
        if band in background_radiances_mwir:
            source_rad = observed_radiances_mwir[band] - background_radiances_mwir[band]
            if source_rad > 0:
                source_mwir[band] = source_rad
    
    # Combine SWIR (entirely source) + background-subtracted MWIR
    all_source_radiances = {**observed_radiances_swir, **source_mwir}
    
    # Fit Planck curve to combined source radiances
    return fit_single_source(all_source_radiances, pixel_area_m2=pixel_area_m2)


def classify_source(temperature_K, persistence_days=None):
    """
    Classify a detected thermal source based on temperature.
    
    Args:
        temperature_K: Fitted temperature
        persistence_days: Number of days detected at same location (optional)
        
    Returns:
        str: Classification label
    """
    if temperature_K > 1400:
        return 'gas_flare'
    elif temperature_K > 1000:
        return 'industrial_or_large_fire'
    elif temperature_K > 700:
        return 'biomass_burning'
    elif temperature_K > 500:
        return 'smoldering'
    else:
        return 'low_temperature_anomaly'


def radiant_heat_to_bcm(radiant_heat_mw_sum, calibration_slope=0.029353):
    """
    Convert cumulative radiant heat to estimated flared gas volume in BCM.

    The calibration slope is published in BCM/MW-sum (Elvidge et al.) and is
    the canonical unit for this constant in the literature. All downstream
    unit conversions should start from BCM.

    Args:
        radiant_heat_mw_sum: Sum of radiant heat (MW) over time period
        calibration_slope: Calibration coefficient (BCM per MW-sum)
                          Default from World Bank/EOG global calibration

    Returns:
        Estimated volume in billion cubic meters (BCM)
    """
    return calibration_slope * radiant_heat_mw_sum


# Conversion factor: 1 BCM = 35,314,666.7 Mscf = 35,314.667 MMscf
BCM_TO_MMSCF = 35_314.667


def radiant_heat_to_mmscfd(daily_radiant_heat_mw_sum, calibration_slope=0.029353):
    """
    Convert a single day's radiant heat sum to a flared gas daily rate in MMscf/d.

    Args:
        daily_radiant_heat_mw_sum: Sum of radiant heat (MW) for one day
        calibration_slope: Calibration coefficient (BCM per MW-sum)

    Returns:
        Estimated daily flaring rate in MMscf/d
    """
    bcm = calibration_slope * daily_radiant_heat_mw_sum
    return bcm * BCM_TO_MMSCF
