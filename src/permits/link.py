"""
link.py — resolve records from four feeds into physical sites.

The whole point of the database is this step: an ERCOT queue entry, a TCEQ air
permit and a stormwater NOI that all describe one development have to collapse
into a single site before "is there a data center next to that gas plant?" can be
answered.

Matching strategy
-----------------
Candidate pairs come from cheap blocking keys (county, shared TCEQ regulated
entity number, shared operator) rather than an all-pairs sweep. Each candidate is
then scored on three independent axes:

    spatial   how close the two sites are, when both carry real coordinates
    name      token-set similarity of normalized project names
    operator  token-set similarity of normalized company names

A shared TCEQ regulated-entity (RN) number short-circuits all of it — that *is*
TCEQ's site identifier, so two records carrying the same RN are the same site.

Records with only a county centroid for geography get a different, stricter
weighting: "same county" is weak evidence, so name and operator have to carry
the match. Without that distinction, every solar project in Pecos County would
merge into one blob.

Clusters are formed by union-find over links at or above `threshold`, then
summarized into the `sites` table.
"""

import hashlib
from collections import defaultdict

from . import classify, status
from .db import utcnow
from .normalize import haversine_km, name_similarity

# Scoring knobs. Exposed as module constants so the CLI can override them and
# the effect is visible in one place.
DEFAULT_THRESHOLD = 0.62      # minimum combined score to link two records
SAME_SITE_KM = 1.0            # at or under this, spatial evidence is maximal
MAX_LINK_KM = 5.0             # beyond this, coordinates are evidence *against*
RN_MATCH_SCORE = 0.98         # shared TCEQ regulated-entity number
MIN_NAME_FOR_COUNTY_MATCH = 0.55
MIN_OPERATOR_FOR_COUNTY_MATCH = 0.80

# Preference order when picking a site's display name / operator.
SOURCE_PRIORITY = ["ercot_gis", "ercot_large_load", "tceq_air", "tceq_swnoi"]

# A site's lifecycle is the most advanced state any of its records reached: a
# campus with an issued air permit and a pending amendment is an operating or
# approved site that also has something in process, not a pending one.
_SITE_LIFECYCLE_PRECEDENCE = [
    status.OPERATING, status.APPROVED, status.PENDING,
    status.DENIED, status.WITHDRAWN, status.EXPIRED, status.UNKNOWN,
]

SITE_GENERATION_ONLY = "generation_only"
SITE_LOAD_ONLY = "load_only"
SITE_COLOCATED = "colocated_gen_load"
SITE_CONSTRUCTION_ONLY = "construction_only"
SITE_MIXED = "mixed"


class UnionFind:
    """Minimal union-find over string ids."""

    def __init__(self):
        self.parent = {}

    def add(self, item):
        self.parent.setdefault(item, item)

    def find(self, item):
        self.add(item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        # Path compression.
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b):
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a

    def groups(self):
        clusters = defaultdict(list)
        for item in self.parent:
            clusters[self.find(item)].append(item)
        return clusters


def spatial_score(a, b):
    """Return (score, distance_km, comparable).

    `comparable` is False when at least one record lacks real site coordinates,
    which tells the caller to fall back to the county-based weighting.
    """
    if a["geo_precision"] != "site" or b["geo_precision"] != "site":
        return (0.0, None, False)
    distance = haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"])
    if distance is None:
        return (0.0, None, False)
    if distance <= SAME_SITE_KM:
        return (1.0, distance, True)
    if distance >= MAX_LINK_KM:
        return (0.0, distance, True)
    # Linear decay between the two radii.
    return (
        1.0 - (distance - SAME_SITE_KM) / (MAX_LINK_KM - SAME_SITE_KM),
        distance,
        True,
    )


