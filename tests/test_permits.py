"""
test_permits.py — Unit tests for the TCEQ / ERCOT permit database.

Run with: python -m pytest tests/test_permits.py -v
"""

import argparse
import json
import os
import tempfile

import pandas as pd
import pytest

from src.permits import (
    classify, db as dbmod, link as linkmod, normalize, pipeline, seed,
    status as statusmod,
)
from src.permits import net as netmod
import permits_cli
from src.permits.sources import (base, ercot, puct, tceq_air,
                                 tceq_records, tceq_stormwater)


class TestNormalize:
    """Text, geography and unit normalization."""

    def test_company_suffixes_stripped(self):
        assert normalize.norm_company("Redbud Digital Holdings, LLC") == "redbud digital"
        assert normalize.norm_company("REDBUD DIGITAL HOLDINGS L.L.C.") == "redbud digital"

    def test_company_variants_collapse_to_same_key(self):
        variants = [
            "Vantage Data Centers LLC",
            "Vantage Data Centers, L.L.C.",
            "VANTAGE DATA CENTERS INC.",
        ]
        keys = {normalize.norm_company(name) for name in variants}
        assert len(keys) == 1

    def test_roman_numerals_folded(self):
        assert normalize.norm_project("Llano Mesa Solar II") == \
               normalize.norm_project("Llano Mesa Solar 2")

    def test_project_noise_words_dropped(self):
        assert "project" not in normalize.norm_project("Ocotillo Flats Solar Project")

    def test_null_like_values_become_empty(self):
        for value in (None, "", "nan", "N/A", "  "):
            assert normalize.norm_text(value) == ""

    def test_county_suffix_and_multi_county(self):
        assert normalize.norm_county("EL PASO COUNTY") == "el paso"
        assert normalize.norm_county("Ward, Winkler") == "ward"

    def test_haversine_known_distance(self):
        # Austin to Houston is roughly 235 km.
        distance = normalize.haversine_km(30.2672, -97.7431, 29.7604, -95.3698)
        assert 225 < distance < 245

    def test_haversine_missing_coordinate(self):
        assert normalize.haversine_km(30.0, None, 31.0, -97.0) is None

    def test_parse_mw_units(self):
        assert normalize.parse_mw("250 MW") == 250.0
        assert normalize.parse_mw("1,120") == 1120.0
        assert normalize.parse_mw("500 kW") == 0.5
        assert normalize.parse_mw("1.5 GW") == 1500.0
        assert normalize.parse_mw("TBD") is None

    def test_parse_date_formats(self):
        assert normalize.parse_date("2027-06-01") == "2027-06-01"
        assert normalize.parse_date("6/1/2027") == "2027-06-01"
        assert normalize.parse_date("TBD") is None

    def test_county_centroid_lookup(self):
        latitude, longitude = normalize.county_centroid("Pecos County")
        assert latitude is not None and 30 < latitude < 32
        assert longitude is not None and -104 < longitude < -102
        assert normalize.county_centroid("Nowhere") == (None, None)

    def test_name_similarity_bounds(self):
        assert normalize.name_similarity("brazos ridge", "brazos ridge") == 1.0
        assert normalize.name_similarity("", "brazos ridge") == 0.0
        assert 0.0 < normalize.name_similarity("brazos ridge energy",
                                               "brazos ridge digital") < 1.0


class TestClassifyKind:
    """Deciding what a record is."""

    def test_ercot_gis_is_structurally_generation(self):
        kind, confidence, _ = classify.classify_kind(
            name="Anything At All", source="ercot_gis"
        )
        assert kind == classify.KIND_GENERATION
        assert confidence > 0.9

    def test_naics_beats_keywords(self):
        kind, _, evidence = classify.classify_kind(
            name="Riverbend Generating Station", naics="518210"
        )
        assert kind == classify.KIND_DATA_CENTER
        assert "518210" in evidence

    def test_data_center_keyword(self):
        kind, _, _ = classify.classify_kind(name="Deer Hollow Data Center Campus")
        assert kind == classify.KIND_DATA_CENTER

    def test_crypto_wins_over_data_center(self):
        # Crypto sites are routinely described as data centers; the narrower
        # label has to win or every miner is filed as a hyperscaler.
        kind, _, _ = classify.classify_kind(
            name="Bitcoin mining data center", description="digital mining"
        )
        assert kind == classify.KIND_CRYPTO

    def test_generation_keyword(self):
        kind, _, _ = classify.classify_kind(name="Concho Peak Peaking Facility")
        assert kind == classify.KIND_GENERATION

    def test_industrial_load_keyword(self):
        kind, _, _ = classify.classify_kind(name="Bluff Creek Electrolyzer Facility")
        assert kind == classify.KIND_INDUSTRIAL_LOAD

    def test_large_load_fallback_is_low_confidence(self):
        kind, confidence, evidence = classify.classify_kind(
            name="Site 12", source="ercot_large_load", load_mw=300
        )
        assert kind == classify.KIND_DATA_CENTER
        assert confidence <= 0.35
        assert "unconfirmed" in evidence

    def test_unknown_when_no_signal(self):
        kind, confidence, _ = classify.classify_kind(name="Site 12")
        assert kind == classify.KIND_UNKNOWN
        assert confidence < 0.2

    def test_substring_does_not_trigger_keyword(self):
        # 'mining' inside 'determining' must not classify this as crypto.
        kind, _, _ = classify.classify_kind(name="Determining Factors Warehouse")
        assert kind != classify.KIND_CRYPTO


class TestClassifyFuel:
    """Fuel and prime-mover classification."""

    def test_ercot_fuel_codes(self):
        assert classify.classify_fuel(fuel_code="SOL")[0] == classify.FUEL_SOLAR
        assert classify.classify_fuel(fuel_code="WIN")[0] == classify.FUEL_WIND
        assert classify.classify_fuel(fuel_code="GAS")[0] == classify.FUEL_GAS

    def test_battery_refines_other_code(self):
        # ERCOT files storage as fuel OTH with the detail in Technology.
        fuel, tech, _ = classify.classify_fuel(
            fuel_code="OTH", technology="Other - Battery Energy Storage"
        )
        assert fuel == classify.FUEL_STORAGE
        assert tech == "battery"

    def test_combined_cycle_beats_bare_gas(self):
        fuel, tech, _ = classify.classify_fuel(
            technology="Combined-Cycle Gas Turbine with Duct Burner"
        )
        assert fuel == classify.FUEL_GAS
        assert tech == "combined_cycle"

    def test_diesel_from_description(self):
        fuel, _, _ = classify.classify_fuel(
            description="Forty-eight 3.0 MW diesel emergency standby generators"
        )
        assert fuel == classify.FUEL_PETROLEUM

    def test_no_signal_returns_none(self):
        assert classify.classify_fuel(name="Deer Hollow Campus")[0] is None

    def test_dispatchability(self):
        assert classify.is_dispatchable(classify.FUEL_GAS)
        assert classify.is_dispatchable(classify.FUEL_STORAGE)
        assert not classify.is_dispatchable(classify.FUEL_SOLAR)
        assert not classify.is_dispatchable(classify.FUEL_WIND)


class TestColumnResolution:
    """The adapters must survive upstream header churn."""

    def test_exact_and_fuzzy_header_match(self):
        df = pd.DataFrame(columns=["INR", "Project  Name", "Capacity (MW)*", "County"])
        resolved = base.resolve_columns(df, ercot.GIS_COLUMNS, required=["project_name"])
        assert resolved["project_name"] == "Project  Name"
        assert resolved["capacity_mw"] == "Capacity (MW)*"
        assert resolved["county"] == "County"

    def test_missing_required_column_raises(self):
        df = pd.DataFrame(columns=["Something Else"])
        with pytest.raises(KeyError):
            base.resolve_columns(df, ercot.GIS_COLUMNS, required=["project_name"])

    def test_unresolved_optional_field_is_absent(self):
        df = pd.DataFrame(columns=["Project Name"])
        resolved = base.resolve_columns(df, ercot.GIS_COLUMNS)
        assert "project_name" in resolved
        assert "capacity_mw" not in resolved

    def test_header_row_found_below_banner(self):
        df = pd.DataFrame(
            [
                ["ERCOT Monthly GIS Report", None, None],
                ["Generated 2026-05-01", None, None],
                ["INR", "Project Name", "County"],
                ["24INR0101", "Llano Mesa Solar", "Pecos"],
            ]
        )
        assert base.find_header_row(df, ["INR", "Project Name", "County"]) == 2


class TestBuildEntity:
    """Shared normalization applied to every source."""

    def test_county_centroid_fallback_is_flagged(self):
        record = base.build_entity(
            source="ercot_gis", source_key="24INR0101",
            project_name="Llano Mesa Solar", county="Pecos",
        )
        assert record["geo_precision"] == "county"
        assert record["latitude"] is not None

    def test_site_coordinates_win(self):
        record = base.build_entity(
            source="tceq_air", source_key="PROJ-1",
            project_name="Sabine Point", county="Jefferson",
            latitude=29.9, longitude=-94.07,
        )
        assert record["geo_precision"] == "site"
        assert record["latitude"] == pytest.approx(29.9)

    def test_dropped_longitude_sign_repaired(self):
        record = base.build_entity(
            source="tceq_air", source_key="PROJ-2",
            project_name="Test", latitude=31.5, longitude=102.5,
        )
        assert record["longitude"] == pytest.approx(-102.5)

    def test_out_of_state_coordinates_rejected(self):
        record = base.build_entity(
            source="tceq_air", source_key="PROJ-3",
            project_name="Test", county="Nowhere",
            latitude=48.0, longitude=-122.0,
        )
        assert record["geo_precision"] == "none"
        assert record["latitude"] is None

    def test_empty_source_key_rejected(self):
        with pytest.raises(ValueError):
            base.build_entity(source="tceq_air", source_key="  ", project_name="X")


