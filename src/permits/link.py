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

# Records that are proceedings rather than places, held out of site resolution.
# A PUCT rulemaking is statewide policy: docket 58481 has 130 parties and no
# location, and clustering it would drag every party's projects together into
# one meaningless site. A docket about a *named* project is different — an
# application by Braes Bayou Generating LLC names the same legal entity ERCOT
# and TCEQ name — so those stay in, matched on their applicant.
NON_SITE_PERMIT_TYPES = frozenset({"PUCT rulemaking / generic proceeding"})


def is_site_bearing(row):
    """Whether a record describes a place that can be merged with others."""
    return (row["permit_type"] if "permit_type" in row.keys() else None) \
        not in NON_SITE_PERMIT_TYPES

# Scoring knobs. Exposed as module constants so the CLI can override them and
# the effect is visible in one place.
DEFAULT_THRESHOLD = 0.62      # minimum combined score to link two records
SAME_SITE_KM = 1.0            # at or under this, spatial evidence is maximal
MAX_LINK_KM = 5.0             # beyond this, coordinates are evidence *against*
RN_MATCH_SCORE = 0.98         # shared TCEQ regulated-entity number
# Tuned against the real July 2026 GIS report (1,827 records). ERCOT gives no
# coordinates, only a county, so every ERCOT-to-ERCOT pair falls to this rule —
# and a county like Brazoria holds dozens of unrelated projects. The original
# "name OR operator" gate merged 42 distinct Brazoria developments (Austin Bayou,
# Bell Creek, Bodkin, Cascade, Clutch City...) into a single site, because one
# big developer's name matching was enough and union-find then chained the rest.
# Requiring BOTH a strong name and a strong operator caps the largest cluster at
# 8 and leaves only legitimate phased campuses merged: Watermelon 1-8 Energy
# Storage, Charro Creek Solar 1-3 with its three colocated storage units,
# Kickstart Energy Storage I-VI. 0.75 rather than 0.85 because
# "Austin Bayou Solar" / "Austin Bayou Storage I" scores 0.80 and is a genuine
# solar-plus-storage site that 0.85 would wrongly split.
MIN_NAME_FOR_COUNTY_MATCH = 0.75
MIN_OPERATOR_FOR_COUNTY_MATCH = 0.75

# The rule above is right within one feed and wrong across feeds, because the
# two failure modes are not the same shape.
#
# Within ERCOT, one developer really does hold dozens of separate projects in a
# county, so a shared operator says almost nothing and the project name has to
# do the work. Across feeds the opposite holds — and worse, the name evidence
# the rule demands does not exist. A TCEQ air permit carries no site name at
# all: the export's "Regulated Entity Name" is the company, so name_norm and
# operator_norm are the same string by construction. Hays Energy is the case in
# point — ERCOT calls it "Hays Energy Unit 3 Repower" operated by HAYS ENERGY,
# LLC; TCEQ calls it "Hays Energy, LLC" in both fields, in Hays County. Same
# plant, obviously, but the names score ~0.6 and the pair was rejected. Across
# 1,827 ERCOT projects and 41,829 TCEQ permits the strict rule found 17 joins.
#
# So a cross-source pair may match on operator and county alone, at a higher
# operator bar — but only where that operator+county is *unambiguous*, meaning
# neither feed shows several distinct projects there. Where a developer does
# hold a portfolio in one county, a permit could belong to any of them and the
# name has to decide, exactly as before.
CROSS_SOURCE_OPERATOR_MATCH = 0.88
# Not a fitted number. Of the 46 operator+county keys where ERCOT and TCEQ both
# hold records, 44 have three or fewer distinct TCEQ sites and 42 have exactly
# one — then a gap, and the remaining two sit at 21 and 45, which are oil and gas
# well portfolios rather than power projects. Any threshold from 3 to 20 selects
# the same set, so this picks the conservative end of a wide plateau.
MAX_NAMES_FOR_OPERATOR_ALONE = 3

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