def score_pair(a, b):
    """Score two entity dicts. Returns (score, method, distance_km, name, operator)."""
    name_score = name_similarity(a.get("name_norm"), b.get("name_norm"))
    operator_score = name_similarity(a.get("operator_norm"), b.get("operator_norm"))

    # A shared TCEQ regulated-entity number is an identity statement.
    rn_a, rn_b = a.get("regulated_entity"), b.get("regulated_entity")
    if rn_a and rn_b and str(rn_a).strip() == str(rn_b).strip():
        return (RN_MATCH_SCORE, "tceq_regulated_entity", None, name_score, operator_score)

    spatial, distance, comparable = spatial_score(a, b)

    if comparable:
        if spatial <= 0.0:
            # Both have real coordinates and they are far apart. Nothing else can
            # rescue the pair — identical names at 40 km are different phases.
            return (0.0, "distance_reject", distance, name_score, operator_score)
        score = 0.55 * spatial + 0.25 * name_score + 0.20 * operator_score
        return (score, "geo", distance, name_score, operator_score)

    # County-only geography: require the counties to agree, and make the textual
    # evidence carry the match.
    county_a, county_b = a.get("county_norm") or "", b.get("county_norm") or ""
    if not county_a or not county_b or county_a != county_b:
        return (0.0, "no_common_geography", None, name_score, operator_score)

    if (
        name_score < MIN_NAME_FOR_COUNTY_MATCH
        and operator_score < MIN_OPERATOR_FOR_COUNTY_MATCH
    ):
        return (0.0, "county_only_weak_text", None, name_score, operator_score)

    score = 0.15 + 0.50 * name_score + 0.35 * operator_score
    return (score, "county_and_name", None, name_score, operator_score)


def _candidate_pairs(entities):
    """Yield index pairs worth scoring, from blocking keys.

    Blocking keys: county, TCEQ regulated-entity number, and normalized operator.
    A record can sit in several blocks; pairs are de-duplicated by the caller.
    """
    blocks = defaultdict(list)
    for index, entity in enumerate(entities):
        county = entity.get("county_norm")
        if county:
            blocks[("county", county)].append(index)
        rn = entity.get("regulated_entity")
        if rn:
            blocks[("rn", str(rn).strip())].append(index)
        operator = entity.get("operator_norm")
        if operator:
            blocks[("operator", operator)].append(index)

    seen = set()
    for key, members in blocks.items():
        # A block that swallows a large share of the dataset carries no signal;
        # skipping it keeps the pass near-linear without losing real matches,
        # which will also be reachable through a tighter block.
        if len(members) > 4000:
            continue
        for position, left in enumerate(members):
            for right in members[position + 1:]:
                pair = (left, right) if left < right else (right, left)
                if pair not in seen:
                    seen.add(pair)
                    yield pair


def build_links(entities, threshold=DEFAULT_THRESHOLD):
    """Score candidate pairs and return the links that clear `threshold`.

    `entities` is a list of dicts (sqlite3.Row works too). Returns a list of
    dicts ready for the entity_links table.
    """
    entities = [dict(entity) for entity in entities]
    links = []
    for left, right in _candidate_pairs(entities):
        a, b = entities[left], entities[right]
        score, method, distance, name_score, operator_score = score_pair(a, b)
        if score < threshold:
            continue
        first, second = sorted((a["entity_id"], b["entity_id"]))
        links.append(
            {
                "entity_a": first,
                "entity_b": second,
                "score": round(float(score), 4),
                "method": method,
                "distance_km": round(distance, 3) if distance is not None else None,
                "name_score": round(float(name_score), 4),
                "operator_score": round(float(operator_score), 4),
            }
        )
    return links


def _site_id(entity_ids):
    digest = hashlib.sha1("|".join(sorted(entity_ids)).encode("utf-8")).hexdigest()
    return f"site:{digest[:16]}"


def _pick_display(members, field):
    """Pick a display value by source priority, then by longest string."""
    best, best_rank = None, (len(SOURCE_PRIORITY) + 1, 0)
    for member in members:
        value = member.get(field)
        if not value:
            continue
        try:
            priority = SOURCE_PRIORITY.index(member.get("source"))
        except ValueError:
            priority = len(SOURCE_PRIORITY)
        rank = (priority, -len(str(value)))
        if rank < best_rank:
            best, best_rank = value, rank
    return best


