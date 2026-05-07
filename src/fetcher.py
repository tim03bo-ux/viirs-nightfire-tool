"""
fetcher.py — LAADS DAAC API client for automated VIIRS data retrieval.

Downloads VJ102MOD + VJ103MOD granule pairs, processes them through the
nightfire pipeline, stores results, and deletes the raw files to save disk space.

Requires a NASA Earthdata bearer token in .env as LAADS_TOKEN=...

API flow:
  1. searchForFiles (classic SOAP API, no auth) -> file IDs
  2. getFileUrls -> download URLs (collection 5201)
  3. Download .nc files with Authorization: Bearer header
  4. Process with pipeline -> append to CSV
  5. Delete .nc files
"""

import os
import re
import json
import time
import tempfile
import shutil
import urllib.request
import urllib.error
from datetime import datetime, timedelta, date
from xml.etree import ElementTree
from pathlib import Path

import pandas as pd

from .reader import CONUS_BBOX
from .pipeline import process_granule_pair, parse_filename_datetime


# LAADS DAAC endpoints
SEARCH_URL = (
    "https://modwebsrv.modaps.eosdis.nasa.gov/axis2/services/"
    "MODAPSservices/searchForFiles"
)
FILE_URLS_URL = (
    "https://modwebsrv.modaps.eosdis.nasa.gov/axis2/services/"
    "MODAPSservices/getFileUrls"
)

COLLECTION = "5201"  # VIIRS Collection 2.1 (Archive Set 5201)

# Namespace for parsing SOAP XML responses
NS = {"mws": "http://modapsws.gsfc.nasa.gov/xsd"}

# Rate limiting defaults (seconds)
API_DELAY = 1.0        # Pause between LAADS search/metadata API calls
DOWNLOAD_DELAY = 2.0   # Pause between file downloads
MAX_RETRIES = 3        # Retry count for transient failures
RETRY_BACKOFF = 5.0    # Base backoff seconds (doubles each retry)
SEARCH_CHUNK_DAYS = 7  # Max days per search query to avoid overwhelming the API


def load_token(env_path=None):
    """Load LAADS bearer token from .env file or environment variable."""
    # Check environment variable first
    token = os.environ.get("LAADS_TOKEN")
    if token and token != "PASTE_YOUR_TOKEN_HERE":
        return token

    # Try .env file
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("LAADS_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    if token and token != "PASTE_YOUR_TOKEN_HERE":
                        return token

    raise ValueError(
        "No LAADS token found. Set LAADS_TOKEN in .env or environment variable.\n"
        "Get a token at: https://ladsweb.modaps.eosdis.nasa.gov/profiles/#generate-token-modal"
    )


def _request_with_retry(req, timeout=60):
    """Execute a urllib request with retry and exponential backoff."""
    for attempt in range(MAX_RETRIES):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = RETRY_BACKOFF * (2 ** attempt)
            time.sleep(wait)


def _fetch_xml(url):
    """Fetch URL and parse as XML, with rate limiting and retry."""
    time.sleep(API_DELAY)
    req = urllib.request.Request(url)
    resp = _request_with_retry(req, timeout=30)
    try:
        return ElementTree.fromstring(resp.read())
    finally:
        resp.close()


def _search_granules_chunk(product, start_str, end_str, bbox, day_night):
    """Search a single date chunk (internal)."""
    params = (
        f"?product={product}"
        f"&collection={COLLECTION}"
        f"&start={start_str}"
        f"&stop={end_str}"
        f"&north={bbox['lat_max']}"
        f"&south={bbox['lat_min']}"
        f"&west={bbox['lon_min']}"
        f"&east={bbox['lon_max']}"
        f"&coordsOrTiles=coords"
        f"&dayNightBoth={day_night}"
    )

    root = _fetch_xml(SEARCH_URL + params)

    file_ids = []
    for elem in root.findall("return", NS) or root.findall("return"):
        text = elem.text
        if text and text.strip() and text.strip() != "No results":
            file_ids.append(text.strip())
    return file_ids


def search_granules(product, start_date, end_date, bbox=None, day_night="N"):
    """
    Search LAADS DAAC for available granule file IDs.

    Large date ranges are automatically chunked into SEARCH_CHUNK_DAYS windows
    with rate-limited pauses between queries.

    Args:
        product: 'VJ102MOD' or 'VJ103MOD'
        start_date: date object or 'YYYY-MM-DD' string
        end_date: date object or 'YYYY-MM-DD' string
        bbox: dict with lat_min, lat_max, lon_min, lon_max
        day_night: 'N' for night, 'D' for day, 'DNB' for both

    Returns:
        List of file ID strings
    """
    if bbox is None:
        bbox = CONUS_BBOX

    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

    # Chunk large ranges into SEARCH_CHUNK_DAYS windows
    all_ids = []
    chunk_start = start_date
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=SEARCH_CHUNK_DAYS - 1), end_date)
        ids = _search_granules_chunk(
            product,
            chunk_start.strftime("%Y-%m-%d"),
            chunk_end.strftime("%Y-%m-%d"),
            bbox, day_night,
        )
        all_ids.extend(ids)
        chunk_start = chunk_end + timedelta(days=1)

    return all_ids


