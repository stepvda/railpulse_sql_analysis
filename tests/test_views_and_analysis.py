"""The semantic layer (sql/05_views.sql) and the analysis suite.

The views encode the project's definitions — what a departure is, what a
morning trip is, how "5 days a week" is measured. These tests pin those
definitions down against the fixture feed, where the right answer is known by
construction rather than by running the query and believing it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# v_departure — the canonical event
# ---------------------------------------------------------------------------
def test_v_departure_excludes_pass_throughs(conn):
    """The bus call has pickup_type = 1, so it is not a departure."""
    trips = {r["trip_id"] for r in conn.execute("SELECT trip_id FROM v_departure")}
    assert "T_BUS" not in trips

    # It is still in the fact table — excluded by definition, not by deletion.
    assert conn.execute(
        "SELECT COUNT(*) FROM stop_time WHERE trip_id = 'T_BUS'"
    ).fetchone()[0] == 1


def test_v_departure_counts_only_boardable_calls(conn):
    """3 of the 7 loaded calls are drop-off-only terminals."""
    assert conn.execute("SELECT COUNT(*) FROM v_departure").fetchone()[0] == 3


def test_v_departure_resolves_station_and_route(conn):
    row = conn.execute(
        "SELECT station_name, platform_code, route_short_name, route_type_label "
        "  FROM v_departure WHERE trip_id = 'T_MORNING'"
    ).fetchone()
    assert row["station_name"] == "Testville-Central"
    assert row["platform_code"] == "1"
    assert row["route_short_name"] == "IC"
    assert row["route_type_label"] == "Rail"


# ---------------------------------------------------------------------------
# v_trip_origin — one row per trip, the first boardable call
# ---------------------------------------------------------------------------
def test_v_trip_origin_has_one_row_per_trip(conn):
    duplicates = conn.execute(
        "SELECT COUNT(*) FROM (SELECT trip_id FROM v_trip_origin "
        "                       GROUP BY trip_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert duplicates == 0


def test_v_trip_origin_picks_the_lowest_stop_sequence(conn):
    row = conn.execute(
        "SELECT stop_sequence, departure_time, station_name "
        "  FROM v_trip_origin WHERE trip_id = 'T_MORNING'"
    ).fetchone()
    assert row["stop_sequence"] == 1
    assert row["departure_time"] == "07:00:00"
    assert row["station_name"] == "Testville-Central"


def test_morning_filter_selects_the_right_trips(conn):
    """Q3's filter: origin before 12:00 on the same service day."""
    trips = {r["trip_id"] for r in conn.execute(
        "SELECT trip_id FROM v_trip_origin "
        " WHERE departure_secs < 12 * 3600 AND day_offset = 0"
    )}
    assert trips == {"T_MORNING"}          # T_EVENING is 18:00, T_WEEKEND 23:40


# ---------------------------------------------------------------------------
# v_trip_service_days — the annualisation weight
# ---------------------------------------------------------------------------
def test_operating_days_counts_calendar_dates(conn):
    rows = {r["trip_id"]: r["operating_days"]
            for r in conn.execute("SELECT * FROM v_trip_service_days")}
    assert rows["T_MORNING"] == 5      # SVC_DAILY, Mon-Fri
    assert rows["T_EVENING"] == 5
    assert rows["T_WEEKEND"] == 2      # SVC_WEEKEND, Sat + Sun
    assert rows["T_BUS"] == 1          # SVC_ONEOFF


def test_duplicate_calendar_date_is_not_double_counted(conn):
    """SVC_DAILY lists 2026-01-05 twice; it must still count as five days."""
    row = conn.execute(
        "SELECT operating_days FROM v_trip_service_days WHERE trip_id = 'T_MORNING'"
    ).fetchone()
    assert row["operating_days"] == 5


# ---------------------------------------------------------------------------
# v_service_frequency — the derived weekly rhythm (DQ-01)
# ---------------------------------------------------------------------------
def test_frequency_classification_matches_the_brief(conn):
    rows = {r["service_id"]: r for r in
            conn.execute("SELECT * FROM v_service_frequency")}

    assert rows["SVC_DAILY"]["typical_days_per_week"] == 5
    assert rows["SVC_DAILY"]["frequency_class"] == "High Frequency"

    assert rows["SVC_WEEKEND"]["typical_days_per_week"] == 2
    assert rows["SVC_WEEKEND"]["frequency_class"] == "Medium Frequency"

    assert rows["SVC_ONEOFF"]["typical_days_per_week"] == 1
    assert rows["SVC_ONEOFF"]["frequency_class"] == "Low Frequency/Special"