def _winning_source(members, field, kinds=None, fuels=None):
    """Pick the source whose records account for a site's capacity.

    Returns (source, total). Ties break on source name so the choice is stable
    across runs. See `_capacity_by_source` for why the largest source wins.
    """
    totals = defaultdict(float)
    for member in members:
        if kinds and member.get("project_kind") not in kinds:
            continue
        if fuels and member.get("fuel") not in fuels:
            continue
        value = member.get(field)
        if value is None:
            continue
        totals[member.get("source")] += float(value)
    if not totals:
        return (None, None)
    source = max(sorted(totals), key=lambda name: totals[name])
    return (source, round(totals[source], 2))


def _capacity_in_lifecycle(members, field, source, lifecycles, kinds=None):
    """Sum one source's records that sit in the given lifecycle states.

    Restricting to the *winning* source is what keeps the approved/pending split
    a true partition of the site total. Without it, a plant with an executed
    interconnection agreement but a pending air permit would report its full
    capacity as approved and again as pending — the same megawatts twice.
    """
    if source is None:
        return None
    total = 0.0
    found = False
    for member in members:
        if member.get("source") != source:
            continue
        if kinds and member.get("project_kind") not in kinds:
            continue
        if member.get("lifecycle") not in lifecycles:
            continue
        value = member.get(field)
        if value is None:
            continue
        total += float(value)
        found = True
    return round(total, 2) if found else None


def _capacity_by_source(members, field, kinds=None, fuels=None, lifecycles=None):
    """Sum a capacity field within each source, then take the largest source total.

    Summing across sources would double-count: a 400 MW plant appears once in the
    ERCOT queue and again on its TCEQ air permit. Summing *within* a source is
    still right, because one site can hold several distinct queue entries (a
    solar project plus its colocated battery).
    """
    totals = defaultdict(float)
    seen = set()
    for member in members:
        if kinds and member.get("project_kind") not in kinds:
            continue
        if fuels and member.get("fuel") not in fuels:
            continue
        if lifecycles and member.get("lifecycle") not in lifecycles:
            continue
        value = member.get(field)
        if value is None:
            continue
        totals[member.get("source")] += float(value)
        seen.add(member.get("source"))
    if not totals:
        return None
    return round(max(totals.values()), 2)