def get_file_urls(file_ids):
    """
    Convert file IDs to download URLs via LAADS API.

    Processes in batches of 100 with rate-limited pauses between requests.

    Args:
        file_ids: List of file ID strings

    Returns:
        List of download URL strings
    """
    if not file_ids:
        return []

    urls = []
    for i in range(0, len(file_ids), 100):
        chunk = file_ids[i : i + 100]
        ids_param = ",".join(chunk)
        url = f"{FILE_URLS_URL}?fileIds={ids_param}"
        root = _fetch_xml(url)  # _fetch_xml already rate-limits

        for elem in root.findall("return", NS) or root.findall("return"):
            if elem.text and elem.text.startswith("http"):
                urls.append(elem.text.strip())

    return urls


def _get_granule_key(filename):
    """Extract the date+time matching key from a filename.

    VJ102MOD.A2026076.0748.021.2026076134531.nc -> 'A2026076.0748'
    """
    parts = os.path.basename(filename).split(".")
    if len(parts) >= 3:
        return f"{parts[1]}.{parts[2]}"
    return None


def download_file(url, dest_path, token):
    """Download a file from LAADS DAAC with bearer auth, retry, and throttle."""
    time.sleep(DOWNLOAD_DELAY)
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")

    resp = _request_with_retry(req, timeout=300)
    try:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(resp, f)
    finally:
        resp.close()

    return dest_path


def load_processed_log(output_dir):
    """Load the set of already-processed granule keys."""
    log_path = os.path.join(output_dir, "processed_granules.json")
    if os.path.exists(log_path):
        with open(log_path) as f:
            return set(json.load(f))
    return set()


def save_processed_log(output_dir, processed_keys):
    """Save the set of processed granule keys."""
    log_path = os.path.join(output_dir, "processed_granules.json")
    with open(log_path, "w") as f:
        json.dump(sorted(processed_keys), f, indent=2)


