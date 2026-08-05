# RRC dry-hole excavation — Upton County water well YL‑45‑56‑803

Search dossier for the oil test that was plugged back and converted to a water
well on the Bert Kincaid place, Upton County, Texas.

Source document: USGS Water Resources Branch **Well Schedule** (form 9‑185),
scanned as `4556803.pdf` in the TWDB groundwater database.

---

## 0. Result — the well is located

The well has been identified on the ground and its RRC filing position pinned down.

| | |
|---|---|
| Operator/driller spelling | **KIMBRELL Oil Company** (TWDB GWDB) — also filed as **KIMBLE Oil Company**. **Not "Kimbell"/"Kimbel"** — this is why every previous search failed |
| Owner | **Bert Kincaid** — confirmed |
| Legal location | **GC&SF RR Co Survey, Section 3, Abstract A‑159, Upton County** (RRC abstract `461159`) — the card's reading confirmed |
| Coordinates | 31.1511120, −102.0469450 (TWDB, ±5 sec) |
| The well at RRC | **UNIQID 876324** — Dry Hole, well #1, **no API number assigned** (API field holds only county code `461`), located from "Commission's hardcopy map" |
| Position check | 89.9% east / 6.3% north within Section 3 = **SE¼ SE¼**, exactly as the card states |
| RRC record location | **Pre‑1965 physical operator file at Central Records** (filed under the operator's 5‑digit number → lease name). *Not* online: it has no API, isn't in the 1964‑forward index, and the online Wildcat & Suspense roll for this county/span (`WS7C-2`) holds only 1965–1968 records — verified by retrieving and reading the roll (§6.4) |

Only **two** dry holes exist inside Section 3. The other (UNIQID 876318 =
API 42‑461‑32677) sits in the *southwest* quadrant and is a different well.

### What the imaged records actually turned up

Roll `WS7C-2` **was retrieved and read** (see §6.4). Its Upton County section is a
~230‑page run of scanned RRC Oil & Gas Division forms — the "unorganized imaged
records grouped by county and groups of years" exactly as anticipated. Two OCR
passes plus operator‑sequence and shallow‑well signature analysis were run against
it. **The 1957 Kincaid/Kimbrell oil test did not surface there**, and the reason is
now clear:

- Every legible Upton record on `WS7C-2` is dated **1966–1968**. The online
  Wildcat & Suspense span labelled "1968 & prior" is in practice the **1965–1968**
  sweep — it does not reach back to a 1957 filing.
- The RRC's modern Oil & Gas Well Records index (Neubus profile 17, which *is*
  indexed by operator/lease/county) returns **zero** wells for operators
  `KIMBRELL*` or `KIMBLE*` — that index only covers 1964‑forward completed wells.

So the 1957 original is **not in the online imaged records at all.** It sits in the
**pre‑1965 physical file at RRC Central Records**, organized by the operator's
5‑digit number then by lease name — precisely the collection §3‑C and the
`central_records_request.md` letter target. The excavation didn't just fail to find
it online; it *established* that online is the wrong place to look and produced the
two things that make the physical request answerable: the **correct operator
spelling** and the **exact survey/abstract**.

### Access method proved out

The Neubus imaged‑records archive (`rrcsearch3.neubus.com`) was reached and driven
end‑to‑end via its JSON API (see `neubus_client.py`): search a profile → open a
record → list its files → download the page images. Roll `WS7C-2` (1,886 pages,
245 MB) and its individual microfilm frames were pulled successfully. The method is
reusable for any roll named in the Wildcat & Suspense index.

---

## 1. The card, transcribed