def summarize_site(members, link_scores):
    """Build one `sites` row from its member entity dicts."""
    entity_ids = [member["entity_id"] for member in members]
    site_id = _site_id(entity_ids)

    kinds = {member.get("project_kind") for member in members}
    has_generation = classify.KIND_GENERATION in kinds
    has_load = bool(kinds & classify.LOAD_KINDS)

    if has_generation and has_load:
        site_class = SITE_COLOCATED
    elif has_generation:
        site_class = SITE_GENERATION_ONLY
    elif has_load:
        site_class = SITE_LOAD_ONLY
    elif kinds <= {classify.KIND_CONSTRUCTION, classify.KIND_UNKNOWN, None}:
        site_class = SITE_CONSTRUCTION_ONLY
    else:
        site_class = SITE_MIXED

    gen_source, gen_mw = _winning_source(
        members, "capacity_mw", kinds={classify.KIND_GENERATION}
    )
    load_source, load_mw = _winning_source(members, "load_mw")
    dispatchable_fuels = {
        fuel for fuel in classify.FUEL_ORDER if classify.is_dispatchable(fuel)
    }
    dispatchable_mw = _capacity_by_source(
        members, "capacity_mw",
        kinds={classify.KIND_GENERATION}, fuels=dispatchable_fuels,
    )

    # Split the same capacity the headline uses, so approved + pending never
    # exceeds gen_mw. A site with an issued permit and a pending amendment shows
    # both parts; a site whose single project is approved in one feed and pending
    # in another counts once, with has_pending flagging the open action.
    gen_mw_approved = _capacity_in_lifecycle(
        members, "capacity_mw", gen_source, status.AUTHORIZED,
        kinds={classify.KIND_GENERATION},
    )
    gen_mw_pending = _capacity_in_lifecycle(
        members, "capacity_mw", gen_source, status.IN_PROCESS,
        kinds={classify.KIND_GENERATION},
    )
    load_mw_approved = _capacity_in_lifecycle(
        members, "load_mw", load_source, status.AUTHORIZED
    )
    load_mw_pending = _capacity_in_lifecycle(
        members, "load_mw", load_source, status.IN_PROCESS
    )

    member_lifecycles = {member.get("lifecycle") for member in members}
    site_lifecycle = next(
        (value for value in _SITE_LIFECYCLE_PRECEDENCE if value in member_lifecycles),
        status.UNKNOWN,
    )
    has_pending = bool(member_lifecycles & status.IN_PROCESS)

    programs = sorted(
        {member.get("permit_program") for member in members if member.get("permit_program")}
    )
    nox_tpy = sum(
        member["nox_tpy"] for member in members if member.get("nox_tpy") is not None
    ) or None

    fuels = [
        fuel for fuel in classify.FUEL_ORDER
        if fuel in {member.get("fuel") for member in members}
    ]
    technologies = sorted(
        {member.get("technology") for member in members if member.get("technology")}
    )

    # Site geography: average the members that carry real coordinates; fall back
    # to whatever county centroid is available.
    located = [
        member for member in members
        if member.get("geo_precision") == "site"
        and member.get("latitude") is not None
    ]
    if located:
        latitude = sum(m["latitude"] for m in located) / len(located)
        longitude = sum(m["longitude"] for m in located) / len(located)
        geo_precision = "site"
    else:
        fallback = next(
            (m for m in members if m.get("latitude") is not None), None
        )
        latitude = fallback["latitude"] if fallback else None
        longitude = fallback["longitude"] if fallback else None
        geo_precision = "county" if fallback else "none"

    dates = [
        value
        for member in members
        for value in (member.get("status_date"), member.get("projected_cod"))
        if value
    ]

    return {
        "site_id": site_id,
        "site_name": _pick_display(members, "project_name"),
        "operator": _pick_display(members, "operator"),
        "county": _pick_display(members, "county"),
        "latitude": latitude,
        "longitude": longitude,
        "geo_precision": geo_precision,
        "site_class": site_class,
        "has_generation": int(has_generation),
        "has_load": int(has_load),
        "gen_mw": gen_mw,
        "load_mw": load_mw,
        "gen_mw_approved": gen_mw_approved,
        "gen_mw_pending": gen_mw_pending,
        "load_mw_approved": load_mw_approved,
        "load_mw_pending": load_mw_pending,
        "lifecycle": site_lifecycle,
        "has_pending": int(has_pending),
        "fuels": ",".join(fuels) or None,
        "technologies": ",".join(technologies) or None,
        "programs": ",".join(programs) or None,
        "dispatchable_mw": dispatchable_mw,
        "nox_tpy": nox_tpy,
        "n_members": len(members),
        "sources": ",".join(sorted({member["source"] for member in members})),
        "earliest_date": min(dates) if dates else None,
        "latest_date": max(dates) if dates else None,
        "confidence": round(min(link_scores), 4) if link_scores else 1.0,
        "updated_at": utcnow(),
    }


