#!/usr/bin/env python3
"""Excavation helper for the Upton County plugged-back oil test behind water
well YL-45-56-803 (TWDB site 4556803).

Three steps, all runnable independently:

  twdb     Download TWDB Report 78 (White, 1968, Ground-Water Resources of
           Upton County) and pull out the records-of-wells entry for
           45-56-803 plus any nearby mention of an oil test or the landowner.
           This is the cheapest shot at learning the oil test's operator name.

  wildcat  Download the RRC Wildcat & Suspense microfilm roll index and filter
           it to District 7C, listing every year span at or after 1957. A 1957
           dry hole that never got a W-2 completion report lives in suspense,
           and suspense files were swept up years late -- so the 1957 span is
           not enough on its own.

  variants Print the name / key permutation matrix to paste into RRC queries.

Written to run from a machine with open internet access. See README.md section 7:
this has not been executed against the live RRC or TWDB endpoints.

Usage:
    python3 find_rrc_records.py                 # all steps
    python3 find_rrc_records.py --step twdb
    python3 find_rrc_records.py --step wildcat --outdir ./downloads
"""

from __future__ import annotations

import argparse
import html
import io
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = "Mozilla/5.0 (compatible; records-research/1.0)"

R78_PDF = (
    "https://www.twdb.texas.gov/publications/reports/numbered_reports"
    "/doc/R78/R78.pdf"
)
WILDCAT_INDEX_PAGE = (
    "https://www.rrc.texas.gov/resource-center/research/research-queries"
    "/imaged-records/imaged-records-menu/wildcat-and-suspense-microfilm-index/"
)
FILM_GUIDES = {
    "wildcat-and-suspense-guide.pdf": "https://www.rrc.texas.gov/media/55xbpldb/historical-wildcat-and-suspence-film-users-guide.pdf",
    "historical-well-records-guide.pdf": "https://www.rrc.texas.gov/media/qfjjyj45/historical-well-records-film-users-guide.pdf",
    "closed-potential-guide.pdf": "https://www.rrc.texas.gov/media/bi3atklh/historical-closed-potential-film-users-guide.pdf",
    "p-13-guide.pdf": "https://www.rrc.texas.gov/media/flinhkth/historical-p-13-film-user-guide.pdf",
}

# The well, as transcribed from the USGS well schedule.
STATE_WELL_NO = "45-56-803"
DRILLED = "1957-05-14"
COUNTY, RRC_DISTRICT, COUNTY_CODE = "UPTON", "7C", "461"

# Lease name is the alphabetical key in pre-1965 RRC filing -- this matters far
# more than the driller's name.
LEASE_VARIANTS = [
    "KINCAID", "KINCADE", "KINKAID", "KINCAID ESTATE",
    "B KINCAID", "BERT KINCAID", "KINGRID", "KINARD",
]
OPERATOR_VARIANTS = [
    "KIMBELL", "KIMBEL", "KIMBLE", "KIMBALL", "KIMBRELL", "KIMBROUGH",
    "KIMBELL OIL CO", "KIMBELL OIL COMPANY OF TEXAS",
]


class FetchError(RuntimeError):
    """A URL could not be retrieved, with a human-actionable explanation."""


def fetch(url: str, dest: Path | None = None) -> bytes:
    """GET a URL, optionally caching to disk. Returns the raw body.

    Raises FetchError with an actionable message rather than a traceback --
    these endpoints are commonly unreachable from restricted networks, and a
    proxy denial should read as "your network blocked this", not as a bug.
    """
    if dest is not None and dest.exists() and dest.stat().st_size > 0:
        print(f"  cached: {dest}")
        return dest.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}\n    HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        hint = ""
        reason = str(exc.reason)
        if "403" in reason or "Tunnel connection failed" in reason:
            hint = (
                "\n    A proxy refused the CONNECT. This host is very likely blocked"
                "\n    by your network's egress policy -- run this from an unrestricted"
                "\n    machine, or allowlist rrc.texas.gov and twdb.texas.gov."
            )
        raise FetchError(f"{url}\n    {reason}{hint}") from exc
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        print(f"  saved:  {dest}  ({len(body):,} bytes)")
    return body


