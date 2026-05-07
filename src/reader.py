"""
reader.py — NetCDF/HDF5 reader for VIIRS VJ102MOD and VJ103MOD files.

This module handles loading and pairing radiance + geolocation granules.
It's the foundation — get this right first before building detection.

Usage:
    from src.reader import VIIRSGranule
    g = VIIRSGranule('data/raw/VJ102MOD.*.nc', 'data/raw/VJ103MOD.*.nc')
    g.describe()  # Print file structure and available variables
    m10 = g.get_radiance('M10')  # 2D array of calibrated radiances
    lat, lon = g.get_geolocation()
    night_mask = g.get_night_mask(sza_threshold=100.0)
"""

import numpy as np

try:
    import netCDF4 as nc
except ImportError:
    nc = None

try:
    import h5py
except ImportError:
    h5py = None


# VIIRS M-band center wavelengths (meters)
BAND_WAVELENGTHS_M = {
    'M01': 0.412e-6,
    'M02': 0.445e-6,
    'M03': 0.488e-6,
    'M04': 0.555e-6,
    'M05': 0.672e-6,
    'M06': 0.746e-6,
    'M07': 0.865e-6,
    'M08': 1.240e-6,
    'M09': 1.378e-6,
    'M10': 1.610e-6,
    'M11': 2.250e-6,
    'M12': 3.700e-6,
    'M13': 4.050e-6,
    'M14': 8.550e-6,
    'M15': 10.763e-6,
    'M16': 12.013e-6,
}

# Bands used for nightfire detection
NIGHTFIRE_BANDS = ['M07', 'M08', 'M10', 'M11', 'M12', 'M13', 'M14']

# Band center wavelengths for nightfire bands (meters)
NIGHTFIRE_WAVELENGTHS = {b: BAND_WAVELENGTHS_M[b] for b in NIGHTFIRE_BANDS}