class TestAirCapacityParsing:
    """MW ratings buried in TCEQ permit descriptions."""

    def test_multiplier_expression(self):
        assert tceq_air.capacity_from_text("8 x 37.5 MW turbines") == pytest.approx(300.0)

    def test_plain_rating(self):
        assert tceq_air.capacity_from_text("1,120 MW combined cycle") == pytest.approx(1120.0)

    def test_spelled_out_multiplier(self):
        # "Twenty-two 12 MW engines" is a 264 MW power block, not a 12 MW one.
        assert tceq_air.capacity_from_text(
            "Twenty-two 12 MW natural gas reciprocating engines"
        ) == pytest.approx(264.0)
        assert tceq_air.capacity_from_text(
            "Forty-eight 3.0 MW diesel emergency standby generators"
        ) == pytest.approx(144.0)

    def test_word_to_int(self):
        assert tceq_air.word_to_int("twenty-two") == 22
        assert tceq_air.word_to_int("eight") == 8
        assert tceq_air.word_to_int("banana") is None

    def test_other_units_ignored(self):
        assert tceq_air.capacity_from_text("two 8 MMBtu/hr process heaters") is None

    def test_no_rating(self):
        assert tceq_air.capacity_from_text("concrete batch plant") is None


class TestDatabase:
    """Storage, provenance and idempotency."""

    @pytest.fixture()
    def conn(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        os.unlink(path)
        connection = dbmod.open_db(path)
        yield connection
        connection.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.unlink(path + suffix)

    def test_deterministic_entity_id(self):
        assert dbmod.make_entity_id("ercot_gis", "24INR0101") == \
               dbmod.make_entity_id("ercot_gis", "24INR0101")
        assert dbmod.make_entity_id("ercot_gis", "24INR0101") != \
               dbmod.make_entity_id("tceq_air", "24INR0101")

    def test_upsert_is_idempotent(self, conn):
        record = base.build_entity(
            source="ercot_gis", source_key="24INR0101",
            project_name="Llano Mesa Solar", county="Pecos", capacity_mw=250,
        )
        inserted, updated = dbmod.upsert_entities(conn, [record])
        assert (inserted, updated) == (1, 0)
        inserted, updated = dbmod.upsert_entities(conn, [record])
        assert (inserted, updated) == (0, 1)
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 1

    def test_first_seen_preserved_last_seen_advances(self, conn):
        record = base.build_entity(
            source="ercot_gis", source_key="24INR0101", project_name="Llano Mesa",
        )
        dbmod.upsert_entities(conn, [record], observed_at="2026-01-01T00:00:00+00:00")
        dbmod.upsert_entities(conn, [record], observed_at="2026-06-01T00:00:00+00:00")
        row = conn.execute(
            "SELECT first_seen, last_seen FROM entities"
        ).fetchone()
        assert row["first_seen"].startswith("2026-01-01")
        assert row["last_seen"].startswith("2026-06-01")

    def test_unknown_column_rejected(self, conn):
        with pytest.raises(ValueError):
            dbmod.upsert_entities(conn, [{"entity_id": "x", "source": "s",
                                          "source_key": "k", "bogus": 1}])

    def test_source_file_hash_guard(self, conn):
        file_id = dbmod.record_source_file(conn, "ercot_gis", "/tmp/x.xlsx", "abc123", 10)
        assert file_id > 0
        assert dbmod.already_ingested(conn, "ercot_gis", "abc123")
        assert not dbmod.already_ingested(conn, "tceq_air", "abc123")


class TestScoring:
    """Pairwise match scoring."""

    @staticmethod
    def entity(**overrides):
        record = {
            "entity_id": "e1", "name_norm": "brazos ridge energy center",
            "operator_norm": "brazos ridge development", "county_norm": "milam",
            "latitude": 30.80, "longitude": -96.99, "geo_precision": "site",
            "regulated_entity": None,
        }
        record.update(overrides)
        return record

    def test_shared_regulated_entity_short_circuits(self):
        a = self.entity(regulated_entity="RN112900431", name_norm="a",
                        operator_norm="a", county_norm="x", geo_precision="none",
                        latitude=None, longitude=None)
        b = self.entity(regulated_entity="RN112900431", name_norm="z",
                        operator_norm="z", county_norm="y", geo_precision="none",
                        latitude=None, longitude=None)
        score, method, _, _, _ = linkmod.score_pair(a, b)
        assert method == "tceq_regulated_entity"
        assert score >= 0.95

    def test_close_coordinates_link(self):
        a = self.entity()
        b = self.entity(entity_id="e2", latitude=30.805, longitude=-96.995,
                        name_norm="brazos ridge digital campus")
        score, method, distance, _, _ = linkmod.score_pair(a, b)
        assert method == "geo"
        assert distance < 1.0
        assert score >= linkmod.DEFAULT_THRESHOLD

    def test_identical_names_far_apart_are_rejected(self):
        a = self.entity()
        b = self.entity(entity_id="e2", latitude=32.50, longitude=-96.99)
        score, method, _, _, _ = linkmod.score_pair(a, b)
        assert method == "distance_reject"
        assert score == 0.0

    def test_county_only_needs_text_evidence(self):
        a = self.entity(geo_precision="county")
        b = self.entity(entity_id="e2", geo_precision="county",
                        name_norm="ocotillo flats solar",
                        operator_norm="ocotillo flats solar")
        score, method, _, _, _ = linkmod.score_pair(a, b)
        assert score == 0.0
        assert method == "county_only_weak_text"

    def test_county_plus_shared_operator_is_not_enough(self):
        # A shared county and a shared developer describe most of that
        # developer's portfolio. On the real July 2026 GIS report the old
        # "name OR operator" rule merged 42 unrelated Brazoria County projects
        # into one site, so both signals are now required.
        a = self.entity(geo_precision="county")
        b = self.entity(entity_id="e2", geo_precision="county",
                        name_norm="brazos ridge digital campus")
        score, method, _, _, _ = linkmod.score_pair(a, b)
        assert method == "county_only_weak_text"
        assert score == 0.0

    def test_county_plus_strong_name_and_operator_links(self):
        # "Austin Bayou Solar" / "Austin Bayou Storage I" — a real solar-plus-
        # storage site that must still merge.
        a = self.entity(geo_precision="county", name_norm="austin bayou solar",
                        operator_norm="austin bayou solar")
        b = self.entity(entity_id="e2", geo_precision="county",
                        name_norm="austin bayou storage 1",
                        operator_norm="austin bayou solar")
        score, method, _, _, _ = linkmod.score_pair(a, b)
        assert method == "county_and_name"
        assert score >= linkmod.DEFAULT_THRESHOLD

    def test_different_counties_never_link_without_coordinates(self):
        a = self.entity(geo_precision="county")
        b = self.entity(entity_id="e2", geo_precision="county", county_norm="pecos")
        score, method, _, _, _ = linkmod.score_pair(a, b)
        assert score == 0.0
        assert method == "no_common_geography"


class TestUnionFind:
    def test_transitive_grouping(self):
        union = linkmod.UnionFind()
        union.union("a", "b")
        union.union("b", "c")
        union.add("d")
        groups = {frozenset(members) for members in union.groups().values()}
        assert frozenset({"a", "b", "c"}) in groups
        assert frozenset({"d"}) in groups


class TestLifecycle:
    """Approved vs still-sitting-in-process, across every feed's vocabulary."""

    @pytest.mark.parametrize("text,expected", [
        ("Issued", statusmod.APPROVED),
        ("Registered", statusmod.APPROVED),
        ("Final", statusmod.APPROVED),
        ("Active", statusmod.APPROVED),
        ("IA Signed", statusmod.APPROVED),
        ("Approved for energization", statusmod.APPROVED),
        ("Pending - Technical Review", statusmod.PENDING),
        ("Pending - Public Notice", statusmod.PENDING),
        ("Under Review", statusmod.PENDING),
        ("Notice of Receipt of Application", statusmod.PENDING),
        ("Study in progress", statusmod.PENDING),
        ("Screening", statusmod.PENDING),
        ("Energized", statusmod.OPERATING),
        ("In Service", statusmod.OPERATING),
        ("Withdrawn", statusmod.WITHDRAWN),
        ("Void", statusmod.WITHDRAWN),
        ("Denied", statusmod.DENIED),
        ("Expired", statusmod.EXPIRED),
        ("Terminated", statusmod.EXPIRED),
    ])
    def test_status_text(self, text, expected):
        assert statusmod.classify_lifecycle(status=text)[0] == expected

    def test_pending_beats_approved_in_mixed_text(self):
        # "FIS Approved" is a queue milestone, not an authorization: the project
        # is still waiting on an interconnection agreement.
        assert statusmod.classify_lifecycle(status="FIS Approved")[0] == statusmod.PENDING
        assert statusmod.classify_lifecycle(
            status="Screening Study Complete"
        )[0] == statusmod.PENDING
        assert statusmod.classify_lifecycle(
            status="Security posted"
        )[0] == statusmod.PENDING

    def test_terminal_outcome_beats_progress_words(self):
        assert statusmod.classify_lifecycle(
            status="Withdrawn after technical review"
        )[0] == statusmod.WITHDRAWN

    def test_dates_break_the_tie_when_status_is_unreadable(self):
        assert statusmod.classify_lifecycle(
            status="", decision_date="2025-03-04"
        )[0] == statusmod.APPROVED
        assert statusmod.classify_lifecycle(
            status="", received_date="2026-01-12"
        )[0] == statusmod.PENDING
        assert statusmod.classify_lifecycle(status="")[0] == statusmod.UNKNOWN

    @pytest.mark.parametrize("text,stage", [
        ("Pending - Technical Review", "technical_review"),
        ("Pending - Administrative Review", "administrative_review"),
        ("Pending - Public Notice", "public_notice"),
        ("Contested Case Hearing", "contested_case_hearing"),
        ("IA Signed", "interconnection_agreement"),
        ("FIS Requested", "full_interconnection_study"),
        ("Screening Study Started", "screening_study"),
    ])
    def test_stage_extraction(self, text, stage):
        assert statusmod.classify_lifecycle(status=text)[1] == stage

    def test_bucket_membership(self):
        assert statusmod.PENDING in statusmod.IN_PROCESS
        assert statusmod.APPROVED in statusmod.AUTHORIZED
        assert statusmod.OPERATING in statusmod.AUTHORIZED
        assert not (statusmod.IN_PROCESS & statusmod.AUTHORIZED)

    def test_days_sitting(self):
        from datetime import date

        assert statusmod.days_sitting("2026-01-01", as_of=date(2026, 3, 2)) == 60
        assert statusmod.days_sitting(None) is None
        assert statusmod.days_sitting("not a date") is None


class TestProgram:
    """Which TCEQ air authorization (or other program) a record belongs to."""

    @pytest.mark.parametrize("permit_type,expected", [
        ("New Source Review - Air Quality Permit", statusmod.PROGRAM_NSR),
        ("New Source Review - PSD", statusmod.PROGRAM_PSD),
        ("Nonattainment New Source Review", statusmod.PROGRAM_NNSR),
        ("Permit by Rule - 30 TAC 106.512", statusmod.PROGRAM_PBR),
        ("Air Quality Standard Permit", statusmod.PROGRAM_STANDARD),
        ("Federal Operating Permit - Title V - Renewal", statusmod.PROGRAM_TITLE_V),
        ("De Minimis", statusmod.PROGRAM_DE_MINIMIS),
    ])
    def test_from_permit_type(self, permit_type, expected):
        assert statusmod.classify_program(permit_type=permit_type) == expected

    def test_major_source_beats_plain_nsr(self):
        # PSD and nonattainment are flavours of NSR; the specific label is the
        # useful one, so it must not be swallowed by the generic match.
        assert statusmod.classify_program(
            permit_type="New Source Review - PSD"
        ) == statusmod.PROGRAM_PSD

    def test_from_permit_number_prefix(self):
        assert statusmod.classify_program(
            permit_number="PBR-166051"
        ) == statusmod.PROGRAM_PBR
        assert statusmod.classify_program(
            permit_number="TXR15A4471"
        ) == statusmod.PROGRAM_STORMWATER

    def test_source_default(self):
        assert statusmod.classify_program(
            source="ercot_gis"
        ) == statusmod.PROGRAM_INTERCONNECTION
        assert statusmod.classify_program(
            source="tceq_air"
        ) == statusmod.PROGRAM_OTHER_AIR

    @pytest.mark.parametrize("permit_type,expected", [
        ("New Source Review - Amendment", statusmod.ACTION_AMENDMENT),
        ("Title V - Renewal", statusmod.ACTION_RENEWAL),
        ("Permit Alteration", statusmod.ACTION_ALTERATION),
        ("Change of Location", statusmod.ACTION_CHANGE_OF_LOCATION),
        ("New Source Review - Air Quality Permit", statusmod.ACTION_NEW),
    ])
    def test_action(self, permit_type, expected):
        assert statusmod.classify_action(permit_type=permit_type) == expected


class TestMigration:
    """Existing databases pick up new columns without a rebuild."""

    def test_adds_missing_columns(self, tmp_path):
        db_path = str(tmp_path / "legacy.db")
        conn = dbmod.connect(db_path)
        # A database created before the lifecycle columns existed.
        conn.execute(
            "CREATE TABLE entities (entity_id TEXT PRIMARY KEY, source TEXT, "
            "source_key TEXT, status TEXT)"
        )
        conn.commit()

        added = dbmod.migrate(conn)
        assert "entities.lifecycle" in added
        assert "entities.permit_program" in added
        assert "entities.nox_tpy" in added

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(entities)")}
        assert "decision_date" in columns
        conn.close()

    def test_is_idempotent(self, tmp_path):
        conn = dbmod.open_db(str(tmp_path / "fresh.db"))
        assert dbmod.migrate(conn) == []
        conn.close()

    def test_schema_version_stamped(self, tmp_path):
        conn = dbmod.open_db(str(tmp_path / "fresh.db"))
        value = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert int(value) == dbmod.SCHEMA_VERSION
        conn.close()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Build the demo database once and share it across the end-to-end tests."""
    directory = tmp_path_factory.mktemp("permits")
    db_path = str(directory / "permits.db")
    raw_dir = str(directory / "raw")
    summary = seed.build_demo_db(db_path, raw_dir=raw_dir, verbose=False)
    conn = dbmod.open_db(db_path)
    yield db_path, conn, summary
    conn.close()


class TestEndToEnd:
    """The demo dataset exercises ingest, classification and linking together."""

    def test_every_registered_source_ingested(self, built):
        _, conn, _ = built
        info = dbmod.stats(conn)
        assert set(info["by_source"]) == set(pipeline.ADAPTERS)
        # 49 project records plus the 4 PUCT dockets.
        assert info["entities"] == 53

    def test_sites_resolve_close_to_one_per_development(self, built):
        # 18 invented developments resolve to 19 sites. The extra one is Kiowa
        # Draw, where a wind repower and a crypto mine share only a place name
        # and a county — ERCOT publishes no coordinates, and refusing to assert
        # colocation on that evidence is the correct behaviour, not a regression.
        _, _, summary = built
        # 21 from 18. Cross-source matching closed the splits that came from
        # TCEQ carrying no site name — a plant's ERCOT entry and its TCEQ permit
        # now join on operator and county, which is the only evidence TCEQ has.
        # What remains are the three project-specific PUCT dockets: a CCN
        # docket's applicant is the *utility* building the line, not the
        # developer, so it correctly does not merge into the plant it serves.
        # The fourth docket is the SB 6 rulemaking, held out of site resolution
        # entirely.
        assert summary["sites"] == len(seed.DEVELOPMENTS) + 3

    def test_colocated_sites_detected(self, built):
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        colocated = sites[sites["site_class"] == linkmod.SITE_COLOCATED]
        # Four. Kiowa Draw joined the other three once cross-source matching
        # landed: its wind repower and its mining load share an operator and a
        # county across two feeds, which is the colocation pattern itself rather
        # than a coincidence of place names.
        assert len(colocated) == 4
        names = set(colocated["site_name"])
        assert any("Brazos Ridge" in name for name in names)
        assert any("Panhandle Nexus" in name for name in names)

    def test_colocated_site_joins_multiple_feeds(self, built):
        # The colocated Brazos Ridge site is held together by the TCEQ regulated
        # entity number shared across its air permits and its stormwater NOI,
        # plus the large-load record. The ERCOT generation entry sits apart:
        # TCEQ's real NSR export has no site name to match it on.
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        colocated = sites[sites["site_class"] == linkmod.SITE_COLOCATED]
        brazos = colocated[colocated["site_name"].str.contains("Brazos Ridge")].iloc[0]
        feeds = set(brazos["sources"].split(","))
        assert {"tceq_air", "tceq_swnoi"} <= feeds
        assert brazos["load_mw"] == 400.0
        assert brazos["n_members"] >= 3

    def test_capacity_not_double_counted_across_sources(self, built):
        # Sabine Point appears in the ERCOT queue at 1,120 MW and again on its
        # issued TCEQ air permit, plus a pending amendment for another 45 MW.
        # The site must report 1,165 — the air feed's own total — and never
        # 2,285, which is what adding the feeds together would give.
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        sabine = sites[sites["site_name"].str.contains("Sabine Point")].iloc[0]
        assert sabine["gen_mw"] == 1165.0
        assert sabine["n_members"] == 4

    def test_multiple_queue_entries_at_one_site_are_summed(self, built):
        # Llano Mesa is 250 MW solar plus a 150 MW battery, both in ERCOT.
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        llano = sites[sites["site_name"].str.contains("Llano Mesa")].iloc[0]
        assert llano["gen_mw"] == 400.0
        assert set(llano["fuels"].split(",")) == {
            classify.FUEL_SOLAR, classify.FUEL_STORAGE
        }

    def test_chart_subset_matches_site_totals(self, built):
        # The dashboard aggregates raw records; that total has to equal the sum
        # of the site capacities it is displayed beside.
        _, conn, _ = built
        entities = dbmod.load_entities(conn)
        members = pd.read_sql_query("SELECT * FROM site_members", conn)
        sites = dbmod.load_sites(conn)
        kept = linkmod.select_capacity_records(entities, members)
        charted = entities[entities["entity_id"].isin(kept)]["capacity_mw"].sum()
        assert charted == pytest.approx(sites["gen_mw"].sum())

    def test_stormwater_noi_carries_acreage(self, built):
        _, conn, _ = built
        entities = dbmod.load_entities(conn, sources=["tceq_swnoi"])
        assert entities["acres"].notna().all()
        assert entities["acres"].max() > 1000

    def test_both_approved_and_pending_present(self, built):
        _, conn, _ = built
        info = dbmod.stats(conn)
        assert info["by_lifecycle"][statusmod.APPROVED] > 0
        assert info["by_lifecycle"][statusmod.PENDING] > 0

    def test_all_air_programs_represented(self, built):
        # The demo deliberately spans every TCEQ air authorization family, so a
        # regression in program classification shows up here.
        _, conn, _ = built
        programs = set(dbmod.stats(conn)["by_program"])
        for program in (statusmod.PROGRAM_NSR, statusmod.PROGRAM_PSD,
                        statusmod.PROGRAM_PBR, statusmod.PROGRAM_STANDARD,
                        statusmod.PROGRAM_TITLE_V):
            assert program in programs

    def test_capacity_split_is_a_partition(self, built):
        # approved + pending must never exceed the site total, or the same
        # megawatts are being counted twice.
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        split = sites[["gen_mw_approved", "gen_mw_pending"]].fillna(0).sum(axis=1)
        assert (split <= sites["gen_mw"].fillna(0) + 1e-6).all()

    def test_issued_permit_and_pending_amendment_both_counted(self, built):
        # Sabine Point holds an issued PSD permit plus a pending amendment for
        # an extra 45 MW; both belong in the split.
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        sabine = sites[sites["site_name"].str.contains("Sabine Point")].iloc[0]
        assert sabine["gen_mw_approved"] == 1120.0
        assert sabine["gen_mw_pending"] == 45.0
        assert sabine["has_pending"] == 1

    def test_pending_applications_carry_filing_dates(self, built):
        _, conn, _ = built
        records = dbmod.load_entities(conn, sources=["tceq_air"])
        pending = records[records["lifecycle"] == statusmod.PENDING]
        assert len(pending) > 0
        assert pending["received_date"].notna().all()
        assert pending["decision_date"].isna().all()

    def test_permitted_emissions_ingested(self, built):
        _, conn, _ = built
        records = dbmod.load_entities(conn, sources=["tceq_air"])
        assert records["nox_tpy"].notna().sum() >= 5
        assert records["nox_tpy"].max() > 100

    def test_demo_flag_set(self, built):
        _, conn, _ = built
        assert dbmod.stats(conn)["is_demo"] is True

    def test_relink_is_stable(self, built):
        db_path, conn, summary = built
        again = linkmod.rebuild_sites(conn, verbose=False)
        assert again["sites"] == summary["sites"]
        assert again["colocated"] == summary["colocated"]

    def test_reingest_updates_rather_than_duplicates(self, built):
        db_path, conn, _ = built
        before = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        raw_dir = os.path.join(os.path.dirname(db_path), "raw")
        pipeline.ingest_dir(db_path, raw_dir, force=True, verbose=False)
        after = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        assert after == before


class TestRealGisLayout:
    """Pins the layout of the actual ERCOT GIS workbook.

    Reconstructed from the real July 2026 report: a title, a nine-paragraph
    notes block, a grouping band, and only then the header — on row 30. The
    original 12-row header scan read the notes as column names and resolved
    nothing.
    """

    HEADERS = [
        "INR", "Project Name", "GIM Study Phase", "Interconnecting Entity",
        "POI Location", "County", "CDR Reporting Zone", "Projected COD", "Fuel",
        "Technology", "Capacity (MW)", "IA Signed", "Air Permit",
        "Construction Start", "Construction End", "Approved for Energization",
    ]

    def _workbook(self, tmp_path, header_row=30):
        rows = [[None] * len(self.HEADERS) for _ in range(header_row)]
        rows[6][0] = "GIM Project Details - Large Generators"
        rows[8][0] = "NOTES:"
        rows[9][0] = "Due to Protocol confidentiality provisions, ERCOT ..."
        rows[29][0] = "Project Attributes"
        rows.append(list(self.HEADERS))
        rows.append([None] * len(self.HEADERS))          # spacer row, as in the real file
        rows.append([
            "25INR0102", "Austin Bayou Solar", "SS Completed, FIS Started, No IA",
            "Austin Bayou Solar, LLC", "59903 Bearkat 345kV", "Brazoria", "ERCOT",
            "2027-06-01", "SOL", "PV", 502.48, None, "Not Required", None, None, None,
        ])
        rows.append([
            "23INR0029", "Cedar Bayou 5", "SS Completed, FIS Completed, IA",
            "NRG Texas Power LLC", "Cedar Bayou 345kV", "Chambers", "ERCOT",
            "2028-06-01", "GAS", "CC", 697.0, "2022-05-01", "2021-03-17",
            "2024-01-01", "2027-12-31", "2028-05-01",
        ])
        path = tmp_path / "gis.xlsx"
        pd.DataFrame(rows).to_excel(path, index=False, header=False,
                                    sheet_name="Project Details - Large Gen")
        return str(path)

    def test_header_found_on_row_30(self, tmp_path):
        df = ercot.read_gis(self._workbook(tmp_path))
        assert "INR" in df.columns
        assert "Capacity (MW)" in df.columns
        assert "Air Permit" in df.columns

    def test_records_parse_with_real_codes(self, tmp_path):
        records = ercot.gis_to_entities(ercot.read_gis(self._workbook(tmp_path)))
        by_inr = {r["permit_number"]: r for r in records}
        assert set(by_inr) == {"25INR0102", "23INR0029"}

        solar = by_inr["25INR0102"]
        assert solar["fuel"] == classify.FUEL_SOLAR
        assert solar["technology"] == "photovoltaic"
        assert solar["capacity_mw"] == pytest.approx(502.48)
        # No IA and no energization approval: still an application.
        assert solar["lifecycle"] == statusmod.PENDING
        assert solar["air_permit_status"] == "not_required"

        gas = by_inr["23INR0029"]
        assert gas["fuel"] == classify.FUEL_GAS
        assert gas["technology"] == "combined_cycle"
        assert gas["lifecycle"] == statusmod.APPROVED
        assert gas["air_permit_status"] == "obtained"
        assert gas["air_permit_date"] == "2021-03-17"
        assert gas["construction_start"] == "2024-01-01"

    def test_battery_survives_the_oth_fuel_code(self, tmp_path):
        # Half the real queue is fuel OTH + technology BA. Reading BA as prose
        # loses 877 of 1,797 projects to fuel "other" with no technology.
        fuel, tech, _ = classify.classify_fuel(fuel_code="OTH", technology="BA")
        assert fuel == classify.FUEL_STORAGE
        assert tech == "battery"

    def test_spacer_rows_under_the_header_are_dropped(self, tmp_path):
        records = ercot.gis_to_entities(ercot.read_gis(self._workbook(tmp_path)))
        assert len(records) == 2


class TestSourceInference:
    """Filename-to-source mapping used by `ingest --dir`."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("ercot-gis-report-demo.csv", pipeline.SOURCE_GIS),
            ("Large_Load_Interconnection_Status.xlsx", pipeline.SOURCE_LARGE_LOAD),
            ("tceq-stormwater-noi-2026.csv", pipeline.SOURCE_TCEQ_SWNOI),
            ("TXR150000_export.csv", pipeline.SOURCE_TCEQ_SWNOI),
            ("tceq-air-nsr-pending.xlsx", pipeline.SOURCE_TCEQ_AIR),
            ("puct-interchange-dockets-demo.csv", pipeline.SOURCE_PUCT),
            ("holiday_photos.csv", None),
        ],
    )
    def test_infer_source(self, filename, expected):
        assert pipeline.infer_source(filename) == expected


