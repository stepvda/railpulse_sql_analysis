"""The cleaning rules in sql/03_transform.sql, exercised against a feed that
breaks every one of them exactly once.

See tests/conftest.py for the fixture and the intent behind each bad row.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# The good rows survive
# ---------------------------------------------------------------------------
def test_stations_and_platforms_are_split_by_grain(conn):
    """stops.txt holds two grains; the transform separates them."""
    assert conn.execute("SELECT COUNT(*) FROM station").fetchone()[0] == 2
    # stops.txt carries 6 location_type=0 rows. Five load; P_ORPHAN, whose
    # parent_station does not exist, is quarantined by DQ-04.
    assert conn.execute("SELECT COUNT(*) FROM platform").fetchone()[0] == 5

    # station_name lives only on `station` — asking `platform` for it must fail.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(platform)")}
    assert "station_name" not in columns
    assert "stop_name" not in columns


def test_platform_code_null_child_exists_per_station(conn):
    """Each station keeps exactly one 'no track allocated' child."""
    rows = conn.execute(
        "SELECT station_id, COUNT(*) AS n FROM platform "
        " WHERE platform_code IS NULL GROUP BY station_id"
    ).fetchall()
    assert {r["station_id"]: r["n"] for r in rows} == {"S_CENTRAL": 1, "S_NORTH": 1}


def test_has_platform_code_flag_matches_platform_code(conn):
    mismatches = conn.execute(
        "SELECT COUNT(*) FROM platform "
        " WHERE has_platform_code <> (CASE WHEN platform_code IS NOT NULL "
        "                             THEN 1 ELSE 0 END)"
    ).fetchone()[0]
    assert mismatches == 0


def test_trips_load_except_the_orphan(conn):
    assert conn.execute("SELECT COUNT(*) FROM trip").fetchone()[0] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM trip WHERE trip_id = 'T_ORPHAN_ROUTE'"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# DQ-06 — date and whitespace normalisation
# ---------------------------------------------------------------------------
def test_feed_info_dates_are_iso_despite_leading_space(conn):
    """The real feed ships ' 20260101'. TRIM then slice, or this breaks."""
    row = conn.execute("SELECT * FROM feed_info").fetchone()
    assert row["feed_start_date"] == "2026-01-01"
    assert row["feed_end_date"] == "2026-01-31"


def test_service_dates_are_iso(conn):
    row = conn.execute(
        "SELECT MIN(service_date) AS lo, MAX(service_date) AS hi FROM service_date"
    ).fetchone()
    assert row["lo"] == "2026-01-03"
    assert row["hi"] == "2026-01-15"


# ---------------------------------------------------------------------------
# DQ-02 — accessibility codes: 0 means "no information", not "no"
# ---------------------------------------------------------------------------
def test_empty_accessibility_becomes_no_information_not_no(conn):
    bus = conn.execute(
        "SELECT bikes_allowed, wheelchair_accessible FROM trip WHERE trip_id = 'T_BUS'"
    ).fetchone()
    assert bus["bikes_allowed"] == 0            # 0 = no information
    assert bus["wheelchair_accessible"] == 0
    assert bus["bikes_allowed"] != 2            # 2 would mean "explicitly no"

    rail = conn.execute(
        "SELECT bikes_allowed FROM trip WHERE trip_id = 'T_MORNING'"
    ).fetchone()
    assert rail["bikes_allowed"] == 1


def test_is_guaranteed_only_true_for_code_one(conn):
    rows = {r["code"]: r["is_guaranteed"]
            for r in conn.execute("SELECT code, is_guaranteed FROM ref_accessibility")}
    assert rows == {0: 0, 1: 1, 2: 0}


# ---------------------------------------------------------------------------
# Derived time columns
# ---------------------------------------------------------------------------
def test_after_midnight_departure_keeps_raw_text_and_wraps_the_hour(conn):
    """GTFS 24:10:00 is 00:10 on the platform clock, one day after service start."""
    row = conn.execute(
        "SELECT departure_time, departure_secs, departure_hour, day_offset "
        "  FROM stop_time WHERE trip_id = 'T_WEEKEND' AND stop_sequence = 2"
    ).fetchone()
    assert row["departure_time"] == "24:10:00"      # raw value preserved
    assert row["departure_secs"] == 24 * 3600 + 600
    assert row["departure_hour"] == 0               # NOT 24
    assert row["day_offset"] == 1


def test_same_day_departure_has_zero_offset(conn):
    row = conn.execute(
        "SELECT departure_hour, day_offset FROM stop_time "
        " WHERE trip_id = 'T_MORNING' AND stop_sequence = 1"
    ).fetchone()
    assert row["departure_hour"] == 7
    assert row["day_offset"] == 0


def test_boardable_flags_follow_pickup_and_dropoff(conn):
    """The bus call is pickup=1 AND drop_off=1: a technical pass-through."""
    row = conn.execute(
        "SELECT is_boardable, is_alightable FROM stop_time "
        " WHERE trip_id = 'T_BUS' AND stop_sequence = 1"
    ).fetchone()
    assert row["is_boardable"] == 0
    assert row["is_alightable"] == 0

    row = conn.execute(
        "SELECT is_boardable, is_alightable FROM stop_time "
        " WHERE trip_id = 'T_MORNING' AND stop_sequence = 1"
    ).fetchone()
    assert row["is_boardable"] == 1                # pickup_type = 0
    assert row["is_alightable"] == 0               # drop_off_type = 1


# ---------------------------------------------------------------------------
# The quarantine — every rule fires exactly once
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rule_code, expected",
    [
        ("DQ-03-IMPLAUSIBLE-DEPARTURE", 1),        # 87:16:00
        ("DQ-04-ORPHAN-PLATFORM", 1),              # P_ORPHAN
        ("DQ-04-ORPHAN-SERVICE-DATE", 1),          # SVC_GHOST
        ("DQ-04-ORPHAN-STOP-TIME-TRIP", 1),        # T_GHOST
        ("DQ-04-ORPHAN-STOP-TIME-PLATFORM", 1),    # P_NOWHERE
        ("DQ-05-DUPLICATE-CALL", 1),               # T_EVENING seq 2 twice
        ("DQ-05-DUPLICATE-SERVICE-DATE", 1),       # SVC_DAILY 20260105 twice
        ("DQ-09-ORPHAN-TRIP", 1),                  # T_ORPHAN_ROUTE
    ],
)
def test_each_rule_quarantines_exactly_what_it_should(conn, rule_code, expected):
    count = conn.execute(
        "SELECT COUNT(*) FROM rejected_row WHERE rule_code = ?", (rule_code,)
    ).fetchone()[0]
    assert count == expected, (
        f"{rule_code} caught {count} rows, expected {expected}. "
        f"Either the fixture or the rule in sql/03_transform.sql has changed."
    )


def test_nothing_is_lost_without_being_recorded(conn):
    """loaded + quarantined must account for every staged stop_time row."""
    staged = conn.execute("SELECT COUNT(*) FROM stg_stop_times").fetchone()[0]
    loaded = conn.execute("SELECT COUNT(*) FROM stop_time").fetchone()[0]
    rejected = conn.execute(
        "SELECT COUNT(*) FROM rejected_row WHERE source_table = 'stg_stop_times'"
    ).fetchone()[0]
    assert loaded + rejected == staged
    assert loaded == 7          # 11 staged - 4 quarantined


def test_quarantined_rows_carry_a_traceable_line_number(conn):
    rows = conn.execute(
        "SELECT src_line_no, payload FROM rejected_row "
        " WHERE rule_code = 'DQ-03-IMPLAUSIBLE-DEPARTURE'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["src_line_no"] is not None
    assert "87:16:00" in rows[0]["payload"]


# ---------------------------------------------------------------------------
# Referential integrity of the finished model
# ---------------------------------------------------------------------------
def test_no_foreign_key_violations(conn):
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_translations_are_value_keyed(conn):
    """record_id is empty in the source, so the PK has to be the value tuple."""
    row = conn.execute(
        "SELECT translation FROM text_translation "
        " WHERE table_name = 'stops' AND field_name = 'stop_name' "
        "   AND field_value = 'Testville-Central' AND language = 'nl'"
    ).fetchone()
    assert row["translation"] == "Testdorp-Centraal"


def test_column_order_is_read_from_the_header_not_assumed(conn):
    """The fixture ships columns alphabetically, like the real SNCB feed.

    A positional loader would put stop_lat into zone_id and this would fail.
    """
    row = conn.execute(
        "SELECT latitude, longitude FROM station WHERE station_id = 'S_CENTRAL'"
    ).fetchone()
    assert row["latitude"] == pytest.approx(50.845)
    assert row["longitude"] == pytest.approx(4.357)