class VIIRSGranule:
    """
    Container for a paired VJ102MOD (radiance) + VJ103MOD (geolocation) granule.
    
    The first task in Claude Code should be to instantiate this with real files
    and call describe() to understand the actual variable structure, then update
    the variable name mappings accordingly.
    """
    
    def __init__(self, radiance_path, geolocation_path):
        """
        Args:
            radiance_path: Path to VJ102MOD .nc file (radiance data)
            geolocation_path: Path to VJ103MOD .nc file (geolocation data)
        """
        self.radiance_path = radiance_path
        self.geolocation_path = geolocation_path
        self._rad_ds = None
        self._geo_ds = None
        
    def open(self):
        """Open both NetCDF files."""
        if nc is not None:
            self._rad_ds = nc.Dataset(self.radiance_path, 'r')
            # Disable auto-scaling on radiance file — we apply radiance_scale_factor manually
            self._rad_ds.set_auto_scale(False)
            self._geo_ds = nc.Dataset(self.geolocation_path, 'r')
            # Keep auto-scaling on geo file (lat/lon/solar_zenith use scale_factor correctly)
        elif h5py is not None:
            self._rad_ds = h5py.File(self.radiance_path, 'r')
            self._geo_ds = h5py.File(self.geolocation_path, 'r')
        else:
            raise ImportError("Need either netCDF4 or h5py installed")
        return self
    
    def close(self):
        """Close both files."""
        if self._rad_ds is not None:
            self._rad_ds.close()
        if self._geo_ds is not None:
            self._geo_ds.close()
    
    def __enter__(self):
        return self.open()
    
    def __exit__(self, *args):
        self.close()
    
    def describe(self):
        """
        Print the complete structure of both files.
        
        RUN THIS FIRST with real data files to understand the variable names,
        groups, dimensions, and attributes. The variable name patterns below
        are initial guesses that may need updating based on the actual files.
        """
        print("=" * 70)
        print(f"RADIANCE FILE: {self.radiance_path}")
        print("=" * 70)
        self._describe_dataset(self._rad_ds)
        
        print("\n" + "=" * 70)
        print(f"GEOLOCATION FILE: {self.geolocation_path}")
        print("=" * 70)
        self._describe_dataset(self._geo_ds)
    
    def _describe_dataset(self, ds):
        """Recursively print dataset structure."""
        if nc is not None and isinstance(ds, nc.Dataset):
            self._describe_netcdf(ds, indent=0)
        elif h5py is not None and isinstance(ds, h5py.File):
            self._describe_h5(ds, indent=0)
    
    def _describe_netcdf(self, group, indent=0):
        """Describe a netCDF4 group recursively."""
        prefix = "  " * indent
        
        # Global attributes
        if indent == 0:
            print(f"{prefix}Global attributes:")
            for attr in group.ncattrs():
                val = group.getncattr(attr)
                if isinstance(val, str) and len(val) > 100:
                    val = val[:100] + "..."
                print(f"{prefix}  {attr} = {val}")
        
        # Dimensions
        if group.dimensions:
            print(f"{prefix}Dimensions:")
            for name, dim in group.dimensions.items():
                print(f"{prefix}  {name} = {len(dim)}")
        
        # Variables
        if group.variables:
            print(f"{prefix}Variables:")
            for name, var in group.variables.items():
                attrs = {a: var.getncattr(a) for a in var.ncattrs()}
                units = attrs.get('units', '')
                scale = attrs.get('scale_factor', '')
                offset = attrs.get('add_offset', '')
                fill = attrs.get('_FillValue', '')
                info = f"shape={var.shape} dtype={var.dtype}"
                if units:
                    info += f" units={units}"
                if scale:
                    info += f" scale={scale}"
                if offset:
                    info += f" offset={offset}"
                print(f"{prefix}  {name}: {info}")
        
        # Groups
        if group.groups:
            for gname, g in group.groups.items():
                print(f"\n{prefix}Group: {gname}/")
                self._describe_netcdf(g, indent + 1)
    
    def _describe_h5(self, group, indent=0):
        """Describe an h5py group recursively."""
        prefix = "  " * indent
        for key in group:
            item = group[key]
            if isinstance(item, h5py.Group):
                print(f"{prefix}Group: {key}/")
                self._describe_h5(item, indent + 1)
            elif isinstance(item, h5py.Dataset):
                attrs = dict(item.attrs)
                info = f"shape={item.shape} dtype={item.dtype}"
                if 'units' in attrs:
                    info += f" units={attrs['units']}"
                if 'scale_factor' in attrs:
                    info += f" scale={attrs['scale_factor']}"
                print(f"{prefix}{key}: {info}")

    def get_radiance(self, band_name):
        """
        Get calibrated radiance array for a given band.

        Args:
            band_name: e.g. 'M10', 'M07', etc.

        Returns:
            2D numpy array of radiances in W/(m²·sr·µm)
            Masked array with fill values masked
        """
        # VJ102MOD stores bands as observation_data/M07, observation_data/M10, etc.
        # Data is uint16 with radiance_scale_factor and radiance_add_offset attrs.
        # Flag values 65532-65534 indicate Missing_EV, Bowtie_Deleted, Cal_Fail.
        possible_paths = [
            f'observation_data/{band_name}',
            f'observation_data/{band_name}_Radiance',
            f'All_Data/VIIRS-{band_name}-SDR_All/Radiance',
        ]

        var = None
        for path in possible_paths:
            try:
                if nc is not None and isinstance(self._rad_ds, nc.Dataset):
                    parts = path.split('/')
                    obj = self._rad_ds
                    for part in parts[:-1]:
                        obj = obj.groups[part]
                    var = obj.variables[parts[-1]]
                elif h5py is not None:
                    var = self._rad_ds[path]
                break
            except (KeyError, IndexError):
                continue

        if var is None:
            raise KeyError(
                f"Could not find radiance variable for {band_name}. "
                f"Tried: {possible_paths}. "
                f"Run describe() to see actual variable names."
            )

        # Read raw uint16 data
        raw = np.array(var[:], dtype=np.float64)

        if nc is not None and isinstance(self._rad_ds, nc.Dataset):
            attrs = {a: var.getncattr(a) for a in var.ncattrs()}
        else:
            attrs = dict(var.attrs)

        fill_value = attrs.get('_FillValue', 65535)
        valid_max = attrs.get('valid_max', 65527)

        # Mask fill values and flag values (>=65528 are special flags)
        invalid = (raw >= float(valid_max) + 1) | (raw == float(fill_value))

        # Use radiance_scale_factor for conversion to W/(m²·sr·µm)
        rad_scale = float(attrs.get('radiance_scale_factor', attrs.get('scale_factor', 1.0)))
        rad_offset = float(attrs.get('radiance_add_offset', attrs.get('add_offset', 0.0)))

        data = raw * rad_scale + rad_offset

        # Apply mask
        data = np.ma.array(data, mask=invalid | (data <= 0))

        return data
    
    def get_geolocation(self):
        """
        Get latitude and longitude arrays.
        
        Returns:
            tuple of (latitude, longitude) as 2D numpy arrays in decimal degrees
        """
        possible_lat_paths = [
            'geolocation_data/latitude',
            'geolocation_data/Latitude',
            'All_Data/VIIRS-MOD-GEO_All/Latitude',
        ]
        possible_lon_paths = [
            'geolocation_data/longitude', 
            'geolocation_data/Longitude',
            'All_Data/VIIRS-MOD-GEO_All/Longitude',
        ]
        
        lat = self._find_variable(self._geo_ds, possible_lat_paths)
        lon = self._find_variable(self._geo_ds, possible_lon_paths)
        
        return np.array(lat[:]), np.array(lon[:])
    
    def get_solar_zenith(self):
        """Get solar zenith angle array (degrees).

        netCDF4 auto-applies scale_factor on the geo dataset, so
        var[:] already returns physical degrees — no extra scaling needed.
        """
        paths = [
            'geolocation_data/solar_zenith',
            'geolocation_data/SolarZenithAngle',
            'All_Data/VIIRS-MOD-GEO_All/SolarZenithAngle',
        ]
        var = self._find_variable(self._geo_ds, paths)
        return np.array(var[:])
    
    def get_night_mask(self, sza_threshold=100.0):
        """
        Get boolean mask of nighttime pixels.
        
        Args:
            sza_threshold: Solar zenith angle threshold in degrees.
                          >90 = sun below horizon, >100 = well into astronomical twilight
        
        Returns:
            2D boolean array, True where it's nighttime
        """
        sza = self.get_solar_zenith()
        return sza > sza_threshold
    
    def get_bbox_mask(self, lat_min, lon_min, lat_max, lon_max):
        """
        Get boolean mask of pixels within a geographic bounding box.
        
        Args:
            lat_min, lon_min, lat_max, lon_max: Bounding box coordinates
            
        Returns:
            2D boolean array, True where pixel is within the bbox
        """
        lat, lon = self.get_geolocation()
        return (lat >= lat_min) & (lat <= lat_max) & (lon >= lon_min) & (lon <= lon_max)
    
    def _find_variable(self, ds, paths):
        """Try multiple variable paths and return the first match."""
        for path in paths:
            try:
                if nc is not None and isinstance(ds, nc.Dataset):
                    parts = path.split('/')
                    obj = ds
                    for part in parts[:-1]:
                        obj = obj.groups[part]
                    return obj.variables[parts[-1]]
                elif h5py is not None:
                    return ds[path]
            except (KeyError, IndexError):
                continue
        raise KeyError(f"Could not find variable. Tried: {paths}. Run describe().")