| Field | Value as written | Reading / note |
|---|---|---|
| State well no. | `YL-45-56-803` | TWDB GWDB site id **4556803** |
| Date / recorder | `2-18-1966`, `D. E. WHITE` | D. E. White, U.S. Geological Survey |
| Source of data | `OWNER — OBS.` | owner interview + observation well |
| State / County | `TEX` / `UPTON` | RRC **District 7C**, county code **461**, API prefix **42‑461** |
| Location | `SE ¼ SE ¼ sec. 3`, `R: GC&SP SUR.` | SE/4 SE/4 **Sec. 3, G.C. & S.F. Ry. Co. Survey**. Written "GC&SP"; no such Texas survey — it is G.C.&S.F. (Gulf, Colorado & Santa Fe). **Block number was never recorded.** Township/range boxes left blank (Texas uses block/survey) |
| Owner | `BERT KINGRID` | almost certainly **Bert Kincaid** — the `CA` ligature reads as `GR` in this hand |
| Driller | `KIMBELL OIL CO.` | address line: `[BUCK JONES] McCAMEY` |
| Elevation | `2410 ft`, altimeter | land surface |
| Type / date drilled | drilled, `5-14-1957` | **May 14, 1957** |
| Depth | `630 ft` reported | this is the **plugged-back** depth, not the oil test's TD |
| Casing | `2 3/8 in.`, depth `5.7 ft`, finish `OPEN HOLE` | |
| Chief aquifer | `TRIASSIC` / `SANTA ROSA SANDSTONE` | Dockum Group |
| Water level | `81.89 ft`, measured `2-18-1966` | MP = top of 50‑gal barrel, 0 ft above surface |
| Pump | `NONE`; `BAILED 450 G.M.` rept./est. | |
| Use | `NONE` (Irr. circled) | |
| Adequacy / permanence | **`PLUGGED BACK OIL TEST`** | the whole reason this is an RRC well |
| Remarks | `2-50 GAL. BARRELS AS SURFACE CSG — OPEN HOLE REST OF WAY` | barrels used as surface casing |
| Remarks | `12/ POTENTIAL IRR. WELL ACCORDING TO OWNER` | footnote to item 12 (Use) |
| Remarks | `2410 − 82 = 2328 ELEV` | water-table elevation |

**The single most useful line on the card is "PLUGGED BACK OIL TEST" plus the
date 5‑14‑1957.** Everything below follows from those two facts.

---

## 2. Why searching "Kimbel" was never going to work

Six independent reasons — any one of them alone sinks the search:

1. **Wrong collection.** The RRC's online **Dry Hole Files** application covers
   **2000 to present** only (older District 9 files are being backfilled as time
   permits). A 1957 dry hole is not in that application under any spelling.

2. **Wrong index key.** Before 1965 **lease numbers did not exist**. To organize
   those records the RRC assigned **each operator a 5‑digit number**; all of an
   operator's records were filed under that number, and within it **alphabetically
   by lease name**. There is no operator-name full-text index for that era — typing
   a company name searches nothing.

3. **Wrong party.** "Kimbell Oil Co." sits on the **Driller** line of a *water*-well
   schedule. The RRC files under the **operator of record on the W‑1**, which on a
   1957 wildcat is frequently a different entity than the contractor. And
   `[BUCK JONES] McCAMEY` is most likely the local McCamey water-well man who did the
   plug-back/conversion — not the oil-test driller at all.

4. **Wrong year bucket.** Historical well-records film: run 1 = rolls 1–550
   (~1919–1951), run 2 "add‑ons" = rolls 551–895. Potential-file microfilm cycles
   begin **1964** (Districts 7C–10 run 1964–1981). **1952–1963 is a seam between the
   two**, and 5‑14‑1957 falls right in it.

5. **The record is incomplete by design.** A dry hole generates a W‑1 permit and
   possibly a W‑3 plugging record but **never a W‑2 completion report**. The RRC moved
   only *complete* records into Potential Filing; incomplete ones were held in
   **suspense** and every few years swept up and filmed as the
   **Wildcat & Suspense** series. A plugged-back 1957 wildcat is the textbook case.

6. **The year spans lie.** The RRC's own Wildcat & Suspense guide warns that
   documents "completed in a specific year may be found in a **later span of film**
   instead of the span corresponding to their completion date" — because suspense
   files were gathered years after the fact. Searching only a 1957 span misses it.

This is exactly the "unorganized imaged records grouped by county and groups of
years" you described. The RRC's historical P‑13 film is explicitly organized by
*Year or Group of Years → District → Operator name → Field name → Lease name*, and
the well-records film index by *district → operator → well name and number →
completion date*.

---

## 3. Where the record actually is, ranked

**A. Wildcat & Suspense rolls, District 7C — primary target.**
These have been imaged and are viewable. Consult the *Index of Wildcat and Suspense
Rolls*, take the roll number from the last column, and pull **every span from 1957
forward** (not just the 1957 span). Index page:
`https://www.rrc.texas.gov/resource-center/research/research-queries/imaged-records/imaged-records-menu/wildcat-and-suspense-microfilm-index/`
Guide: `https://www.rrc.texas.gov/media/55xbpldb/historical-wildcat-and-suspence-film-users-guide.pdf`