class TestAdapterParsing:
    """Each adapter against a minimal in-memory frame."""

    def test_gis_rows_become_generation(self):
        df = pd.DataFrame([{
            "INR": "24INR0101", "Project Name": "Llano Mesa Solar",
            "Interconnecting Entity": "Llano Mesa Renewables LLC", "County": "Pecos",
            "Fuel": "SOL", "Technology": "Photovoltaic Solar",
            "Capacity (MW)": 250, "Projected COD": "2027-06-01",
            "GIM Study Phase": "SS Complete",
        }])
        records = ercot.gis_to_entities(df)
        assert len(records) == 1
        assert records[0]["project_kind"] == classify.KIND_GENERATION
        assert records[0]["fuel"] == classify.FUEL_SOLAR
        assert records[0]["capacity_mw"] == 250.0
        assert records[0]["source_key"] == "24INR0101"

    def test_large_load_colocation_flag_recorded(self):
        df = pd.DataFrame([{
            "Request Number": "LIR-1", "Project Name": "Brazos Ridge Digital Campus",
            "Interconnecting Entity": "Brazos Ridge Development LLC", "County": "Milam",
            "Requested Capacity (MW)": 400, "Load Type": "Data center",
            "Co-located": "Yes",
        }])
        records = ercot.large_load_to_entities(df)
        assert records[0]["project_kind"] == classify.KIND_DATA_CENTER
        assert records[0]["load_mw"] == 400.0
        assert "co-located" in records[0]["kind_evidence"].lower()

    def test_air_permit_capacity_from_description(self):
        df = pd.DataFrame([{
            "Project Number": "PROJ-1", "Regulated Entity Name": "Brazos Ridge Energy Center",
            "Customer Name": "Brazos Ridge Development LLC", "County": "Milam",
            "RN Number": "RN112900431",
            "Project Description": "8 x 37.5 MW natural gas simple cycle turbines",
            "NAICS": "221112",
        }])
        records = tceq_air.to_entities(df)
        assert records[0]["capacity_mw"] == pytest.approx(300.0)
        assert records[0]["project_kind"] == classify.KIND_GENERATION
        assert records[0]["regulated_entity"] == "RN112900431"

    def test_stormwater_min_acres_filter(self):
        df = pd.DataFrame([
            {"Permit Number": "TXR1", "Site Name": "Big Campus",
             "Operator Name": "X LLC", "County": "Milam", "Acres Disturbed": 300},
            {"Permit Number": "TXR2", "Site Name": "Small Lot",
             "Operator Name": "Y LLC", "County": "Milam", "Acres Disturbed": 2},
        ])
        assert len(tceq_stormwater.to_entities(df)) == 2
        kept = tceq_stormwater.to_entities(df, min_acres=50)
        assert len(kept) == 1
        assert kept[0]["source_key"] == "TXR1"


