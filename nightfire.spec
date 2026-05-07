# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for VIIRS Nightfire Dashboard.

Build with:  pyinstaller nightfire.spec
Output:      dist/NightfireDashboard/NightfireDashboard.exe
"""

import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# --- Paths ---
SITE_PACKAGES = os.path.join(
    os.path.dirname(sys.executable), 'Lib', 'site-packages'
)
STREAMLIT_DIR = os.path.join(SITE_PACKAGES, 'streamlit')
PLOTLY_DIR = os.path.join(SITE_PACKAGES, 'plotly')
PYPROJ_DIR = os.path.join(SITE_PACKAGES, 'pyproj')
CARTOPY_DATA = os.path.join(os.path.expanduser('~'), '.local', 'share', 'cartopy')

# --- Data files to bundle ---
datas = [
    # Our application files
    ('dashboard.py', '.'),
    ('src', 'src'),
    ('output', 'output'),
    ('.env', '.'),

    # Streamlit needs its static assets and proto files
    (os.path.join(STREAMLIT_DIR, 'static'), os.path.join('streamlit', 'static')),
    (os.path.join(STREAMLIT_DIR, 'proto'), os.path.join('streamlit', 'proto')),

    # Plotly templates and data
    (os.path.join(PLOTLY_DIR, 'package_data'), os.path.join('plotly', 'package_data')),

    # pyproj projection database (required by cartopy)
    (os.path.join(PYPROJ_DIR, 'proj_dir', 'share', 'proj'),
     os.path.join('pyproj', 'proj_dir', 'share', 'proj')),
]

# Cartopy shapefiles (if downloaded)
if os.path.isdir(CARTOPY_DATA):
    datas.append((CARTOPY_DATA, os.path.join('cartopy', 'data')))

# Collect additional package data
datas += collect_data_files('streamlit')
datas += collect_data_files('plotly')
datas += collect_data_files('certifi')
datas += collect_data_files('pyproj')

# Package metadata (needed by importlib.metadata at runtime)
for pkg in ['streamlit', 'plotly', 'altair', 'pandas', 'numpy', 'scipy',
            'packaging', 'pyarrow', 'pydeck', 'validators', 'certifi',
            'toml', 'typing_extensions', 'narwhals', 'watchdog']:
    try:
        datas += copy_metadata(pkg)
    except Exception:
        pass

# --- Hidden imports ---
# Streamlit, plotly, and the scientific stack have many dynamic imports
hiddenimports = (
    collect_submodules('streamlit')
    + collect_submodules('plotly')
    + collect_submodules('cartopy')
    + collect_submodules('pyproj')
    + collect_submodules('pandas')
    + collect_submodules('numpy')
    + collect_submodules('scipy')
    + [
        # Core scientific
        'netCDF4',
        'h5py',
        'cftime',

        # Visualization
        'matplotlib',
        'matplotlib.backends.backend_agg',
        'PIL',
        'PIL.Image',

        # Streamlit extras
        'streamlit.runtime.scriptrunner',
        'streamlit.web.server',
        'streamlit.web.bootstrap',
        'streamlit.commands.page_config',
        'altair',
        'validators',
        'toml',
        'watchdog',
        'watchdog.observers',
        'watchdog.events',

        # Plotly
        'plotly.express',
        'plotly.graph_objects',
        'plotly.subplots',

        # Networking (for fetcher)
        'urllib.request',
        'xml.etree.ElementTree',
        'json',

        # Our source package
        'src',
        'src.reader',
        'src.detect',
        'src.planck',
        'src.pipeline',
        'src.fetcher',
        'src.viz',

        # Misc
        'packaging',
        'packaging.version',
        'packaging.requirements',
        'importlib_metadata',
        'typing_extensions',
    ]
)

# --- Analysis ---
a = Analysis(
    ['launch_app.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='NightfireDashboard',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Keep console visible so user sees Streamlit startup messages
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='NightfireDashboard',
)
