# TCEQ / ERCOT Permit & Interconnection Database

A second, self-contained tool in this repository. It joins four public Texas
feeds into one database of **sites**, so that a generation project, a data
center, and the earthwork that preceded both stop being three unrelated rows and
become one development you can reason about.

| Feed | Source id | What it tells you |
|---|---|---|
| ERCOT Generator Interconnection Status (GIS) report | `ercot_gis` | Every generation interconnection request: fuel, technology, nameplate MW, county, POI, study milestones, projected COD |
| ERCOT Large Load interconnection status report | `ercot_large_load` | Data centers, crypto miners, industrial electrification: requested MW, county, energization date, whether the load is behind a generator's meter |
| TCEQ air New Source Review permit applications | `tceq_air` | What actually burns: turbines, engines, backup gensets — with fuel, ratings, NAICS/SIC, RN/CN numbers and site coordinates |
| TCEQ stormwater construction NOIs (TXR150000) | `tceq_swnoi` | The earliest signal there is: disturbed acreage, operator and site coordinates, typically 12–24 months before energization |

## Quick start

No network needed — this builds a synthetic dataset and runs it through the real
pipeline end to end:

```bash
python permits_cli.py demo
streamlit run permits_dashboard.py
```

With real data:

```bash
python permits_cli.py fetch                   # ERCOT MIS, where reachable
python permits_cli.py ingest --dir data/raw   # anything downloaded by hand
python permits_cli.py link
python permits_cli.py stats
```

## Getting the source files

**File ingest is the primary path.** The upstream sites change layout and access
rules; downloading an export by hand and pointing `ingest` at it always works,
and the adapters resolve column names loosely so a renamed header degrades to a
missing optional field rather than a crash.

- **ERCOT GIS report** — ERCOT MIS, monthly. `permits_cli.py fetch` pulls the
  newest workbook through the MIS document API
  (`IceDocListJsonWS` → `mirDownload`). The report type id defaults to `15933`
  and can be overridden with `ERCOT_GIS_REPORT_TYPE_ID`.
- **ERCOT Large Load report** — same API, but ERCOT reorganizes its report
  catalog, so no id is assumed. Set `ERCOT_LARGE_LOAD_REPORT_TYPE_ID` once you
  have confirmed it from the MIS listing, or just download the workbook and use
  `--file`.
- **TCEQ air NSR applications** — the pending/issued permit lists at
  <https://www.tceq.texas.gov/permitting/air/nav/air_pendingpermits.html>, or a
  Central Registry query export from <https://www15.tceq.texas.gov/crpub/>.
  csv, xlsx and saved HTML results tables all work.
- **TCEQ stormwater NOIs** — the water quality general permit search at
  <https://www2.tceq.texas.gov/wq_dpa/index.cfm> filtered to TXR150000.
  `tceq_stormwater.search_url(county=...)` builds the query URL for you.

Drop files in `data/raw/`; `ingest --dir` infers the source from the filename
(override with `--source`). Files are hashed, so re-running is cheap and an
unchanged monthly report is skipped.

## How records become sites

Everything hinges on the linking step. Records from different agencies never
share a key, so pairs are scored on three independent axes:

| Axis | What it measures |
|---|---|
| spatial | Great-circle distance, when **both** records carry real site coordinates |
| name | Token-set similarity of normalized project names (corporate suffixes, roman numerals and filler words removed) |
| operator | Token-set similarity of normalized company names |

Two shortcuts and one guard rail matter:

- **A shared TCEQ regulated-entity (RN) number is identity.** That *is* TCEQ's
  site identifier; the pair links at 0.98 regardless of anything else.
- **County-only geography is weak evidence.** Records without coordinates get
  their county centroid and are tagged `geo_precision='county'`. For those,
  "same county" contributes little and the name/operator evidence has to carry
  the match — otherwise every solar project in Pecos County would merge.
- **Real coordinates far apart are evidence *against*.** Beyond 5 km, a pair is
  rejected outright even with identical names; those are separate phases.

Pairs at or above the threshold (default `0.62`, tunable with `--threshold`) are
unioned into clusters, and each cluster is summarized into one site. Both derived
tables are rebuilt from scratch by `link`, so tuning the threshold and re-running
is the intended workflow.

Every join is inspectable:

```bash
python permits_cli.py explain site:5c97359f75fe48a3
```

...and the dashboard's **Site detail** tab shows the same evidence table.

## Classification

Two independent questions, each answered with a confidence and the evidence
behind it:

**What is it?** `generation` · `data_center` · `crypto_mining` ·
`industrial_load` · `construction` · `unknown`

The ladder, highest confidence first: source structure (an ERCOT GIS row is
generation by construction) → NAICS/SIC code → known operator name → keyword in
the project name or description → weak contextual inference. A bare large-load
entry with no descriptive text is tagged `data_center` at confidence 0.30 and
says so in its evidence string — data centers are the modal large load, but that
is an inference, not a filing.

Crypto is checked before data center deliberately: mining sites are routinely
described as data centers, and the narrower label should win.

**What does it burn?** Canonical fuels (`natural_gas`, `solar`, `wind`,
`battery_storage`, `nuclear`, `coal`, `petroleum`, `hydro`, `biomass`,
`geothermal`, `hydrogen`, `other`) plus a prime mover (`combined_cycle`,
`combustion_turbine`, `reciprocating_engine`, `battery`, …). ERCOT fuel codes map
directly; free text is parsed otherwise. ERCOT files batteries as fuel `OTH` with
the detail in Technology, so text detail refines an `other` code rather than
losing to it.

`is_dispatchable(fuel)` marks the fuels that can serve load on demand — the
property that matters when judging whether colocated generation could actually
back a data center.

## Counting capacity without double counting

A 400 MW plant appears once in the ERCOT queue and again on its TCEQ air permit.
Naively summing a site's records would report 800 MW.

The rule: **sum within each source, then keep the largest source total.** Summing
within a source is still correct, because one site legitimately holds several
queue entries — a solar project plus its colocated battery. `sites.gen_mw` and
`sites.load_mw` both use this, and `link.select_capacity_records()` exposes the
same subset so the dashboard's charts total to the site figures displayed beside
them.

## Schema

```
source_files    provenance: one row per ingested file or API pull, with sha256
entities        one normalized record per source row — the unit of matching.
                Keeps the untouched source row in raw_json, so every derived
                column can be recomputed without re-downloading anything.
                first_seen / last_seen track queue history: a project that drops
                out of the ERCOT report stays visible with an older last_seen.
entity_links    pairwise match evidence: score, method, distance, sub-scores
sites           resolved clusters, fully derived from entities + entity_links
site_members    cluster membership
```

Site classes: `colocated_gen_load`, `generation_only`, `load_only`,
`construction_only`, `mixed`.

## Dashboard

```bash
streamlit run permits_dashboard.py     # reads output/permits.db, or $PERMITS_DB
```

Tabs: **Map** (sites by class, area ∝ MW), **Colocated gen + load** (the join
that motivates the whole tool), **Generation** (capacity by fuel and COD year,
size distribution), **Loads** (requested MW by type and year), **Site detail**
(member records plus the link evidence), **All records** (everything before
resolution, with CSV download).

The map uses OpenStreetMap tiles; tick **Offline basemap** in the sidebar to draw
from bundled US geometry instead when the machine has no internet access.

Records placed at a county centroid are labelled as approximate on the map and
carry `geo_precision='county'` in the data — they are never presented as surveyed
locations.

## Caveats

- **Classification is inference, not a filing.** Nothing in ERCOT's large-load
  report says "data center" in a structured field. Check `kind_confidence` and
  `kind_evidence` before treating a label as fact; the dashboard surfaces both.
- **Colocation is inferred from proximity and shared operators** except where
  ERCOT itself flags a load as behind-the-meter. A 3–5 km cluster is a strong
  hint, not a confirmed electrical arrangement.
- **County centroids are approximate** and exist only so coordinate-less records
  appear on the map at all.
- **The demo dataset is entirely synthetic.** Every project, company, permit
  number and coordinate in `src/permits/seed.py` is invented. Databases built
  from it are flagged, and the dashboard says so at the top.

## Tests

```bash
python -m pytest tests/test_permits.py -v
```

Covers normalization, both classifiers, column resolution against renamed
headers, upsert idempotency, every scoring branch, and an end-to-end build of the
demo dataset that asserts the 18 invented developments resolve to exactly 18
sites with the 4 expected colocated ones.