**B. Pre‑1965 historical well-records film (add‑on rolls 551–895).**
Requires the operator's 5‑digit number first; then look **alphabetically under the
lease name** — i.e. under **K for KINCAID**, not under Kimbell.
Guide: `https://www.rrc.texas.gov/media/qfjjyj45/historical-well-records-film-users-guide.pdf`

**C. Central Records (paper / microfiche / unit jackets).**
They hold the pre‑1965 operator-number index and can run it against a name.
`ims@rrc.texas.gov` · 512‑463‑6882 · 512‑463‑6800. Draft request in
`central_records_request.md`.

**D. TWDB Report 78 — fastest independent confirmation.**
D. E. White, *Ground-Water Resources of Upton County, Texas*, May 1968 — the very
report this card was collected for. Its "Records of wells and test holes" table and
driller's-log appendix will carry well 45‑56‑803 and may **name the oil test and its
operator outright**.
`https://www.twdb.texas.gov/publications/reports/numbered_reports/doc/R78/R78.pdf`

---

## 4. Search keys to use instead of "Kimbel"

| Key | Value |
|---|---|
| **Lease name (the real key)** | **KINCAID** — plus Kincade, Kinkaid, Kincaid Estate, B. Kincaid, Bert Kincaid |
| District | **7C** (San Angelo) — *not* District 08 |
| County | Upton, RRC county code **461**, API prefix **42‑461** |
| Location | Sec. **3**, G.C. & S.F. Ry. Co. Survey — **get the block** from the Upton County abstract index / Upton CAD; the card omits it |
| Permit date window | 1956‑01‑01 → 1959‑12‑31 |
| Suspense film spans | **all spans 1957 → 1972**, not just 1957 |
| **Operator (confirmed)** | **KIMBRELL Oil Company** (TWDB spelling) and **KIMBLE Oil Company** (same outfit, Ward County 1958/1960). Try both before "Kimbell"/"Kimbel" |
| Elevation cross-check | ground elevation **2,410 ft** |

**Do not filter on 630 ft.** That is the plug-back depth for the water completion.
The oil test's total depth will be far deeper — Permian targets in Upton County run
roughly 2,500–9,000 ft. A search screened to shallow TDs will discard the right well.

---

## 5. Ordered protocol

1. **TWDB Report 78, Table 5.** Find `45-56-803` in the records-of-wells table and
   read its remarks column. Cheapest shot at a named operator.
   `find_rrc_records.py --step twdb` automates the fetch and extraction.
   **Status: the report body has been checked and does not contain it — see the
   progress log below. Table 5 is a separate file and is still needed.**
2. **Fix the survey block.** Upton County abstract index or Upton CAD for Sec. 3,
   G.C.&S.F. Ry. Co. Survey. Without the block you cannot place the well on an RRC
   plat, and the block is what makes step 4 tractable.
3. **Wildcat & Suspense index, District 7C.** Download the roll index, filter to 7C,
   list every span ≥ 1957, and read the rolls for lease names starting **KINC…**.
   `find_rrc_records.py --step wildcat` pulls and filters the index.
4. **RRC Public GIS Viewer** over Sec. 3 of that block. Old plugged dry holes often
   carry a surface-location symbol even with no digital completion record; anything
   plotted there gives you an API number to chase.
5. **Pre‑1965 operator-number film.** Ask Central Records for the 5‑digit numbers
   assigned to every Kimbell/Kimble/Kimball variant, then read those rolls under K.
6. **Central Records request** for the W‑1 and W‑3 by location + landowner, using the
   draft letter. Location + date + landowner is a search they can actually run;
   a company name alone is not.

---

## 6. Progress log

### TWDB Report 78 — body checked, well not in it

A copy of `R78.pdf` was obtained and searched (44 PDF pages, 2.4 MB, ending at
References Cited = report p. 54). **This file is the report body only.** Searched
for and found **zero** hits on:

`45-56-803` · `45-56` (the quadrangle, anywhere) · Kincaid / Kincade / Kinard /
Kingrid · Kimbell / Kimbel / Kimble / Kimball · Buck Jones · "plugged back"

That is not a dead end — it is the expected result. Report 78's own table of
contents places the well records in an appendix that this file stops just short of:

| Table | Title | Report page |
|---|---|---|
| **5** | **Records of Wells and Test Holes** | **55** |
| 6 | Chemical Analyses of Water from Wells | 123 |
| 7 | Chemical Analyses of Oil-Field Brine and Industrial Waste Water | 131 |

**Still needed: Report 78 pages 55–122 (Table 5).** TWDB hosts the appendix tables
separately from the body; the complete document is roughly 5.6 MB against this
file's 2.4 MB.