def fetch_and_process(
    start_date,
    end_date,
    token=None,
    bbox=None,
    output_dir=None,
    verbose=True,
    progress_callback=None,
    api_delay=None,
    download_delay=None,
):
    """
    Fetch VIIRS granules from LAADS DAAC, process them, and discard raw files.

    Rate limiting:
      - Large date ranges are chunked into 7-day search windows
      - API calls are spaced by api_delay seconds (default 1.0)
      - File downloads are spaced by download_delay seconds (default 2.0)
      - Transient failures retry with exponential backoff

    Args:
        start_date: date object or 'YYYY-MM-DD'
        end_date: date object or 'YYYY-MM-DD'
        token: LAADS bearer token (loaded from .env if None)
        bbox: Bounding box dict (default: CONUS)
        output_dir: Where to store results CSV and log
        verbose: Print progress
        progress_callback: Optional callable(current, total, message) for UI updates
        api_delay: Seconds between API calls (default 1.0)
        download_delay: Seconds between file downloads (default 2.0)

    Returns:
        pandas DataFrame of all new detections
    """
    global API_DELAY, DOWNLOAD_DELAY
    if api_delay is not None:
        API_DELAY = api_delay
    if download_delay is not None:
        DOWNLOAD_DELAY = download_delay
    if token is None:
        token = load_token()

    if bbox is None:
        bbox = CONUS_BBOX

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")
    os.makedirs(output_dir, exist_ok=True)

    if isinstance(start_date, str):
        start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
    if isinstance(end_date, str):
        end_date = datetime.strptime(end_date, "%Y-%m-%d").date()

    # Load already-processed granules
    processed_keys = load_processed_log(output_dir)

    if verbose:
        print(f"Searching LAADS DAAC for {start_date} to {end_date}...")

    # Step 1: Search for file IDs
    rad_ids = search_granules("VJ102MOD", start_date, end_date, bbox, day_night="N")
    geo_ids = search_granules("VJ103MOD", start_date, end_date, bbox, day_night="N")

    if verbose:
        print(f"  Found {len(rad_ids)} VJ102MOD and {len(geo_ids)} VJ103MOD file IDs")

    if not rad_ids or not geo_ids:
        if verbose:
            print("  No granules found for this date range and region.")
        return pd.DataFrame()

    # Step 2: Get download URLs
    rad_urls = get_file_urls(rad_ids)
    geo_urls = get_file_urls(geo_ids)

    if verbose:
        print(f"  Resolved {len(rad_urls)} radiance and {len(geo_urls)} geolocation URLs")

    # Step 3: Pair by date+time key
    rad_by_key = {}
    for url in rad_urls:
        key = _get_granule_key(url)
        if key:
            rad_by_key[key] = url

    geo_by_key = {}
    for url in geo_urls:
        key = _get_granule_key(url)
        if key:
            geo_by_key[key] = url

    pairs = []
    for key in sorted(rad_by_key.keys()):
        if key in geo_by_key:
            if key not in processed_keys:
                pairs.append((key, rad_by_key[key], geo_by_key[key]))

    if verbose:
        print(f"  {len(pairs)} new granule pairs to process "
              f"({len(processed_keys)} already processed)")

    if not pairs:
        if verbose:
            print("  All granules already processed.")
        return pd.DataFrame()

    # Step 4: Download, process, delete each pair
    csv_path = os.path.join(output_dir, "nightfire_detections.csv")
    all_new = []
    batch_start_time = time.time()

    for i, (key, rad_url, geo_url) in enumerate(pairs):
        rad_filename = os.path.basename(rad_url)
        geo_filename = os.path.basename(geo_url)

        # Estimate remaining time
        elapsed = time.time() - batch_start_time
        if i > 0:
            per_pair = elapsed / i
            remaining = per_pair * (len(pairs) - i)
            eta_min = int(remaining // 60)
            eta_sec = int(remaining % 60)
            eta_str = f" (~{eta_min}m {eta_sec}s left)" if eta_min > 0 else f" (~{eta_sec}s left)"
        else:
            eta_str = ""

        if verbose:
            print(f"\n[{i + 1}/{len(pairs)}] {rad_filename}{eta_str}")

        if progress_callback:
            progress_callback(
                i, len(pairs),
                f"[{i + 1}/{len(pairs)}] Downloading {rad_filename}...{eta_str}",
            )

        # Download to temp directory
        tmp_dir = tempfile.mkdtemp(prefix="viirs_")
        try:
            rad_path = os.path.join(tmp_dir, rad_filename)
            geo_path = os.path.join(tmp_dir, geo_filename)

            if verbose:
                print(f"  Downloading radiance file...")
            download_file(rad_url, rad_path, token)

            if verbose:
                print(f"  Downloading geolocation file...")
            download_file(geo_url, geo_path, token)

            # Process
            if progress_callback:
                progress_callback(i, len(pairs), f"[{i + 1}/{len(pairs)}] Processing...")

            if verbose:
                print(f"  Processing...")
            df = process_granule_pair(rad_path, geo_path, bbox=bbox, verbose=verbose)

            if len(df) > 0:
                all_new.append(df)

                # Append to CSV — ensure schema matches existing file
                if os.path.exists(csv_path):
                    existing_cols = pd.read_csv(csv_path, nrows=0).columns.tolist()
                    # Align new data to existing column order, add missing cols
                    for col in existing_cols:
                        if col not in df.columns:
                            df[col] = pd.NA
                    df = df[existing_cols + [c for c in df.columns if c not in existing_cols]]
                    df.to_csv(csv_path, mode="a", header=False, index=False)
                else:
                    df.to_csv(csv_path, mode="w", header=True, index=False)

            # Mark as processed
            processed_keys.add(key)
            save_processed_log(output_dir, processed_keys)

            if verbose:
                n_det = len(df) if len(df) > 0 else 0
                print(f"  Done: {n_det} detections. Raw files deleted.")

        except Exception as e:
            if verbose:
                print(f"  ERROR: {e}")
        finally:
            # Always delete raw files
            shutil.rmtree(tmp_dir, ignore_errors=True)

    total_elapsed = time.time() - batch_start_time
    if progress_callback:
        progress_callback(len(pairs), len(pairs),
                         f"Complete! {len(pairs)} granules in {int(total_elapsed)}s")

    if all_new:
        combined = pd.concat(all_new, ignore_index=True)
        if verbose:
            n_flares = (combined["classification"] == "gas_flare").sum()
            print(f"\nTotal new detections: {len(combined)} ({n_flares} gas flares)")
        return combined
    else:
        if verbose:
            print("\nNo new detections.")
        return pd.DataFrame()