class TestPuctParsing:
    """PUCT Interchange: docket search, docket detail, and what a docket is."""

    SEARCH_HTML = """
    <table><tr><th>Control</th><th>Filings</th><th>Utility</th><th>Description</th></tr>
    <tr><td>52455</td><td>31</td><td>ONCOR ELECTRIC DELIVERY CO</td>
        <td>APPLICATION OF ONCOR ELECTRIC DELIVERY COMPANY LLC TO AMEND ITS
            CERTIFICATE OF CONVENIENCE AND NECESSITY FOR THE OLD COUNTRY SWITCH
            345-KV TAP TRANSMISSION LINE IN ELLIS COUNTY</td></tr>
    <tr><td>56903</td><td>57</td><td>EL PASO ELECTRIC COMPANY</td>
        <td>APPLICATION OF EL PASO ELECTRIC COMPANY FOR AN ECONOMIC DEVELOPMENT
            RATE RIDER FOR A NEW DATA CENTER TO BE LOCATED IN EL PASO TEXAS</td></tr>
    </table>"""

    DOCKET_HTML = """
    <table><tr><th>Item</th><th>File Stamp</th><th>Party</th><th>Item Type</th>
                <th>Filing Description</th></tr>
    <tr><td>1</td><td>7/31/2025</td><td>PUC RULES &amp; PROJECTS</td><td>PRJ</td>
        <td>Request for Control Number</td></tr>
    <tr><td>2</td><td>10/10/2025</td><td>EdgeConneX</td><td>PC</td>
        <td>EDGECONNEX RESPONSE TO STAFF&#8217;S QUESTIONS</td></tr>
    <tr><td>3</td><td>4/20/2026</td><td>EdgeConneX</td><td>PC</td>
        <td>Reply comments</td></tr>
    </table>"""

    def test_search_table_parsed(self):
        df = puct.parse_results(self.SEARCH_HTML)
        assert len(df) == 2
        assert list(df.control_number) == ["52455", "56903"]
        # The header row leads with a label, not a docket number, so it drops.
        assert "Control" not in set(df.control_number)

    def test_html_entities_decoded(self):
        df = puct.parse_docket(self.DOCKET_HTML)
        assert df.iloc[0].party == "PUC RULES & PROJECTS"
        assert "'" in df.iloc[1].filing_description

    def test_county_and_voltage_from_case_style(self):
        style = puct.parse_results(self.SEARCH_HTML).iloc[0].case_style
        assert puct.extract_county(style) == "Ellis"
        assert puct.extract_voltage_kv(style) == 345

    def test_county_absent_when_case_style_names_none(self):
        # "IN EL PASO TEXAS" is a city, not a county; inventing one would place
        # a rate proceeding on the map at a location it never claimed.
        style = puct.parse_results(self.SEARCH_HTML).iloc[1].case_style
        assert puct.extract_county(style) is None

    @pytest.mark.parametrize("style,expected", [
        ("APPLICATION FOR A NEW DATA CENTER RATE", "data_center"),
        ("RULEMAKING TO IMPLEMENT LARGE LOAD INTERCONNECTION STANDARDS "
         "UNDER PURA 37.0561", "large_load"),
        ("APPLICATION OF LUMINANT POWER GENERATION LLC", "generation"),
        ("AMEND ITS CERTIFICATE OF CONVENIENCE AND NECESSITY", "transmission"),
        ("PETITION FOR ARBITRATION OF INTERCONNECTION RATES", "other"),
    ])
    def test_docket_relevance(self, style, expected):
        assert puct.classify_docket(style) == expected

    def test_docket_dates_and_parties(self):
        filings = puct.parse_docket(self.DOCKET_HTML)
        assert puct.docket_dates(filings) == ("2025-07-31", "2026-04-20")
        # Commission staff file in every docket; who *came to* it is the signal.
        assert puct.docket_parties(filings) == ["EdgeConneX"]

    def test_utility_type_sent_as_code(self):
        # The form displays "Electric" but posts "E"; sending the label is
        # accepted and silently matches nothing.
        assert puct._utility_code("Electric") == "E"
        assert puct._utility_code("all") == "A"

    def test_transmission_docket_is_not_generation(self):
        df = puct.parse_results(self.SEARCH_HTML)
        records = {r["source_key"]: r for r in puct.to_entities(df)}
        line = records["52455"]
        # A CCN for a tap line is paperwork about a plant, never a plant.
        assert line["project_kind"] == classify.KIND_UNKNOWN
        assert line["county"] == "Ellis"
        assert line["geo_precision"] == "county"
        assert line["capacity_mw"] is None

    def test_data_center_docket_kept_as_load(self):
        df = puct.parse_results(self.SEARCH_HTML)
        records = {r["source_key"]: r for r in puct.to_entities(df)}
        assert records["56903"]["project_kind"] == classify.KIND_DATA_CENTER
        # No county in the case style means no coordinates, not a guess.
        assert records["56903"]["geo_precision"] == "none"

    def test_irrelevant_dockets_dropped_by_default(self):
        df = pd.DataFrame([{
            "control_number": "12345", "filings": "2", "utility": "SOME TELCO",
            "case_style": "PETITION FOR ARBITRATION OF INTERCONNECTION RATES",
        }])
        assert puct.to_entities(df) == []
        assert len(puct.to_entities(df, relevant_only=False)) == 1

    def test_parties_reach_the_description(self):
        df = pd.DataFrame([{
            "control_number": "58481", "filings": "203",
            "utility": "PUC RULES & PROJECTS",
            "case_style": "RULEMAKING TO IMPLEMENT LARGE LOAD INTERCONNECTION "
                          "STANDARDS UNDER PURA 37.0561",
            "parties": "GOOGLE LLC; ROWAN DIGITAL INFRASTRUCTURE LLC",
        }])
        record = puct.to_entities(df)[0]
        # ERCOT publishes the large-load queue in aggregate with no customer
        # named; the docket's party list is where the names actually are, so it
        # has to survive into the stored payload rather than only informing
        # classification and then being dropped.
        assert "GOOGLE LLC" in json.loads(record["raw_json"])["parties"]