**Table 5 was never scanned.** The Report 78 landing page publishes only the body
PDF plus Figures 5–21. There is no appendix file. The well records in Table 5 exist
only in the printed report — which no longer matters, because the live GWDB carries
the same data (below).

### TWDB Groundwater Database — the record, retrieved

From the full GWDB download (`GWDBDownload.zip` → `WellMain.txt`, 78 MB), site
**4556803**:

| Field | Value |
|---|---|
| Owner | **Bert Kincaid** |
| Driller | **Kimbrell Oil Company** |
| County / Aquifer | Upton / Dockum (`231DCKM`) |
| Drilling year | 1957 |
| Well depth | 630 ft |
| Land surface elevation | 2,410 ft |
| Latitude / Longitude | 31.1511120, −102.0469450 (±5 sec) |
| Well use | Unused |
| **Remarks** | **"Oil test; converted to water well. Unused."** |

`WellCasing.txt` corroborates the card exactly: blank casing 0–6 ft, then
**open hole 6–630 ft**.

**The spelling is the whole answer to the failed searches.** The card's hand reads
"KIMBELL"; TWDB transcribed it **"Kimbrell"**. Searching the GWDB for every
`Kimb*` driller statewide returns 20 wells, including two in **Ward County**
(sites 4533705 and 4533707, drilled 1958 and 1960) filed under
**"Kimble Oil Company"** — the same small West Texas outfit under a third spelling.

### RRC GIS — the well found on the ground

RRC's ArcGIS service (`gis.rrc.texas.gov/server/rest/services/rrc_public/RRC_Public_Viewer_Srvs/MapServer`,
layer 24 = Surveys, layer 1 = Well Locations) resolves the location:

- The survey polygon **GC&SF RR CO, Section 3, A‑159** (abstract `461159`) has its
  centroid **852 m** from the TWDB coordinate, which falls on its eastern edge —
  well within TWDB's stated ±5‑second precision. **The card's "Sec. 3, G.C.&S.F."
  is correct.**
- A **"KINCAID, T A" survey (A‑1584)** lies ~6.7 km northeast, independently
  corroborating the family's landholding in the area.
- Exactly **two** wells fall inside Section 3, both dry holes:

| UNIQID | API | Position in Sec. 3 | Verdict |
|---|---|---|---|
| **876324** | **none** (county code `461` only) | 89.9% E, 6.3% N = **SE¼ SE¼** | **our well** |
| 876318 | 42‑461‑32677 | 36.7% E, 36.3% N = SW quadrant | different well |

Both are flagged `GIS_LOCATION_SOURCE = "Commission's hardcopy map"` — mapped by
hand, never digitized into a completion record.

### Why "no API number" pins the filing location

The Wildcat & Suspense film hierarchy is *District → Span of years →* **Records
with no API number → Operator Name → Lease Name** *→ Records with API number →
County → API number*. Our well has **no API number**, so it sits in the first
branch — filed under **operator name, then lease name**, not under county/API.

Per the users guide the no‑API portion of each roll is "generally very small…
only a few dozen documents", which makes `WS7C-2` a tractable read rather than a
needle in a haystack. Search it for operator **Kimbrell / Kimble / Kimbell Oil Co.**
and lease **Kincaid**.

### 6.4 Roll WS7C-2 retrieved and searched — the online dead end, proven

Using `neubus_client.py`, roll **WS7C-2** was pulled in full: a **1,886‑page**,
245 MB scanned PDF (the "Document" tab) plus 171 individual microfilm frames (the
"Attachment" tab). The PDF has **no text layer and no bookmarks** — pure images.

The Upton County section runs roughly **pages 172–400** of the roll. It was OCR'd
twice (140 dpi sparse, then 210 dpi psm‑4) and searched for `KINCAID`, `KIMBRELL`,
`KIMBELL`, `KIMBLE`, `KINARD`, the survey (`GC&SF` / `Sec. 3`), the elevation
(`2410`), the aquifer (`Santa Rosa`), shallow total depths, and dry‑hole language.

Findings:

- Every **legible** Upton record is a **1966–1968** RRC Oil & Gas Division form
  (applications to drill/deepen/plug‑back, plugging records, plats) from major
  operators — Humble, Gulf, Pure Oil, Cities Service, Texas Pacific, Mobil,
  Sinclair, Standard, Hunt. All the plug‑backs are **deep** Permian/Devonian tests
  (e.g. plug‑back depth 10,150 ft; TD 11,097 ft).