def _best_name_score(a, b):
    """Strongest name agreement across both records' name and operator fields.

    Feeds disagree about which field holds a project's identity. ERCOT puts it
    in the project name and the company in the operator; TCEQ has only the
    company and writes it into both. Comparing name-to-name and
    operator-to-operator alone therefore misses the pairing that actually
    identifies the site — ERCOT's project name against TCEQ's company — so all
    four combinations are tried and the best is taken.
    """
    name_a, name_b = a.get("name_norm"), b.get("name_norm")
    operator_a, operator_b = a.get("operator_norm"), b.get("operator_norm")
    return max(
        name_similarity(name_a, name_b),
        name_similarity(name_a, operator_b),
        name_similarity(operator_a, name_b),
        name_similarity(operator_a, operator_b),
    )


def score_pair(a, b, unambiguous=None):
    """Score two entity dicts. Returns (score, method, distance_km, name, operator).

    `unambiguous` is the set of (operator_norm, county_norm) keys where no feed
    shows more than `MAX_NAMES_FOR_OPERATOR_ALONE` distinct projects, built once
    by `build_links`. Only those keys may match on operator and county alone.
    """
    name_score = name_similarity(a.get("name_norm"), b.get("name_norm"))
    operator_score = name_similarity(a.get("operator_norm"), b.get("operator_norm"))

    # A shared TCEQ regulated-entity number is an identity statement — and so is
    # a differing one. RN is TCEQ's per-site identifier, so two records carrying
    # different RNs are different sites no matter how alike they look. Without
    # this, every permit a company holds in one county collapsed together (the
    # real data produced a 167-record cluster), because a TCEQ record's project
    # name *is* its company name, making name and operator identical by
    # construction.
    rn_a, rn_b = a.get("regulated_entity"), b.get("regulated_entity")
    if rn_a and rn_b:
        if str(rn_a).strip() == str(rn_b).strip():
            return (RN_MATCH_SCORE, "tceq_regulated_entity", None,
                    name_score, operator_score)
        return (0.0, "different_regulated_entity", None, name_score, operator_score)

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

    # Across feeds, a strong operator in an unambiguous county is enough on its
    # own — it is the only evidence a TCEQ permit can offer, and where that
    # operator has just one project in the county there is nothing else it could
    # refer to. Within a feed this stays closed: that is where a portfolio of
    # unrelated projects shares an operator and a county.
    if (
        a.get("source") != b.get("source")
        and operator_score >= CROSS_SOURCE_OPERATOR_MATCH
        and unambiguous is not None
        and (a.get("operator_norm"), county_a) in unambiguous
    ):
        best_name = _best_name_score(a, b)
        score = 0.45 + 0.35 * operator_score + 0.20 * best_name
        return (score, "cross_source_operator", None, best_name, operator_score)

    # BOTH must hold. Either alone is worthless here: a shared county plus a
    # shared developer describes most of that developer's portfolio, and a
    # shared name plus a different developer is usually a reused place name.
    if (
        name_score < MIN_NAME_FOR_COUNTY_MATCH
        or operator_score < MIN_OPERATOR_FOR_COUNTY_MATCH
    ):
        return (0.0, "county_only_weak_text", None, name_score, operator_score)

    # The project name carries site identity; the operator only corroborates.
    score = 0.30 + 0.55 * name_score + 0.15 * operator_score
    return (score, "county_and_name", None, name_score, operator_score)


def _site_identity(entity):
    """What distinguishes one of a feed's sites from another within that feed.

    TCEQ's identifier is the regulated-entity number, and it has to be used:
    counting distinct *names* cannot work there, because a TCEQ record's name is
    its company name, so every permit one company holds looks like one name no
    matter how many separate sites it covers. Pioneer Natural Resources holds
    135 permits in Upton County, one per well site, under a single name — read
    by name that is "one project", and a single ERCOT wind project sharing the
    operator and the county swallowed all 135 into one 145-record site. Read by
    RN it is 135 sites, which is the truth.
    """
    rn = entity.get("regulated_entity")
    if rn:
        return f"rn:{str(rn).strip()}"
    return f"name:{entity.get('name_norm') or ''}"