class TestProxyDiscovery:
    """The transport must outlive a proxy that moves ports mid-run."""

    def test_refused_connection_triggers_rediscovery(self, monkeypatch):
        calls = []
        monkeypatch.setattr(netmod, "_PROXY", "http://127.0.0.1:9", raising=False)
        monkeypatch.setattr(netmod, "_PROXY_CHECKED", True, raising=False)
        monkeypatch.setattr(netmod, "_discover", lambda: calls.append(1) or "")
        netmod.current_proxy(rediscover=True)
        assert calls, "a refused connection must re-ask which port the proxy is on"

    def test_listening_ports_are_read_from_proc(self):
        # The point of reading /proc rather than the environment: a running
        # process's os.environ was snapshotted at exec and never updates.
        ports = netmod._listening_ports()
        assert isinstance(ports, list)
        assert all(isinstance(p, int) for p in ports)


class TestCrossSourceLinking:
    """One project seen by ERCOT, TCEQ, PUCT and stormwater must resolve to one site."""

    ERCOT = {
        "entity_id": "ercot_gis:1", "source": "ercot_gis",
        "name_norm": "hays energy unit 3 repower", "operator_norm": "hays energy",
        "county_norm": "hays", "latitude": None, "longitude": None,
        "geo_precision": "county",
        "regulated_entity": None,
    }
    TCEQ = {
        "entity_id": "tceq_air:1", "source": "tceq_air",
        # TCEQ's export has no site name: the company goes in both fields.
        "name_norm": "hays energy", "operator_norm": "hays energy",
        "county_norm": "hays", "latitude": None, "longitude": None,
        "geo_precision": "county",
        "regulated_entity": "RN100542831",
    }
    PUCT = {
        "entity_id": "puct:1", "source": "puct",
        "name_norm": "hays energy", "operator_norm": "hays energy",
        "county_norm": "hays", "latitude": None, "longitude": None,
        "geo_precision": "county",
        "regulated_entity": None, "permit_type": "PUCT generation docket",
    }
    NOI = {
        "entity_id": "tceq_swnoi:1", "source": "tceq_swnoi",
        "name_norm": "hays energy expansion", "operator_norm": "hays energy",
        "county_norm": "hays", "latitude": None, "longitude": None,
        "geo_precision": "county",
        "regulated_entity": None,
    }

    def _unambiguous(self, rows):
        return linkmod._unambiguous_operator_counties(rows)

    def test_ercot_and_tceq_join_on_operator_and_county(self):
        rows = [self.ERCOT, self.TCEQ]
        score, method, _, _, _ = linkmod.score_pair(
            self.ERCOT, self.TCEQ, unambiguous=self._unambiguous(rows)
        )
        # The names score too low to clear the same-source rule; the operator
        # and county are all TCEQ can offer, and here they are decisive.
        assert method == "cross_source_operator"
        assert score >= linkmod.DEFAULT_THRESHOLD

    def test_all_four_feeds_resolve_to_one_site(self):
        rows = [self.ERCOT, self.TCEQ, self.PUCT, self.NOI]
        links = linkmod.build_links(rows)
        union = linkmod.UnionFind()
        for row in rows:
            union.add(row["entity_id"])
        for link in links:
            union.union(link["entity_a"], link["entity_b"])
        roots = {union.find(row["entity_id"]) for row in rows}
        assert len(roots) == 1, f"expected one site, got {len(roots)}"

    def test_same_source_portfolio_still_requires_the_name(self):
        # Two unrelated ERCOT projects from one developer in one county. This is
        # the 42-project Brazoria over-merge; operator agreement must not be
        # enough within a feed.
        a = dict(self.ERCOT, entity_id="ercot_gis:a",
                 name_norm="austin bayou solar", operator_norm="big developer",
                 county_norm="brazoria")
        b = dict(self.ERCOT, entity_id="ercot_gis:b",
                 name_norm="bell creek storage", operator_norm="big developer",
                 county_norm="brazoria")
        score, method, _, _, _ = linkmod.score_pair(
            a, b, unambiguous=self._unambiguous([a, b])
        )
        assert score == 0.0
        assert method == "county_only_weak_text"

    def test_ambiguous_operator_county_blocks_the_shortcut(self):
        # One developer, one county, four distinct ERCOT projects: a TCEQ permit
        # from that company could belong to any of them, so operator alone is a
        # coin flip and must not link.
        portfolio = [
            dict(self.ERCOT, entity_id=f"ercot_gis:{i}", name_norm=name,
                 operator_norm="big developer", county_norm="brazoria")
            for i, name in enumerate(
                ["austin bayou", "bell creek", "bodkin", "cascade"]
            )
        ]
        permit = dict(self.TCEQ, entity_id="tceq_air:x",
                      name_norm="big developer", operator_norm="big developer",
                      county_norm="brazoria")
        unambiguous = self._unambiguous(portfolio + [permit])
        score, method, _, _, _ = linkmod.score_pair(
            portfolio[0], permit, unambiguous=unambiguous
        )
        assert score == 0.0, f"linked on an ambiguous operator via {method}"

    def test_operator_with_many_permitted_sites_in_one_county_blocked(self):
        # Pioneer Natural Resources holds 135 TCEQ permits in Upton County, one
        # per well site, all carrying the company name — so counting distinct
        # *names* saw "one project" and let a single ERCOT wind project sharing
        # the operator and county swallow all 135 into one 145-record site.
        # TCEQ's identity is the RN, and by RN this is unmistakably ambiguous.
        wells = [
            dict(self.TCEQ, entity_id=f"tceq_air:{i}", regulated_entity=f"RN{i:09d}",
                 name_norm="pioneer natural resources",
                 operator_norm="pioneer natural resources", county_norm="upton")
            for i in range(20)
        ]
        wind = dict(self.ERCOT, entity_id="ercot_gis:wind",
                    name_norm="giddings wind",
                    operator_norm="pioneer natural resources", county_norm="upton")
        unambiguous = self._unambiguous(wells + [wind])
        score, method, _, _, _ = linkmod.score_pair(
            wind, wells[0], unambiguous=unambiguous
        )
        assert score == 0.0, f"an E&P's well portfolio linked via {method}"

    def test_one_permitted_site_with_many_actions_still_joins(self):
        # The counterpart: five TCEQ permit *actions* on one RN are one site,
        # and must not be mistaken for a portfolio.
        actions = [
            dict(self.TCEQ, entity_id=f"tceq_air:{i}", regulated_entity="RN100542831")
            for i in range(5)
        ]
        unambiguous = self._unambiguous(actions + [self.ERCOT])
        score, method, _, _, _ = linkmod.score_pair(
            self.ERCOT, actions[0], unambiguous=unambiguous
        )
        assert method == "cross_source_operator"
        assert score >= linkmod.DEFAULT_THRESHOLD

    def test_different_counties_never_join(self):
        far = dict(self.TCEQ, county_norm="harris")
        score, _, _, _, _ = linkmod.score_pair(
            self.ERCOT, far, unambiguous=self._unambiguous([self.ERCOT, far])
        )
        assert score == 0.0

    def test_rulemaking_docket_is_not_a_site(self):
        rulemaking = {
            "entity_id": "puct:58481", "source": "puct",
            "permit_type": "PUCT rulemaking / generic proceeding",
        }
        assert not linkmod.is_site_bearing(rulemaking)
        assert linkmod.is_site_bearing(self.PUCT)

    def test_cross_field_name_scoring_finds_the_pairing_that_matters(self):
        # ERCOT's project name against TCEQ's company name is the comparison
        # neither name-to-name nor operator-to-operator makes.
        best = linkmod._best_name_score(
            {"name_norm": "wild horse ranch energy center", "operator_norm": "x"},
            {"name_norm": "y", "operator_norm": "wild horse ranch energy"},
        )
        assert best > 0.8