def rebuild_sites(conn, threshold=DEFAULT_THRESHOLD, verbose=False):
    """Recompute links and sites from scratch. Returns a summary dict.

    Safe to re-run: both derived tables are cleared first, so tuning `threshold`
    and re-running is the intended workflow.
    """
    rows = [dict(row) for row in conn.execute("SELECT * FROM entities")]
    if not rows:
        conn.execute("DELETE FROM site_members")
        conn.execute("DELETE FROM sites")
        conn.execute("DELETE FROM entity_links")
        conn.commit()
        return {"entities": 0, "links": 0, "sites": 0, "colocated": 0}

    links = build_links(rows, threshold=threshold)
    if verbose:
        print(f"  scored candidates -> {len(links)} links at threshold {threshold}")

    conn.execute("DELETE FROM site_members")
    conn.execute("DELETE FROM sites")
    conn.execute("DELETE FROM entity_links")
    conn.executemany(
        "INSERT INTO entity_links(entity_a, entity_b, score, method, distance_km, "
        "name_score, operator_score) VALUES(?, ?, ?, ?, ?, ?, ?)",
        [
            (
                link["entity_a"], link["entity_b"], link["score"], link["method"],
                link["distance_km"], link["name_score"], link["operator_score"],
            )
            for link in links
        ],
    )

    union = UnionFind()
    by_id = {row["entity_id"]: row for row in rows}
    for entity_id in by_id:
        union.add(entity_id)
    for link in links:
        union.union(link["entity_a"], link["entity_b"])

    scores_by_root = defaultdict(list)
    for link in links:
        scores_by_root[union.find(link["entity_a"])].append(link["score"])

    site_rows, member_rows = [], []
    for root, entity_ids in union.groups().items():
        members = [by_id[entity_id] for entity_id in entity_ids]
        site = summarize_site(members, scores_by_root.get(root, []))
        site_rows.append(site)
        member_rows.extend((site["site_id"], entity_id) for entity_id in entity_ids)

    columns = list(site_rows[0].keys())
    conn.executemany(
        f"INSERT INTO sites ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})",
        [[site[column] for column in columns] for site in site_rows],
    )
    conn.executemany(
        "INSERT INTO site_members(site_id, entity_id) VALUES(?, ?)", member_rows
    )
    conn.commit()

    colocated = sum(1 for site in site_rows if site["site_class"] == SITE_COLOCATED)
    return {
        "entities": len(rows),
        "links": len(links),
        "sites": len(site_rows),
        "colocated": colocated,
    }


def select_capacity_records(entities, members, field="capacity_mw",
                            kinds=(classify.KIND_GENERATION,)):
    """Return the entity_ids whose capacity the site totals are actually built from.

    `summarize_site` sums a capacity field within each source and keeps the
    largest source total, so a plant present in both the ERCOT queue and its TCEQ
    air permit is counted once. Charts that aggregate the raw records need the
    same subset, or their totals will exceed the site totals they sit next to.

    entities/members are DataFrames. Returns a set of entity_id.
    """
    if entities.empty:
        return set()

    candidates = entities[entities["project_kind"].isin(kinds)]
    candidates = candidates[candidates[field].notna()]
    if candidates.empty:
        return set()

    joined = candidates.merge(
        members[["site_id", "entity_id"]], on="entity_id", how="left"
    )
    # Records not attached to a site stand alone and are always kept.
    unattached = set(joined[joined["site_id"].isna()]["entity_id"])

    attached = joined[joined["site_id"].notna()]
    if attached.empty:
        return unattached

    totals = attached.groupby(["site_id", "source"], as_index=False)[field].sum()
    winners = totals.sort_values(
        [field, "source"], ascending=[False, True]
    ).drop_duplicates("site_id")[["site_id", "source"]]

    kept = attached.merge(winners, on=["site_id", "source"], how="inner")
    return unattached | set(kept["entity_id"])


def explain_site(conn, site_id):
    """Return the links that hold one site together, for the dashboard's
    'why are these the same site?' panel."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT l.*, "
            "       ea.project_name AS name_a, ea.source AS source_a, "
            "       eb.project_name AS name_b, eb.source AS source_b "
            "FROM entity_links l "
            "JOIN site_members ma ON ma.entity_id = l.entity_a AND ma.site_id = ? "
            "JOIN site_members mb ON mb.entity_id = l.entity_b AND mb.site_id = ? "
            "JOIN entities ea ON ea.entity_id = l.entity_a "
            "JOIN entities eb ON eb.entity_id = l.entity_b "
            "ORDER BY l.score DESC",
            (site_id, site_id),
        )
    ]