def pdf_to_text(data: bytes) -> str:
    """Extract text from a PDF, trying pypdf then the pdftotext binary."""
    try:
        from pypdf import PdfReader
    except ImportError:
        pass
    else:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    import shutil
    import subprocess
    import tempfile

    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(data)
            tmp.flush()
            out = subprocess.run(
                ["pdftotext", "-layout", tmp.name, "-"],
                capture_output=True, text=True, check=False,
            )
            return out.stdout

    sys.exit(
        "Need a PDF text extractor. Install one:\n"
        "    pip install pypdf\n"
        "  or install poppler-utils for the pdftotext binary."
    )


def show_hits(text: str, patterns: list[str], context: int = 2, label: str = "") -> int:
    """Print every line matching any pattern, with surrounding context."""
    lines = text.splitlines()
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    seen: set[int] = set()
    hits = 0
    for i, line in enumerate(lines):
        if not any(c.search(line) for c in compiled):
            continue
        hits += 1
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        block = range(lo, hi)
        if all(n in seen for n in block):
            continue
        seen.update(block)
        print(f"\n  --- {label}line {i + 1} " + "-" * 40)
        for n in block:
            marker = ">>" if n == i else "  "
            print(f"  {marker} {lines[n].rstrip()}")
    return hits


def step_twdb(outdir: Path) -> None:
    print("\n" + "=" * 70)
    print("STEP: TWDB Report 78 -- White (1968), Ground-Water Resources of Upton County")
    print("=" * 70)
    print(f"\nFetching {R78_PDF}")
    try:
        data = fetch(R78_PDF, outdir / "TWDB-R78.pdf")
    except FetchError as exc:
        print(f"  FAILED: {exc}")
        print("\n  Skipping this step. You can also download R78.pdf by hand and drop")
        print(f"  it at {outdir / 'TWDB-R78.pdf'}, then re-run -- it will be picked up.")
        return
    text = pdf_to_text(data)
    print(f"  extracted {len(text):,} characters")

    # The records-of-wells table keys on the state well number. Tables often
    # lose the leading quadrangle digits in extraction, so match loosely.
    print("\n[1] Records-of-wells entry for the well itself:")
    n = show_hits(text, [r"45\s*-\s*56\s*-\s*803", r"\b56\s*-\s*803\b"], context=3)
    if n == 0:
        print("  No direct hit. The table may be an image or a column-split layout;")
        print("  open TWDB-R78.pdf and read the 'Records of Wells' appendix by hand.")

    print("\n[2] Landowner and driller mentions:")
    show_hits(text, [r"Kinc?a?[ir]d", r"Kingrid", r"Kimb[ae]ll?", r"Buck Jones"], context=2)

    print("\n[3] Oil-test / plug-back language (may name the operator):")
    show_hits(text, [r"plugged back", r"oil test", r"abandoned oil", r"dry hole"], context=2)

    print("\n[4] Wells near 2,410 ft elevation drilled in 1957:")
    show_hits(text, [r"\b2,?410\b", r"5-14-57", r"\b1957\b"], context=1)


def find_index_links(page_html: str, base_url: str) -> list[str]:
    """Pull downloadable index files (xlsx/xls/csv/pdf) out of an RRC page."""
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', page_html, re.IGNORECASE)
    wanted = (".xlsx", ".xls", ".csv", ".pdf")
    out: list[str] = []
    for href in hrefs:
        clean = html.unescape(href)
        if clean.lower().split("?")[0].endswith(wanted):
            full = urllib.parse.urljoin(base_url, clean)
            if full not in out:
                out.append(full)
    return out


def filter_district_rows(text: str, district: str = RRC_DISTRICT) -> None:
    """Print index rows for our district, flagging spans that reach 1957+."""
    pattern = re.compile(rf"(?<![0-9A-Z]){re.escape(district)}(?![0-9A-Z])", re.IGNORECASE)
    years = re.compile(r"(19\d{2})")
    matched = 0
    for line in text.splitlines():
        if not pattern.search(line):
            continue
        matched += 1
        found = [int(y) for y in years.findall(line)]
        # A suspense record from 1957 can surface in any later span, so flag
        # every span whose end year is 1957 or later.
        relevant = not found or max(found) >= 1957
        flag = " <== PULL" if relevant else ""
        print(f"  {line.strip()}{flag}")
    if matched == 0:
        print(f"  No rows mentioning District {district} found in this file.")
    else:
        print(f"\n  {matched} row(s) mention District {district}.")