class TestPuctApplicantExtraction:
    """A docket joins the rest of the record through its applicant."""

    @pytest.mark.parametrize("style,expected", [
        ("APPLICATION OF BRAES BAYOU GENERATING, LLC FOR A CERTIFICATE",
         "BRAES BAYOU GENERATING, LLC"),
        ("PETITION OF VERTUS ENERGY STORAGE LLC TO AMEND", "VERTUS ENERGY STORAGE LLC"),
        ("RULEMAKING TO IMPLEMENT LARGE LOAD STANDARDS", None),
    ])
    def test_applicant(self, style, expected):
        assert puct.extract_applicant(style) == expected

    def test_joint_application_takes_the_lead_filer(self):
        style = ("JOINT APPLICATION OF SHARYLAND UTILITIES, L.P. AND CITY OF "
                 "LUBBOCK FOR SALE")
        assert puct.extract_applicant(style) == "SHARYLAND UTILITIES, L.P"

    def test_named_line_becomes_the_project_name(self):
        style = ("APPLICATION OF ONCOR ELECTRIC DELIVERY COMPANY LLC TO AMEND "
                 "ITS CERTIFICATE OF CONVENIENCE AND NECESSITY FOR THE OLD "
                 "COUNTRY SWITCH 345-KV TAP TRANSMISSION LINE IN ELLIS COUNTY")
        assert "OLD COUNTRY SWITCH" in puct.extract_facility(style)

    def test_applicant_beats_the_utility_column(self):
        df = pd.DataFrame([{
            "control_number": "59852", "filings": "4", "utility": "PUC OPDM",
            "case_style": "APPLICATION OF BRAES BAYOU GENERATING, LLC FOR A "
                          "CERTIFICATE OF CONVENIENCE AND NECESSITY",
        }])
        record = puct.to_entities(df)[0]
        # Indexed under the Commission's docket-management office, but the
        # company that can be matched to ERCOT and TCEQ is in the case style.
        assert record["operator"] == "BRAES BAYOU GENERATING, LLC"

    @pytest.mark.parametrize("utility,style", [
        ("PUC RULES & PROJECTS", "RULEMAKING TO IMPLEMENT SB 6"),
        ("PUC OPDM", "COMPLIANCE DOCKET FOR DOCKET NO. 46936"),
    ])
    def test_generic_proceedings_are_flagged(self, utility, style):
        df = pd.DataFrame([{"control_number": "1", "filings": "1",
                            "utility": utility, "case_style": style}])
        records = puct.to_entities(df, relevant_only=False)
        assert records[0]["permit_type"] == "PUCT rulemaking / generic proceeding"
        assert not linkmod.is_site_bearing(records[0])


class TestStormwaterLiveQuery:
    """The TCEQ water-quality app's rules, pinned so they are not re-learned."""

    GRID_HTML = """
    <table><tr><th>Auth #</th><th>Site Name</th><th>Permittee</th><th>SIC Code</th>
      <th>Segment #</th><th>County</th><th>Region</th><th>City</th>
      <th>Site Location</th></tr>
    <tr><td><a href="index.cfm?fuseaction=home.permit_summary&amp;lgl_id=1">TXR1532PU</a></td>
      <td>MICROSOFT SAT 8990 DATA CENTER PROJECT SITE</td><td>Lemartec Corporation</td>
      <td>7374</td><td>1904</td><td>MEDINA</td><td>13</td><td>SAN ANTONIO</td>
      <td>FM 471 &#x7e;2 MI WEST</td></tr>
    <tr><td>TXR1532PU</td><td>MICROSOFT SAT 8990 DATA CENTER PROJECT SITE</td>
      <td>Lemartec Corporation</td><td>7374</td><td>1905</td><td>MEDINA</td>
      <td>13</td><td>SAN ANTONIO</td><td>FM 471 &#x7e;2 MI WEST</td></tr>
    </table>
    <p>Your search returned 36 records.</p>"""

    DETAIL_HTML = """
    <div>Summary of Authorization TXR150024629
    <b>Permit Number:</b> TXR150024629
    <b>Authorization Status:</b> EXPIRED
    <b>Date Coverage Began:</b> 02/28/2016
    <b>Date Coverage Ended:</b> 06/05/2018
    <b>Site Name on Permit:</b> SN4 EARTHWORK
    <b>Authorization Type:</b> CONSTRUCTION
    <b>Primary SIC Code:</b> 7374
    <b>Area Disturbed (in Acres)</b>: 18.94
    <b>Operator:</b> CN603448994 - ROSENDIN ELECTRIC INC
    <b>RN:</b> RN109142042
    <b>Site Location:</b> 3823 WISEMAN BLVD SAN ANTONIO TX 78251
    <b>County:</b> BEXAR
    <b>Latitude:</b> 29.478888
    <b>Longitude:</b> -98.690555
    Regulated Entity Site Information</div>"""

    def test_grid_parsed_and_segment_duplicates_are_one_site(self):
        df = tceq_stormwater.parse_results(self.GRID_HTML)
        assert len(df) == 2                     # the grid really does repeat
        assert df.iloc[0]["Auth #"] == "TXR1532PU"
        assert df["Auth #"].nunique() == 1      # one authorization, two segments
        assert "~2 MI WEST" in df.iloc[0]["Site Location"]

    def test_result_count_read_from_the_page(self):
        assert tceq_stormwater.result_count(self.GRID_HTML) == 36

    def test_detail_gives_acreage_coordinates_and_the_rn(self):
        record = tceq_stormwater.parse_detail(self.DETAIL_HTML)
        assert record["acres"] == "18.94"
        assert record["latitude"] == "29.478888"
        # Longitude is the last labelled field; without a stop marker for the
        # section heading that follows it swallowed the rest of the page.
        assert record["longitude"] == "-98.690555"
        # The RN is why the detail page is worth a request each: it is the same
        # identifier the air permits carry, so this joins by identity.
        assert record["regulated_entity"] == "RN109142042"
        assert record["customer_number"] == "CN603448994"
        assert record["operator"] == "ROSENDIN ELECTRIC INC"

    def test_both_statuses_at_once_is_refused_before_the_request(self):
        # TCEQ answers this with a generic banner, so failing here is clearer.
        with pytest.raises(ValueError, match="not both"):
            tceq_stormwater._search_payload(permit_status="ALL", app_status="ALL")

    def test_payload_shape_matches_the_form(self):
        pairs = tceq_stormwater._search_payload(sic=["7374"], county="medina")
        names = [name for name, _ in pairs]
        # Eight SIC boxes, read positionally by the server.
        assert names.count("sic_code") == 8
        values = dict(pairs)
        assert values["permit_type"] == tceq_stormwater.PERMIT_TYPE_NOI
        assert values["cnty_name"] == "MEDINA"
        # Image submit: the button's name already contains an '=', and the
        # browser appends .x/.y.
        assert any(n.startswith("_fuseaction=home.validate_search_crit.")
                   for n in names)

    @pytest.mark.parametrize("html,expected", [
        ("<p>Errors were found</p> SIC Code invalid &#x3a; 7370", "SIC Code invalid"),
        ("<p>Errors were found</p> Select either permit OR application status.",
         "Select either permit"),
    ])
    def test_specific_error_extracted_not_the_banner(self, html, expected):
        assert expected in tceq_stormwater._search_error(html)

    def test_detail_without_a_session_is_an_error_not_a_bad_record(self):
        # Detail links embed session-scoped ids; fetched outside their session
        # the app serves the search form, which parse_detail would happily
        # scrape into a record full of form text.
        import unittest.mock as mock
        with mock.patch.object(netmod, "request", return_value="<html>search form</html>"):
            with pytest.raises(ValueError, match="session"):
                tceq_stormwater.fetch_detail("index.cfm?fuseaction=home.permit_summary&x=1")

    def test_entities_carry_real_coordinates(self):
        df = pd.DataFrame([{
            "Auth #": "TXR1532PU", "Site Name": "MICROSOFT SAT 8990 DATA CENTER",
            "Permittee": "Lemartec Corporation", "SIC Code": "7374",
            "County": "MEDINA", "City": "SAN ANTONIO",
            "Site Location": "FM 471", "acres": "84",
            "latitude": "29.35138", "longitude": "-98.93942",
            "regulated_entity": "RN111896825", "status": "ACTIVE",
        }])
        record = tceq_stormwater.to_entities(df)[0]
        # The only feed here that states where a site actually is.
        assert record["geo_precision"] == "site"
        assert record["latitude"] == pytest.approx(29.35138)
        assert record["acres"] == pytest.approx(84.0)
        assert record["regulated_entity"] == "RN111896825"
        assert record["project_kind"] == classify.KIND_DATA_CENTER