def _unambiguous_operator_counties(entities):
    """(operator, county) keys where no feed shows several distinct projects.

    A developer with one project in a county can be matched on operator alone.
    A developer with eight cannot: a permit from that company could belong to
    any of them, and picking one would be a coin flip dressed as a join.
    """
    identities = defaultdict(lambda: defaultdict(set))
    for entity in entities:
        operator = entity.get("operator_norm")
        county = entity.get("county_norm")
        if not operator or not county:
            continue
        identities[(operator, county)][entity.get("source")].add(
            _site_identity(entity)
        )
    return {
        key
        for key, by_source in identities.items()
        if all(
            len(distinct) <= MAX_NAMES_FOR_OPERATOR_ALONE
            for distinct in by_source.values()
        )
    }


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
    unambiguous = _unambiguous_operator_counties(entities)
    links = []
    for left, right in _candidate_pairs(entities):
        a, b = entities[left], entities[right]
        score, method, distance, name_score, operator_score = score_pair(
            a, b, unambiguous=unambiguous
        )
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


def split_conflicting_rns(groups, by_id, links):
    """Break apart clusters that ended up holding more than one TCEQ site.

    `score_pair` refuses any pair carrying different regulated-entity numbers —
    RN is TCEQ's per-site identifier, so two records with different ones are
    different sites. But that refusal is pairwise and clustering is transitive:
    a record with *no* RN, which is every ERCOT and PUCT row, can link to two
    TCEQ records that were explicitly refused each other and chain them anyway.
    Air Products in Galveston merged that way, and so did Enchanted Rock's
    distributed-generation fleet — five separate sites in Montgomery County
    bridged into one by a shared operator name.

    So the rule is re-applied after the fact. Each RN present becomes its own
    site, and an RN-less record joins whichever of them it scored highest
    against, keeping the ERCOT queue entry with the permit it actually matched
    rather than with all of them at once.
    """
    best = defaultdict(dict)   # entity -> {other entity: score}
    for link in links:
        a, b, score = link["entity_a"], link["entity_b"], link["score"]
        best[a][b] = max(best[a].get(b, 0.0), score)
        best[b][a] = max(best[b].get(a, 0.0), score)

    out = {}
    for root, entity_ids in groups.items():
        rns = {
            str(by_id[entity_id].get("regulated_entity")).strip()
            for entity_id in entity_ids
            if by_id[entity_id].get("regulated_entity")
        }
        if len(rns) <= 1:
            out[root] = entity_ids
            continue

        by_rn = defaultdict(list)
        unassigned = []
        for entity_id in entity_ids:
            rn = by_id[entity_id].get("regulated_entity")
            if rn:
                by_rn[str(rn).strip()].append(entity_id)
            else:
                unassigned.append(entity_id)

        for entity_id in unassigned:
            scores = best.get(entity_id, {})
            ranked = sorted(
                by_rn,
                key=lambda rn: max(
                    (scores.get(other, 0.0) for other in by_rn[rn]), default=0.0
                ),
                reverse=True,
            )
            top = ranked[0] if ranked else None
            if top is not None and max(
                (scores.get(other, 0.0) for other in by_rn[top]), default=0.0
            ) > 0:
                by_rn[top].append(entity_id)
            else:
                # Linked only to other RN-less records; it is its own site.
                out[f"{root}:free:{entity_id}"] = [entity_id]

        for rn, members in by_rn.items():
            out[f"{root}:{rn}"] = members
    return out


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
    rows = [
        dict(row) for row in conn.execute("SELECT * FROM entities")
        if is_site_bearing(row)
    ]
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

    clusters = split_conflicting_rns(union.groups(), by_id, links)

    site_rows, member_rows = [], []
    for root, entity_ids in clusters.items():
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
