"""
test_planck.py — Unit tests for Planck curve fitting.

Run with: python -m pytest tests/test_planck.py -v
"""

import numpy as np
import pytest
from src.planck import (
    planck_spectral_radiance,
    planck_radiance_per_micron,
    wien_peak_wavelength,
    fit_single_source,
    classify_source,
    BAND_WAVELENGTHS,
)


class TestPlanckFunction:
    """Test the Planck blackbody function."""
    
    def test_positive_radiance(self):
        """Radiance should always be positive for T > 0."""
        for T in [500, 1000, 1800, 3000]:
            for wvl in [0.865e-6, 1.61e-6, 3.7e-6]:
                L = planck_spectral_radiance(wvl, T)
                assert L > 0, f"Negative radiance at T={T}, λ={wvl}"
    
    def test_monotonic_with_temperature(self):
        """At fixed wavelength, radiance increases with temperature."""
        wvl = 1.61e-6  # M10
        temps = [500, 1000, 1500, 2000, 2500]
        radiances = [planck_spectral_radiance(wvl, T) for T in temps]
        for i in range(len(radiances) - 1):
            assert radiances[i] < radiances[i+1]
    
    def test_wien_peak(self):
        """Wien's law: peak wavelength for a gas flare (~1800K) should be ~1.6 µm."""
        peak = wien_peak_wavelength(1800)
        assert 1.0e-6 < peak < 2.0e-6, f"Wien peak {peak} not in expected range"
    
    def test_stefan_boltzmann_integration(self):
        """Total radiance from integration should approximate σT⁴/π."""
        # This is a rough check — the Planck function integrates to σT⁴/π
        T = 1800
        sigma = 5.67e-8
        expected_total = sigma * T**4 / np.pi  # Total hemispheric spectral radiance
        
        # Numerical integration over a wide wavelength range
        wavelengths = np.linspace(0.1e-6, 50e-6, 10000)
        radiances = planck_spectral_radiance(wavelengths, T)
        integrated = np.trapz(radiances, wavelengths)
        
        # Should be within 5% (we're not integrating 0 to infinity)
        ratio = integrated / expected_total
        assert 0.95 < ratio < 1.05, f"Integration ratio {ratio} outside 5% tolerance"


class TestPlanckFitting:
    """Test the Planck curve fitting."""
    
    def test_fit_synthetic_flare(self):
        """Fit should recover known temperature from synthetic flare data."""
        T_true = 1800  # Typical gas flare
        esf_true = 1e-6  # Sub-pixel source
        
        # Generate synthetic radiances
        observed = {}
        for band in ['M07', 'M08', 'M10', 'M11']:
            wvl = BAND_WAVELENGTHS[band]
            observed[band] = esf_true * planck_radiance_per_micron(wvl, T_true)
        
        result = fit_single_source(observed)
        
        assert result['success'], f"Fit failed: {result.get('error')}"
        assert abs(result['temperature_K'] - T_true) < 100, \
            f"Temperature {result['temperature_K']:.0f} too far from true {T_true}"
        assert abs(np.log10(result['esf']) - np.log10(esf_true)) < 1, \
            f"ESF {result['esf']:.2e} too far from true {esf_true:.2e}"
    
    def test_fit_synthetic_fire(self):
        """Fit should work for lower-temperature biomass fire."""
        T_true = 900
        esf_true = 5e-5
        
        observed = {}
        for band in ['M07', 'M08', 'M10', 'M11']:
            wvl = BAND_WAVELENGTHS[band]
            observed[band] = esf_true * planck_radiance_per_micron(wvl, T_true)
        
        result = fit_single_source(observed)
        
        assert result['success']
        assert abs(result['temperature_K'] - T_true) < 200
    
    def test_fit_with_noise(self):
        """Fit should be robust to moderate noise."""
        T_true = 1800
        esf_true = 1e-6
        np.random.seed(42)
        
        observed = {}
        for band in ['M07', 'M08', 'M10', 'M11']:
            wvl = BAND_WAVELENGTHS[band]
            clean = esf_true * planck_radiance_per_micron(wvl, T_true)
            noisy = clean * (1 + 0.1 * np.random.randn())  # 10% noise
            observed[band] = max(noisy, 0)
        
        result = fit_single_source(observed)
        
        assert result['success']
        assert abs(result['temperature_K'] - T_true) < 300
    
    def test_fit_two_bands_minimum(self):
        """Fit should work with only 2 bands."""
        T_true = 1800
        esf_true = 1e-6
        
        observed = {}
        for band in ['M10', 'M11']:
            wvl = BAND_WAVELENGTHS[band]
            observed[band] = esf_true * planck_radiance_per_micron(wvl, T_true)
        
        result = fit_single_source(observed)
        assert result['success']
    
    def test_fit_fails_one_band(self):
        """Fit should fail gracefully with only 1 band."""
        observed = {'M10': 1e-5}
        result = fit_single_source(observed)
        assert not result['success']
    
    def test_radiant_heat_positive(self):
        """Radiant heat should be positive for successful fits."""
        observed = {
            'M07': 1e-8,
            'M08': 5e-7,
            'M10': 1e-5,
            'M11': 3e-5,
        }
        result = fit_single_source(observed)
        if result['success']:
            assert result['radiant_heat_MW'] > 0


class TestClassification:
    """Test source classification."""
    
    def test_gas_flare(self):
        assert classify_source(1800) == 'gas_flare'
        assert classify_source(1500) == 'gas_flare'
    
    def test_biomass(self):
        assert classify_source(900) == 'biomass_burning'
    
    def test_industrial(self):
        assert classify_source(1200) == 'industrial_or_large_fire'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