class TestUnitRuleAssertsGeneration:
    """TCEQ's own unit-rule export is evidence about what a site is."""

    ROW = {
        "Project Number": "P-1", "Regulated Entity Name": "Nexus Hubbard Power, LLC",
        "Customer Name": "Nexus Hubbard Power, LLC", "County": "Hill",
        "RN Number": "RN112221833", "Permit Type": "STDPMT",
    }

    def test_company_name_alone_classifies_as_unknown(self):
        # "Nexus Hubbard Power, LLC" matches no generation keyword, and a TCEQ
        # record has no other text — its project name *is* the company name.
        record = tceq_air.to_entities(pd.DataFrame([self.ROW]))[0]
        assert record["project_kind"] == classify.KIND_UNKNOWN

    def test_electric_generating_unit_rule_supplies_the_kind(self):
        record = tceq_air.to_entities(
            pd.DataFrame([self.ROW]), unit_rule="electric_generating_facilities"
        )[0]
        assert record["project_kind"] == classify.KIND_GENERATION
        assert "electric_generating_facilities" in record["kind_evidence"]

    def test_a_more_specific_kind_still_wins(self):
        # The same export contains the data center that the generation serves.
        # The fallback must not overwrite it, or the colocation disappears.
        row = dict(self.ROW, **{"Regulated Entity Name": "NEXUS DATA CENTER HUBBARD",
                                "Customer Name": "NEXUS DATA CENTER HUBBARD"})
        record = tceq_air.to_entities(
            pd.DataFrame([row]), unit_rule="electric_generating_facilities"
        )[0]
        assert record["project_kind"] == classify.KIND_DATA_CENTER

    def test_unrelated_unit_rule_asserts_nothing(self):
        record = tceq_air.to_entities(
            pd.DataFrame([self.ROW]), unit_rule="boilers_over_40mmbtu"
        )[0]
        assert record["project_kind"] == classify.KIND_UNKNOWN

    @pytest.mark.parametrize("filename,expected", [
        ("tceq-air-nsr-electric_generating_facilities.csv",
         "electric_generating_facilities"),
        ("tceq-air-nsr-pending-all.csv", None),
    ])
    def test_unit_rule_recovered_from_filename(self, filename, expected):
        assert pipeline.infer_unit_rule(filename) == expected


class TestRnConflictSplitting:
    """A refusal that is pairwise must survive transitive clustering."""

    def _rows(self):
        return {
            "tceq:a": {"entity_id": "tceq:a", "regulated_entity": "RN111"},
            "tceq:b": {"entity_id": "tceq:b", "regulated_entity": "RN222"},
            "ercot:x": {"entity_id": "ercot:x", "regulated_entity": None},
        }

    def test_rn_less_record_cannot_bridge_two_tceq_sites(self):
        # score_pair refuses tceq:a to tceq:b outright, but union-find chains
        # them through the ERCOT row that links to both.
        links = [
            {"entity_a": "ercot:x", "entity_b": "tceq:a", "score": 0.90},
            {"entity_a": "ercot:x", "entity_b": "tceq:b", "score": 0.70},
        ]
        groups = {"root": ["tceq:a", "tceq:b", "ercot:x"]}
        split = linkmod.split_conflicting_rns(groups, self._rows(), links)
        assert len(split) == 2, "two RNs are two sites"
        sizes = sorted(len(members) for members in split.values())
        assert sizes == [1, 2]

    def test_the_bridging_record_goes_to_its_best_match(self):
        links = [
            {"entity_a": "ercot:x", "entity_b": "tceq:a", "score": 0.90},
            {"entity_a": "ercot:x", "entity_b": "tceq:b", "score": 0.70},
        ]
        split = linkmod.split_conflicting_rns(
            {"root": ["tceq:a", "tceq:b", "ercot:x"]}, self._rows(), links
        )
        home = [m for m in split.values() if "ercot:x" in m][0]
        assert "tceq:a" in home, "the ERCOT row belongs with the permit it matched"
        assert "tceq:b" not in home

    def test_one_rn_with_many_permit_actions_is_left_alone(self):
        rows = {f"tceq:{i}": {"entity_id": f"tceq:{i}", "regulated_entity": "RN111"}
                for i in range(4)}
        groups = {"root": list(rows)}
        split = linkmod.split_conflicting_rns(groups, rows, [])
        assert len(split) == 1
        assert len(split["root"]) == 4

    def test_rn_less_only_cluster_is_untouched(self):
        rows = {"ercot:1": {"entity_id": "ercot:1", "regulated_entity": None},
                "ercot:2": {"entity_id": "ercot:2", "regulated_entity": None}}
        split = linkmod.split_conflicting_rns({"root": list(rows)}, rows, [])
        assert len(split) == 1


class TestStormwaterNameSearch:
    """Renewables are reachable by name, not by SIC."""

    @pytest.mark.parametrize("name,term,expected", [
        ("PALO DURO WIND", "WIND", True),
        ("WINDSOR PARK ADDITION", "WIND", False),
        ("STAMPEDE SOLAR BESS AND SUBSTATION", "BESS", True),
        # TCEQ matches substrings, so this really does come back for "BESS".
        ("OBESSO RESIDENCE", "BESS", False),
        ("BROOKE HEIGHTS AKA SOLARIS ESTATES", "SOLAR", False),
        ("OCI - ALAMO 3 SOLAR PV PROJECT", "SOLAR", True),
        ("NEXUS DATA CENTER HUBBARD", "DATA CENTER", True),
        ("MIDLAND DATA CENTRE", "DATA CENTER", False),
    ])
    def test_word_boundary_filter(self, name, term, expected):
        assert tceq_stormwater.matches_term(name, term) is expected

    def test_site_name_reaches_the_payload(self):
        values = dict(tceq_stormwater._search_payload(site_name="SOLAR"))
        # phys_name is the site name; princ_name is the permittee.
        assert values["phys_name"] == "SOLAR"
        assert values["princ_name"] == ""


class TestStormwaterCheckpoint:
    """A sweep of thousands of pages has to survive the container going away."""

    def test_cached_details_are_not_refetched(self, tmp_path, monkeypatch):
        path = tmp_path / "ck.json"
        path.write_text(json.dumps({"TXR1": {"acres": "40", "regulated_entity": "RN1"}}))
        calls = []

        def fake_fetch(url, timeout=90, jar=None):
            calls.append(url)
            return {"acres": "99", "regulated_entity": "RN2"}

        monkeypatch.setattr(tceq_stormwater, "fetch_detail", fake_fetch)
        df = pd.DataFrame([
            {"Auth #": "TXR1", "detail_url": "a"},
            {"Auth #": "TXR2", "detail_url": "b"},
        ])
        out = tceq_stormwater.enrich(df, delay=0, verbose=False, checkpoint=str(path))
        assert len(calls) == 1, "the cached authorization was fetched again"
        assert out.set_index("Auth #").loc["TXR1", "acres"] == "40"
        # And the newly fetched one is persisted for the next run.
        assert "TXR2" in json.loads(path.read_text())

    def test_a_failed_fetch_is_not_cached(self, tmp_path, monkeypatch):
        # A proxy restart or an expired session is transient; caching the
        # failure would make the gap permanent across every future run.
        path = tmp_path / "ck.json"

        def boom(url, timeout=90, jar=None):
            raise ValueError("session expired")

        monkeypatch.setattr(tceq_stormwater, "fetch_detail", boom)
        df = pd.DataFrame([{"Auth #": "TXR9", "detail_url": "a"}])
        out = tceq_stormwater.enrich(df, delay=0, verbose=False, checkpoint=str(path))
        assert "detail_error" in out.columns
        assert json.loads(path.read_text()) == {}


class TestEngineFamily:
    """Recip or turbine — the question the permit data is actually asked."""

    @pytest.mark.parametrize("manufacturer,model,expected", [
        # The model decides, because the maker cannot: GE sells the LM6000
        # aeroderivative and owned Jenbacher gas engines.
        ("general electric", "LM6000", classify.ENGINE_TURBINE),
        ("general electric", "J620", classify.ENGINE_RECIP),
        # Rolls-Royce likewise: aero turbines and Bergen reciprocating sets.
        ("rolls-royce", "GG20V4000", classify.ENGINE_RECIP),
        ("caterpillar", "G3520", classify.ENGINE_RECIP),
        ("cummins", "QSK60G", classify.ENGINE_RECIP),
        ("siemens", "SGT6-8000H", classify.ENGINE_TURBINE),
        ("pratt & whitney", "FT4000", classify.ENGINE_TURBINE),
        # Maker-only fallback, where a model was never captured.
        ("caterpillar", None, classify.ENGINE_RECIP),
        ("capstone", None, classify.ENGINE_TURBINE),
        # Wärtsilä's Texas plants are banks of large gas engines — big enough to
        # read as turbines by capacity, reciprocating in fact.
        ("wartsila", None, classify.ENGINE_RECIP),
        # A maker that builds both, with no model, must not be guessed.
        ("general electric", None, classify.ENGINE_UNKNOWN),
        (None, None, classify.ENGINE_UNKNOWN),
    ])
    def test_engine_family(self, manufacturer, model, expected):
        assert classify.classify_engine(manufacturer, model) == expected

    def test_a_site_can_hold_both_families(self):
        # A peaker with black-start engines beside its turbines is one site with
        # two families; picking a winner would lose that.
        families = classify.classify_engines(
            "caterpillar, siemens", "G3520, SGT6-8000H"
        )
        assert families == {classify.ENGINE_RECIP, classify.ENGINE_TURBINE}

    def test_unknown_makers_drop_out_rather_than_counting(self):
        assert classify.classify_engines("acme widgets", None) == set()

    def test_model_wins_over_maker(self):
        # Maker says turbine, model says otherwise. The model is the fact.
        assert classify.classify_engines("general electric", "J920") == {
            classify.ENGINE_RECIP
        }

    @pytest.mark.parametrize("raw,expected", [
        ("wärtsilä", "wartsila"),
        ("Wartsila", "wartsila"),
        ("GE Vernova", "general electric"),
        ("INNIO", "jenbacher"),
        ("caterpillar", "caterpillar"),
        (None, None),
    ])
    def test_maker_spellings_collapse(self, raw, expected):
        # Two spellings of one company split its sites across two filter
        # entries, which reads as two smaller vendors than reality.
        assert classify.canonical_maker(raw) == expected