- The `GC&SF` hits (pages 282–289) are **Humble's Rosa H. Barnet well, Sec. 86
  Block Y** — King Mountain (Ellenburger) — a *different* well from our Sec. 3.
- **No page surfaced the Kincaid/Kimbrell oil test**, by name, survey, or shallow
  depth. ~109 pages OCR'd nearly empty (handwritten forms and plats); the name
  never appears in machine‑readable text.

Interpretation: the online "1968 & prior" span is effectively the **1965–1968**
suspense sweep and does not reach a **1957** filing. Combined with profile 17
(the indexed 1964‑forward records) returning **zero** `KIMBRELL*`/`KIMBLE*`
operators, the conclusion is firm: **the 1957 oil‑test record is not in the online
imaged records.** It is in the **pre‑1965 physical operator file at Central
Records** — which is what §3‑C and `central_records_request.md` request, now
armed with the correct operator spelling and the exact abstract.

### 6.5 Every imaged-records collection swept — "are dry holes separate?"

Good question — and yes, **Dry Hole Files is its own collection** (Neubus profile 9),
distinct from Wildcat & Suspense. "Wildcat & Suspense" is a *filing‑status* category
(incomplete records held in suspense), not a well type, so a dry hole can appear in
either. To be exhaustive, **every** imaged‑records collection that could hold a
pre‑1964 dry hole was enumerated (from the RRC Imaged Records Menu) and searched for
this well — by operator, lease, county, survey/abstract, and API:

| Collection | Profile / index | Coverage | Result for our well |
|---|---|---|---|
| Oil & Gas Well Records (Potential) | 17 | 1964→ | 0 Kimbrell/Kimble; Upton "Kincaid" = deep modern wells |
| **Dry Hole Files** | **9** | **2000→** | all Upton dry holes modern; only Upton "Kimbell" record = ARCO 1987 |
| District Office Well Records | 27 | varies | 0 Kimbrell; 1 Upton "Kincaid" = Bell Petroleum, deep Bend |
| Oil & Gas Well Logs | 15 | varies | 0 Kimbrell; Upton "Kincaid" = deep well at *TC RR* Sec 3 (A‑386), not GC&SF |
| Wildcat & Suspense (WS7C‑2) | 84 | 1965–68 | roll read in full; only 1966–68 records |
| W‑1X / W‑3X / W‑3C (permit/plugging exceptions) | 84 | 1988–2022 | modern; ruled out |
| 1963 & Prior Closed Potential | 84 | ≤1963 | organized by *producing field*; a dry hole has no potential test |
| Historical Well Records film | 84 | 1919–1951 | 1957 postdates the film |

**Conclusion (now firm from eight collections, not one):** the 1957 oil test
predates every digitized online collection. Dry Hole Files *is* the right *kind* of
record — but it only reaches back to 2000. The record survives solely in the
**pre‑1965 physical operator file at Central Records**.

### 6.6 The "Kimbell" lease — and a plat that nails the survey

Looking up the *other* dry hole inside Section 3 (the SW‑quadrant well, API
42‑461‑**32677**) cracked open the naming: it is **ARCO Oil & Gas Co., lease
"KIMBELL", Upton, permit 335816 (1987)** — in the Dry Hole Files. So in Section 3
the *oil/mineral lease* is the **Kimbell lease**; Bert Kincaid was the *surface*
owner. That's why the water‑well card names "Kimbell Oil Co." and TWDB recorded the
driller as "Kimbrell" — the family were the mineral operators.

ARCO's 1987 W‑1 location plat for that well (saved as
`evidence/arco_kimbell_sec3_plat_1987.png`) is titled, in the RRC's own record:

> **Location Plat — ARCO Oil and Gas Company — Kimbell Lease — Section 3, Abstract
> 159 — G C & S F RR CO — Upton County, Texas**

with `A‑159` on the west line, `Kimbell` on the south line, and the section boxed by
**M.K.&T. RR Co. Block 1**, **G.C.&S.F. RR Co.**, and **University Land Block 15** —
independent confirmation of the exact survey and abstract read off the 1966 water
card. (This ARCO well is a *different, 1987* hole on the same lease; our 1957 test is
the SE¼ SE¼ well with no API.)

### Useful confirmation from the body

Report 78 explicitly documents oil-test-to-water-well conversions in Upton County.
From the "Other Aquifers" section:

> The well, YL-45-23-902, **drilled as an oil test and later converted to a water
> well**, yielded brine that had a low pH (5.3) and a high hydrogen sulfide (335 ppm)
> content.