def test_weekend_service_is_not_split_across_a_week_boundary(conn):
    """Sat 03 + Sun 04 Jan 2026 fall in the SAME Monday-to-Sunday week.

    This is why weeks are cut from a fixed Monday epoch instead of with
    strftime('%W'), which would put Saturday in week 00 and Sunday in week 01
    and score the service 1 day/week rather than 2.
    """
    row = conn.execute(
        "SELECT typical_days_per_week, active_weeks FROM v_service_frequency "
        " WHERE service_id = 'SVC_WEEKEND'"
    ).fetchone()
    assert row["active_weeks"] == 1
    assert row["typical_days_per_week"] == 2


def test_calendar_txt_carries_no_usable_pattern(conn):
    """DQ-01, asserted: the GTFS weekday flags are all zero."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, SUM(has_weekday_pattern) AS usable FROM service"
    ).fetchone()
    assert row["n"] == 3
    assert row["usable"] == 0


# ---------------------------------------------------------------------------
# v_trip_amenity — Q5's definitions
# ---------------------------------------------------------------------------
def test_amenity_view_separates_unknown_from_no(conn):
    rows = {r["trip_id"]: r for r in
            conn.execute("SELECT * FROM v_trip_amenity")}

    rail = rows["T_MORNING"]
    assert rail["guarantees_bikes"] == 1
    assert rail["bikes_is_unknown"] == 0
    assert rail["guarantees_wheelchair"] == 0     # unpopulated, not refused
    assert rail["wheelchair_is_unknown"] == 1

    bus = rows["T_BUS"]
    assert bus["guarantees_bikes"] == 0
    assert bus["bikes_is_unknown"] == 1
    assert bus["guarantees_any_amenity"] == 0


def test_amenity_view_covers_every_trip(conn):
    trips = conn.execute("SELECT COUNT(*) FROM trip").fetchone()[0]
    covered = conn.execute("SELECT COUNT(*) FROM v_trip_amenity").fetchone()[0]
    assert covered == trips


# ---------------------------------------------------------------------------
# The analysis suite itself
# ---------------------------------------------------------------------------
def _analysis_files() -> list[Path]:
    return sorted((REPO_ROOT / "sql" / "analysis").glob("q*.sql"))


def test_analysis_files_exist():
    files = _analysis_files()
    assert len(files) >= 5, "the five graded questions must each have a file"


@pytest.mark.parametrize("path", _analysis_files(), ids=lambda p: p.name)
def test_every_analysis_query_parses_and_runs(conn, path):
    """Prepare and execute every query against the fixture database.

    The fixture is tiny, so this is fast, and it catches a syntax error or a
    renamed column the moment it is introduced rather than 90 seconds into a
    run against the real feed.
    """
    from railpulse.analyse import parse_analysis_file

    queries = list(parse_analysis_file(path))
    assert queries, f"{path.name} yielded no queries"

    for query in queries:
        try:
            conn.execute(query.sql).fetchall()
        except Exception as exc:                       # noqa: BLE001
            pytest.fail(f"{path.name} :: {query.label} failed: {exc}")


@pytest.mark.parametrize("path", _analysis_files(), ids=lambda p: p.name)
def test_every_query_is_labelled(path):
    """A query without @label lands in the report under a generated name,
    which is legal but always a mistake — labels are how results are cited."""
    from railpulse.analyse import parse_analysis_file

    for query in parse_analysis_file(path):
        assert not query.label_generated, (
            f"{path.name} has an unlabelled query "
            f"(fell back to {query.label!r}); add a '-- @label:' comment"
        )


def test_labels_are_unique_across_the_suite():
    """Labels name output/*.csv files, so a collision silently overwrites one."""
    from railpulse.analyse import parse_analysis_file

    seen: dict[str, str] = {}
    for path in _analysis_files():
        for query in parse_analysis_file(path):
            assert query.label not in seen, (
                f"label {query.label!r} is used by both {seen[query.label]} "
                f"and {path.name}"
            )
            seen[query.label] = path.name