def step_wildcat(outdir: Path) -> None:
    print("\n" + "=" * 70)
    print("STEP: RRC Wildcat & Suspense roll index, District 7C")
    print("=" * 70)
    print(f"\nFetching index page {WILDCAT_INDEX_PAGE}")
    try:
        raw = fetch(WILDCAT_INDEX_PAGE, outdir / "wildcat-index-page.html")
    except FetchError as exc:
        print(f"  FAILED: {exc}")
        print("\n  Skipping this step.")
        return
    page = raw.decode("utf-8", errors="replace")
    links = find_index_links(page, WILDCAT_INDEX_PAGE)
    if not links:
        print("  No downloadable index files linked. The index may be rendered")
        print("  as an in-page table -- open wildcat-index-page.html and read it.")
    for url in links:
        name = urllib.parse.unquote(url.rsplit("/", 1)[-1].split("?")[0])
        print(f"\nIndex file: {name}")
        try:
            blob = fetch(url, outdir / name)
        except Exception as exc:  # network/404s shouldn't kill the whole run
            print(f"  fetch failed: {exc}")
            continue
        lower = name.lower()
        if lower.endswith(".pdf"):
            filter_district_rows(pdf_to_text(blob))
        elif lower.endswith(".csv"):
            filter_district_rows(blob.decode("utf-8", errors="replace"))
        elif lower.endswith((".xlsx", ".xls")):
            try:
                import openpyxl
            except ImportError:
                print("  Spreadsheet index -- install openpyxl to filter it here.")
                continue
            wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
            rows = []
            for sheet in wb.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    rows.append("\t".join("" if c is None else str(c) for c in row))
            filter_district_rows("\n".join(rows))

    print("\nAlso downloading the film user guides for reference:")
    for name, url in FILM_GUIDES.items():
        try:
            fetch(url, outdir / name)
        except Exception as exc:
            print(f"  {name}: fetch failed: {exc}")


def step_variants() -> None:
    print("\n" + "=" * 70)
    print("STEP: search key matrix")
    print("=" * 70)
    print(f"""
Well ..................... state no. {STATE_WELL_NO} (TWDB site 4556803)
Drilled .................. {DRILLED}
County ................... {COUNTY}, RRC District {RRC_DISTRICT}, county code {COUNTY_CODE}
API prefix ............... 42-{COUNTY_CODE}
Location ................. SE/4 SE/4 Sec. 3, G.C. & S.F. Ry. Co. Survey (block NOT on the card)
Ground elevation ......... 2,410 ft

LEASE NAME is the alphabetical key in pre-1965 RRC filing. Search these first:""")
    for v in LEASE_VARIANTS:
        print(f"    - {v}")
    print("\nOperator/driller variants (secondary -- Kimbell may only be the contractor):")
    for v in OPERATOR_VARIANTS:
        print(f"    - {v}")
    print(f"""
Date windows:
    W-1 permit .............. 1956-01-01 .. 1959-12-31
    Suspense film spans ..... every span from 1957 through 1972

DO NOT filter on 630 ft total depth. 630 ft is the plug-back depth for the water
completion; the oil test's TD is far deeper (Upton County Permian targets run
roughly 2,500-9,000 ft). A shallow-TD screen discards the right well.
""")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--step", choices=["all", "twdb", "wildcat", "variants"], default="all")
    ap.add_argument("--outdir", type=Path, default=Path("./rrc-downloads"))
    args = ap.parse_args()

    args.outdir.mkdir(parents=True, exist_ok=True)
    if args.step in ("all", "variants"):
        step_variants()
    if args.step in ("all", "twdb"):
        step_twdb(args.outdir)
    if args.step in ("all", "wildcat"):
        step_wildcat(args.outdir)


if __name__ == "__main__":
    main()
