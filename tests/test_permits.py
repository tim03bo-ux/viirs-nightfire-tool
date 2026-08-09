"""
test_permits.py — Unit tests for the TCEQ / ERCOT permit database.

Run with: python -m pytest tests/test_permits.py -v
"""

import os
import tempfile

import pandas as pd
import pytest

from src.permits import classify, db as dbmod, link as linkmod, normalize, pipeline, seed
from src.permits.sources import base, ercot, tceq_air, tceq_stormwater


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

    def test_county_plus_shared_operator_links(self):
        a = self.entity(geo_precision="county")
        b = self.entity(entity_id="e2", geo_precision="county",
                        name_norm="brazos ridge digital campus")
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

    def test_all_four_sources_ingested(self, built):
        _, conn, _ = built
        info = dbmod.stats(conn)
        assert set(info["by_source"]) == set(pipeline.ADAPTERS)
        assert info["entities"] == 44

    def test_one_site_per_development(self, built):
        # The demo has 18 invented developments; anything else means the linker
        # over-merged or failed to join records that belong together.
        _, _, summary = built
        assert summary["sites"] == len(seed.DEVELOPMENTS)

    def test_colocated_sites_detected(self, built):
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        colocated = sites[sites["site_class"] == linkmod.SITE_COLOCATED]
        assert len(colocated) == 4
        names = set(colocated["site_name"])
        assert any("Brazos Ridge" in name for name in names)
        assert any("Panhandle Nexus" in name for name in names)

    def test_colocated_site_joins_all_four_feeds(self, built):
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        brazos = sites[sites["site_name"].str.contains("Brazos Ridge")].iloc[0]
        assert set(brazos["sources"].split(",")) == set(pipeline.ADAPTERS)
        assert brazos["gen_mw"] == 300.0
        assert brazos["load_mw"] == 400.0
        assert brazos["fuels"] == classify.FUEL_GAS

    def test_capacity_not_double_counted_across_sources(self, built):
        # Sabine Point appears in the ERCOT queue at 1,120 MW and again on its
        # TCEQ air permit. The site must report 1,120 MW, not 2,240.
        _, conn, _ = built
        sites = dbmod.load_sites(conn)
        sabine = sites[sites["site_name"].str.contains("Sabine Point")].iloc[0]
        assert sabine["gen_mw"] == 1120.0
        assert sabine["n_members"] == 3

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