# Major CONUS oil & gas producing basins (most-specific first for classification)
BASINS = {
    'Permian': {'lat_min': 30.0, 'lat_max': 33.5, 'lon_min': -104.5, 'lon_max': -100.5},
    'Eagle Ford': {'lat_min': 27.5, 'lat_max': 30.0, 'lon_min': -100.5, 'lon_max': -96.0},
    'Haynesville': {'lat_min': 31.5, 'lat_max': 33.5, 'lon_min': -95.5, 'lon_max': -92.5},
    'San Juan': {'lat_min': 36.0, 'lat_max': 37.5, 'lon_min': -109.0, 'lon_max': -107.0},
    'Uinta': {'lat_min': 39.5, 'lat_max': 40.5, 'lon_min': -111.0, 'lon_max': -109.0},
    'Bakken': {'lat_min': 46.0, 'lat_max': 49.0, 'lon_min': -105.5, 'lon_max': -102.0},
    'DJ/Niobrara': {'lat_min': 39.0, 'lat_max': 42.5, 'lon_min': -105.5, 'lon_max': -103.0},
    'Powder River': {'lat_min': 42.5, 'lat_max': 46.0, 'lon_min': -107.0, 'lon_max': -104.5},
    'Anadarko/SCOOP/STACK': {'lat_min': 34.0, 'lat_max': 37.0, 'lon_min': -100.0, 'lon_max': -97.0},
    'Marcellus/Utica': {'lat_min': 38.5, 'lat_max': 42.5, 'lon_min': -82.0, 'lon_max': -77.0},
    'Williston': {'lat_min': 46.0, 'lat_max': 49.0, 'lon_min': -106.0, 'lon_max': -101.0},
    'Appalachian': {'lat_min': 37.0, 'lat_max': 43.0, 'lon_min': -83.0, 'lon_max': -76.0},
}

# Backward-compatible alias
PERMIAN_BBOX = BASINS['Permian']

# CONUS bounding box encompassing all basins
CONUS_BBOX = {
    'lat_min': min(b['lat_min'] for b in BASINS.values()),
    'lat_max': max(b['lat_max'] for b in BASINS.values()),
    'lon_min': min(b['lon_min'] for b in BASINS.values()),
    'lon_max': max(b['lon_max'] for b in BASINS.values()),
}


def classify_basin(lat, lon):
    """Classify detections into basins based on lat/lon bounding boxes.

    Accepts scalar or array inputs. Returns basin name string or numpy array.
    Basins are checked most-specific-first so tighter boxes win over broader ones.
    """
    scalar = np.isscalar(lat)
    lat = np.atleast_1d(np.asarray(lat, dtype=float))
    lon = np.atleast_1d(np.asarray(lon, dtype=float))

    result = np.full(len(lat), 'Other', dtype='U30')
    assigned = np.zeros(len(lat), dtype=bool)

    for name, bbox in BASINS.items():
        in_bbox = (
            ~assigned
            & (lat >= bbox['lat_min']) & (lat <= bbox['lat_max'])
            & (lon >= bbox['lon_min']) & (lon <= bbox['lon_max'])
        )
        result[in_bbox] = name
        assigned |= in_bbox

    return result[0] if scalar else result