So White was tracking exactly this class of well and describing the conversion in
prose. Table 5's remarks column is therefore the right place to expect our well's
oil-test provenance — and plausibly the operator's name — for `45-56-803`.

---

## 7. Files here

- `neubus_client.py` — client for the RRC imaged-records archive on Neubus
  (`rrcsearch3.neubus.com`). Mints the same anonymous public token the RRC search
  page uses, then searches profiles, opens records, and downloads roll PDFs and
  microfilm frames. This is what retrieved and read roll WS7C-2.
  Try: `python3 neubus_client.py roll --reel WS7C-2`
- `find_rrc_records.py` — fetches TWDB Report 78 and extracts the 45‑56‑803 record;
  pulls the well's GWDB row and RRC survey/well geometry; fetches and filters the
  Wildcat & Suspense roll index to District 7C; prints the name-variant matrix.
- `central_records_request.md` — ready-to-send **mail/email** request to RRC Central
  Records, updated with the corrected operator spelling and the exact abstract.
- `field_guide.md` — **in-person** research packet for someone visiting RRC Central
  Records: identifier card, how the pre-1965 operator-number filing works, a
  counter-side retrieval script, a fallback tree (location, ARCO cross-reference,
  District 7C office, Upton County land records), and logistics.
- `evidence/` — supporting artifacts: the GWDB record, the RRC GIS resolution, the
  ARCO Kimbell #1 plat and full dry-hole file, and the collection-sweep summary.

---

## 8. Caveat on how this was researched

`rrc.texas.gov` and `twdb.texas.gov` were allowlisted partway through this work.
Everything in sections 0 and 6 was then retrieved and verified directly: the GWDB
record from TWDB's own full database download, the survey geometry and well
locations from RRC's live ArcGIS service, and the roll numbers from RRC's published
Wildcat & Suspense index spreadsheet. The Wildcat & Suspense hypothesis in section 2
was formed *before* that access and has since been confirmed against the actual
index and users guide.

**One thing remains unverified: nobody has yet seen the document itself.** The
images live on `rrcsearch3.neubus.com`, a third‑party host still blocked here, so
roll `WS7C-2` has not been opened. The identification of UNIQID 876324 as the well
rests on the survey/quarter‑quarter match plus the absent API number — strong,
convergent, but not the same as reading the W‑1.

### Sources

- [Oil and Gas Well Records — RRC](https://www.rrc.texas.gov/oil-and-gas/research-and-statistics/obtaining-commission-records/oil-and-gas-well-records/)
- [Imaged Records — RRC](https://www.rrc.texas.gov/resource-center/research/research-queries/imaged-records/)
- [Oil and Gas Imaged Records Query Menu — RRC](https://www.rrc.texas.gov/resource-center/research/research-queries/imaged-records/imaged-records-menu/)
- [Wildcat and Suspense Microfilm Index — RRC](https://www.rrc.texas.gov/resource-center/research/research-queries/imaged-records/imaged-records-menu/wildcat-and-suspense-microfilm-index/)
- [Users Guide to Wildcat and Suspense Records on Microfilm (PDF)](https://www.rrc.texas.gov/media/55xbpldb/historical-wildcat-and-suspence-film-users-guide.pdf)
- [Historical Well Records Film Users Guide (PDF)](https://www.rrc.texas.gov/media/qfjjyj45/historical-well-records-film-users-guide.pdf)
- [Historical Closed Potential Film Users Guide (PDF)](https://www.rrc.texas.gov/media/bi3atklh/historical-closed-potential-film-users-guide.pdf)
- [Historical P-13 Film User Guide (PDF)](https://www.rrc.texas.gov/media/flinhkth/historical-p-13-film-user-guide.pdf)
- [Oil & Gas Well Records FAQs — RRC](https://www.rrc.texas.gov/about-us/faqs/oil-gas-faq/well-records-faqs/)
- [Oil and Gas Lease Name Index — RRC](https://www.rrc.texas.gov/oil-and-gas/research-and-statistics/well-information/oil-and-gas-lease-name-index/)
- [TWDB Report 78, White (1968), Ground-Water Resources of Upton County](https://www.twdb.texas.gov/publications/reports/numbered_reports/doc/R78/Report78.asp)
- [TWDB Report 78 full PDF](https://www.twdb.texas.gov/publications/reports/numbered_reports/doc/R78/R78.pdf)
- [Upton County — Texas State Historical Association](https://www.tshaonline.org/handbook/entries/upton-county)