class TestScrapeTargetFile:
    """Parsing of --rn-file target lists.

    A tab-separated file was once read with a comma-only split, so every RN came
    out as "RN100209451\tMOTIVA ENTERPRISES LLC", every lookup missed, and the
    run wrote 99 results with zero documents each and exited 0.
    """

    def _write(self, tmp_path, text):
        path = tmp_path / "targets.txt"
        path.write_text(text)
        return str(path)

    def test_comma_separated(self, tmp_path):
        path = self._write(tmp_path, "RN100209451,MOTIVA ENTERPRISES LLC\n")
        targets, malformed = permits_cli.read_rn_file(path)
        assert targets == [("RN100209451", "MOTIVA ENTERPRISES LLC")]
        assert malformed == []

    def test_tab_separated_reads_the_same(self, tmp_path):
        path = self._write(tmp_path, "RN100209451\tMOTIVA ENTERPRISES LLC\n")
        targets, malformed = permits_cli.read_rn_file(path)
        assert targets == [("RN100209451", "MOTIVA ENTERPRISES LLC")]
        assert malformed == []

    def test_rn_only_line_has_no_operator(self, tmp_path):
        path = self._write(tmp_path, "RN100209451\n")
        targets, _ = permits_cli.read_rn_file(path)
        assert targets == [("RN100209451", None)]

    def test_comments_and_blanks_skipped(self, tmp_path):
        path = self._write(
            tmp_path, "# a header\n\nRN100209451,A\n\n# another\nRN100209766,B\n"
        )
        targets, malformed = permits_cli.read_rn_file(path)
        assert [t[0] for t in targets] == ["RN100209451", "RN100209766"]
        assert malformed == []

    def test_non_rn_lines_are_reported_not_scraped(self, tmp_path):
        # These would each cost a session, a search and a wait, all to find
        # nothing. Better to name them than to walk them.
        path = self._write(tmp_path, "RN100209451,A\nnot-an-rn\nRN12345,B\n")
        targets, malformed = permits_cli.read_rn_file(path)
        assert [t[0] for t in targets] == ["RN100209451"]
        assert malformed == ["not-an-rn", "RN12345"]

    def test_shipped_target_lists_parse_clean(self, tmp_path):
        # The two committed lists are the ones people actually run.
        for name in ("scrape_targets_egf.txt", "scrape_targets_casebycase.txt"):
            path = os.path.join("data", name)
            targets, malformed = permits_cli.read_rn_file(path)
            assert not malformed, f"{name}: {malformed[:3]}"
            assert targets, f"{name} is empty"


class TestDocumentPaging:
    """search_all_documents must walk the whole docket, not page one.

    search_documents asks for 50 rows sorted newest-first. A station docket runs
    to hundreds -- 743 for one Houston plant -- so stopping at page one sampled
    the 50 most recent records. The unit tables live in the original
    application, the oldest record, so that page was the least likely to hold
    what the scrape was after.
    """

    def _pages(self, total, page_size=50):
        """A fake search_documents over `total` synthetic documents."""
        calls = []

        def fake(result_count=50, start_row=0, **kw):
            calls.append(start_row)
            rows = [{"doc_id": str(i), "title": "application", "extension": "pdf"}
                    for i in range(start_row, min(start_row + result_count, total))]
            return rows, total
        return fake, calls

    def test_follows_every_page(self, monkeypatch):
        fake, calls = self._pages(743)
        monkeypatch.setattr(tceq_records, "search_documents", fake)
        monkeypatch.setattr(tceq_records, "get_access_id", lambda *a, **k: ("id", "ip"))
        docs, total = tceq_records.search_all_documents(rn_number="RN1")
        assert total == 743
        assert len(docs) == 743
        assert calls == list(range(0, 743, 50))

    def test_single_page_docket_makes_one_call(self, monkeypatch):
        fake, calls = self._pages(12)
        monkeypatch.setattr(tceq_records, "search_documents", fake)
        monkeypatch.setattr(tceq_records, "get_access_id", lambda *a, **k: ("id", "ip"))
        docs, _ = tceq_records.search_all_documents(rn_number="RN1")
        assert len(docs) == 12
        assert calls == [0]

    def test_max_results_stops_early(self, monkeypatch):
        fake, calls = self._pages(743)
        monkeypatch.setattr(tceq_records, "search_documents", fake)
        monkeypatch.setattr(tceq_records, "get_access_id", lambda *a, **k: ("id", "ip"))
        docs, _ = tceq_records.search_all_documents(rn_number="RN1", max_results=120)
        assert len(docs) == 120

    def test_server_ignoring_start_row_does_not_spin(self, monkeypatch):
        # A server that returns page one forever, or a docket that shrank
        # mid-sweep, would loop until the run was killed.
        def stuck(result_count=50, start_row=0, **kw):
            return ([{"doc_id": str(i), "title": "a", "extension": "pdf"}
                     for i in range(50)], 743)
        monkeypatch.setattr(tceq_records, "search_documents", stuck)
        monkeypatch.setattr(tceq_records, "get_access_id", lambda *a, **k: ("id", "ip"))
        docs, _ = tceq_records.search_all_documents(rn_number="RN1")
        assert len(docs) == 50

    def test_empty_result_returns_cleanly(self, monkeypatch):
        monkeypatch.setattr(tceq_records, "search_documents",
                            lambda **kw: ([], 0))
        monkeypatch.setattr(tceq_records, "get_access_id", lambda *a, **k: ("id", "ip"))
        assert tceq_records.search_all_documents(rn_number="RN1") == ([], 0)

    def test_access_grant_taken_once_for_the_whole_sweep(self, monkeypatch):
        # The grant is session-scoped; taking a new one per page invalidates the
        # one in flight.
        fake, _ = self._pages(200)
        grants = []
        monkeypatch.setattr(tceq_records, "search_documents", fake)
        monkeypatch.setattr(tceq_records, "get_access_id",
                            lambda *a, **k: (grants.append(1), ("id", "ip"))[1])
        tceq_records.search_all_documents(rn_number="RN1")
        assert len(grants) == 1


class TestScrapeLimits:
    """The four coverage ceilings resolved from CLI flags."""

    def _args(self, **kw):
        base = {"full": False, "max_docs": 6, "max_pages": 40, "max_mb": 40,
                "any_title": False}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_defaults_stay_cheap(self):
        got = permits_cli.scrape_limits(self._args())
        assert got == {"max_docs": 6, "max_pages": 40,
                       "max_bytes": 40 * 1024 * 1024, "useful_only": True}

    def test_full_lifts_every_ceiling(self):
        got = permits_cli.scrape_limits(self._args(full=True))
        assert got["max_docs"] is None
        assert got["max_pages"] is None
        assert got["max_bytes"] is None

    def test_full_keeps_the_title_filter_unless_asked(self):
        # --full is about depth, not about downloading every scanned letter.
        assert permits_cli.scrape_limits(self._args(full=True))["useful_only"]
        assert not permits_cli.scrape_limits(
            self._args(full=True, any_title=True))["useful_only"]

    @pytest.mark.parametrize("flag,key", [
        ("max_docs", "max_docs"), ("max_pages", "max_pages"),
    ])
    def test_zero_means_unlimited(self, flag, key):
        assert permits_cli.scrape_limits(self._args(**{flag: 0}))[key] is None

    def test_zero_mb_means_no_size_skip(self):
        assert permits_cli.scrape_limits(self._args(max_mb=0))["max_bytes"] is None


class TestDocumentDate:
    """Oldest-first truncation depends on parsing dDocCreatedDate.

    The field is M/D/YYYY, so a plain string sort ranks "1/10/2019" ahead of
    "11/16/2016" and --max-docs would keep documents by leading digit.
    """

    @pytest.mark.parametrize("raw,expected", [
        ("11/16/2016 3:42 PM", (2016, 11, 16)),
        ("1/10/2019 ", (2019, 1, 10)),
        ("6/17/2015", (2015, 6, 17)),
    ])
    def test_parses_month_day_year(self, raw, expected):
        assert tceq_records.doc_date({"created": raw}) == expected

    @pytest.mark.parametrize("raw", [None, "", "not a date"])
    def test_unparseable_sorts_last(self, raw):
        assert tceq_records.doc_date({"created": raw}) == (9999, 12, 31)

    def test_ordering_is_chronological_not_lexical(self):
        docs = [{"created": c} for c in
                ("1/10/2019", "11/16/2016", "6/17/2015", "4/3/2018")]
        order = [d["created"] for d in sorted(docs, key=tceq_records.doc_date)]
        assert order == ["6/17/2015", "11/16/2016", "4/3/2018", "1/10/2019"]


class TestForeignEntityRows:
    """Unit rows must be attributed, not merely found.

    Rockwood Energy Center's "Project File Folder" carries a register listing
    many plants. Reading it in full credited DCP Midstream's Wilcox gas plant
    engines -- and a 1,620 MW figure from a third station -- to Rockwood. The
    old 40-page cap hid this by never reaching those pages.
    """

    def test_line_naming_another_entity_is_dropped(self):
        text = ("NEW DCP MIDSTREAM, LP WILCOX GAS PLANT RN100213487 "
                "Waukesha 1478 hp")
        assert tceq_records.extract_unit_records(
            text, rn_number="RN107573610") == []

    def test_same_line_for_our_own_entity_is_kept(self):
        text = "RN107573610 Siemens SGT6-8000H 300 MW"
        units = tceq_records.extract_unit_records(text, rn_number="RN107573610")
        assert [u["manufacturer"] for u in units] == ["siemens"]

    def test_lines_naming_no_entity_are_kept(self):
        # Most real table rows carry no RN at all; the guard must not eat them.
        text = "Caterpillar G3520C 2.0 MW"
        units = tceq_records.extract_unit_records(text, rn_number="RN107573610")
        assert [u["manufacturer"] for u in units] == ["caterpillar"]

    def test_without_an_rn_nothing_is_filtered(self):
        # Callers that do not know whose docket this is get the old behaviour.
        text = "DCP MIDSTREAM RN100213487 Waukesha 1478 hp"
        assert tceq_records.extract_unit_records(text) != []

    @pytest.mark.parametrize("line,rn,expected", [
        ("RN100213487 and RN107573610", "RN107573610", ["RN100213487"]),
        ("RN107573610 only", "RN107573610", []),
        ("no entity here", "RN107573610", []),
        ("RN100213487", None, []),
    ])
    def test_foreign_rns(self, line, rn, expected):
        assert tceq_records.foreign_rns(line, rn) == expected
