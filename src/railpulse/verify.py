"""Post-build integrity assertions.

    railpulse verify           # exit 0 if the database is sound, 1 otherwise

Constraints catch what they were written to catch. This module checks the
things a CHECK or a FOREIGN KEY *cannot* express: that the counts reconcile
against the source feed, that no analytical view has quietly started returning
nothing, that derived columns actually agree with the raw values they were
derived from, and that the rules in 03_transform.sql did what they claim.

It is the thing you run before believing a number, and the thing CI would run
on every change.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from typing import Callable

from . import config
from .db import connect


@dataclass
class Check:
    """One assertion, its result, and enough context to act on a failure."""

    name: str
    sql: str
    #: Given the first column of the first row, is the database sound?
    predicate: Callable[[object], bool]
    explanation: str
    severity: str = "error"        # 'error' fails the run, 'warning' does not
    value: object = None
    passed: bool = False
    error: str | None = None


def _is_zero(value: object) -> bool:
    return value == 0


def _is_positive(value: object) -> bool:
    return isinstance(value, (int, float)) and value > 0


CHECKS: list[Check] = [
    # -- referential integrity ------------------------------------------
    Check(
        "no foreign key violations",
        "SELECT COUNT(*) FROM pragma_foreign_key_check",
        _is_zero,
        "PRAGMA foreign_key_check found orphan rows. The transform's JOIN "
        "guards should have quarantined these into rejected_row instead.",
    ),
    Check(
        "every platform belongs to a known station",
        "SELECT COUNT(*) FROM platform p "
        " LEFT JOIN station s ON s.station_id = p.station_id "
        " WHERE s.station_id IS NULL",
        _is_zero,
        "A boarding point points at a station that does not exist.",
    ),
    Check(
        "every stop_time resolves to a trip and a platform",
        "SELECT COUNT(*) FROM stop_time st "
        " LEFT JOIN trip t     ON t.trip_id  = st.trip_id "
        " LEFT JOIN platform p ON p.stop_id  = st.stop_id "
        " WHERE t.trip_id IS NULL OR p.stop_id IS NULL",
        _is_zero,
        "The fact table references a dimension row that is not present.",
    ),
    Check(
        "every trip's service exists in the calendar",
        "SELECT COUNT(*) FROM trip t "
        " LEFT JOIN service s ON s.service_id = t.service_id "
        " WHERE s.service_id IS NULL",
        _is_zero,
        "A trip references a service_id absent from calendar.txt.",
    ),

    # -- reconciliation against the source feed --------------------------
    Check(
        "stop_time count reconciles with staging",
        "SELECT (SELECT COUNT(*) FROM stop_time) "
        "     + (SELECT COUNT(*) FROM rejected_row "
        "         WHERE source_table = 'stg_stop_times') "
        "     - 2165519",
        _is_zero,
        "loaded + quarantined must equal the 2 165 519 rows in the feed's "
        "stop_times.txt. A mismatch means rows were lost without being "
        "recorded. NOTE: this number is specific to the feed published on "
        "2026-07-20 and will change when the feed does — update it, or treat a "
        "failure here as 'the feed moved' rather than 'the pipeline broke'.",
        severity="warning",
    ),

    # -- derived columns agree with their sources ------------------------
    Check(
        "departure_hour agrees with departure_time",
        "SELECT COUNT(*) FROM stop_time "
        " WHERE departure_secs IS NOT NULL "
        "   AND departure_hour <> (departure_secs / 3600) % 24",
        _is_zero,
        "A materialised departure_hour disagrees with its own departure_secs. "
        "Every hourly figure in the report would be wrong.",
    ),
    Check(
        "departure_secs agrees with the raw GTFS text",
        "SELECT COUNT(*) FROM stop_time "
        " WHERE departure_time IS NOT NULL "
        "   AND departure_secs <> CAST(substr(departure_time, 1, 2) AS INTEGER) * 3600 "
        "                       + CAST(substr(departure_time, 4, 2) AS INTEGER) * 60 "
        "                       + CAST(substr(departure_time, 7, 2) AS INTEGER)",
        _is_zero,
        "The integer companion column does not match the text it was parsed "
        "from. This is the check that would have caught a positional CSV load.",
    ),
    Check(
        "is_boardable agrees with pickup_type",
        "SELECT COUNT(*) FROM stop_time "
        " WHERE is_boardable <> (CASE WHEN pickup_type = 1 THEN 0 ELSE 1 END)",
        _is_zero,
        "v_departure filters on is_boardable, so a mismatch silently changes "
        "every departure count in the project.",
    ),
    Check(
        "day_of_week agrees with service_date",
        "SELECT COUNT(*) FROM service_date "
        " WHERE day_of_week <> CAST(strftime('%w', service_date) AS INTEGER)",
        _is_zero,
        "Q4's weekly pattern is derived from this column.",
    ),
    Check(
        "no implausible departures survived DQ-03",
        "SELECT COUNT(*) FROM stop_time WHERE departure_secs >= 172800",
        _is_zero,
        "A call 48 h or more into its own service day should have been "
        "quarantined by rule DQ-03.",
    ),

    # -- the views still return data -------------------------------------
    Check("v_departure returns rows",
          "SELECT COUNT(*) FROM v_departure", _is_positive,
          "The canonical departure view is empty; every answer depends on it."),
    Check("v_trip_origin returns one row per trip with a boardable origin",
          "SELECT COUNT(*) FROM v_trip_origin", _is_positive,
          "Q3 depends on this view."),
    Check("v_service_frequency covers every service that operates",
          "SELECT (SELECT COUNT(DISTINCT service_id) FROM service_date "
          "         WHERE exception_type = 1) "
          "     - (SELECT COUNT(*) FROM v_service_frequency)",
          _is_zero,
          "Some operating services are missing from the frequency view, so "
          "Q4's percentages would not sum over the whole fleet."),
    Check("v_trip_amenity covers every trip",
          "SELECT (SELECT COUNT(*) FROM trip) "
          "     - (SELECT COUNT(*) FROM v_trip_amenity)",
          _is_zero,
          "Q5's ratios would be computed over a subset of the fleet."),
    Check("v_trip_origin has exactly one row per trip",
          "SELECT COUNT(*) FROM (SELECT trip_id, COUNT(*) AS n "
          "                        FROM v_trip_origin GROUP BY trip_id "
          "                       HAVING n > 1)",
          _is_zero,
          "The ROW_NUMBER window in v_trip_origin is not de-duplicating, so "
          "Q3 would double-count trips."),

    # -- reference data --------------------------------------------------
    Check("every accessibility code used is defined",
          "SELECT COUNT(*) FROM trip t "
          " LEFT JOIN ref_accessibility a ON a.code = t.bikes_allowed "
          " WHERE a.code IS NULL",
          _is_zero,
          "An unexpected GTFS code slipped through without a label."),
    Check("every route_type used is defined",
          "SELECT COUNT(*) FROM route r "
          " LEFT JOIN ref_route_type rt ON rt.route_type = r.route_type "
          " WHERE rt.route_type IS NULL",
          _is_zero,
          "A route carries a mode code the reference table does not know."),

    # -- provenance -------------------------------------------------------
    Check("the build recorded a successful ingestion run",
          "SELECT COUNT(*) FROM ingestion_run "
          " WHERE source = 'gtfs-static' AND status = 'ok'",
          _is_positive,
          "No successful static ingestion is recorded, so no result can be "
          "dated or attributed."),
    Check("feed_info was loaded",
          "SELECT COUNT(*) FROM feed_info", _is_positive,
          "Without feed_info the report cannot state which timetable it "
          "describes."),
    Check("every quarantined row is linked to an ingestion run",
          "SELECT COUNT(*) FROM rejected_row "
          " WHERE source_table LIKE 'stg%' AND run_id IS NULL",
          _is_zero,
          "A rejected row with no run_id cannot be traced back to the feed "
          "version that produced it, which defeats the point of the quarantine."),

    # -- real-time (informational) ----------------------------------------
    Check("real-time trips resolve against the static timetable",
          "SELECT CASE WHEN (SELECT COUNT(*) FROM rt_trip_update) = 0 THEN 100 "
          "  ELSE CAST(100.0 * (SELECT COUNT(*) FROM rt_trip_update r "
          "                       JOIN trip t ON t.trip_id = r.trip_id) "
          "            / (SELECT COUNT(*) FROM rt_trip_update) AS INTEGER) END",
          lambda v: isinstance(v, (int, float)) and v >= 80,
          "rt_trip_update.trip_id is a soft link by design, so some drift is "
          "expected after an upstream republish. A match rate below 80 % means "
          "the static feed is stale — run `railpulse build`.",
          severity="warning"),
]


def run_checks(conn: sqlite3.Connection) -> list[Check]:
    for check in CHECKS:
        # Reset per run. CHECKS is module-level and reused, so without this a
        # second call in the same interpreter (a test, or a long-lived process)
        # could print an error left over from a previous, unrelated run.
        check.error = None
        check.value = None
        check.passed = False
        try:
            row = conn.execute(check.sql).fetchone()
            check.value = row[0] if row else None
            check.passed = check.predicate(check.value)
        except sqlite3.Error as exc:
            check.error = str(exc)
            check.passed = False
    return CHECKS


def verify(*, quiet: bool = False) -> bool:
    """Run every check. Returns True when no error-severity check failed."""
    if not config.DB_PATH.exists():
        raise SystemExit(f"{config.DB_PATH} does not exist — run `make build`.")

    conn = connect(read_only=True)
    try:
        checks = run_checks(conn)
    finally:
        conn.close()

    errors = [c for c in checks if not c.passed and c.severity == "error"]
    warnings = [c for c in checks if not c.passed and c.severity == "warning"]

    print(f"=== RailPulse verification · {config.DB_PATH.name} ===\n")
    width = max(len(c.name) for c in checks)
    for check in checks:
        if check.passed:
            mark = "✓"
        else:
            mark = "!" if check.severity == "warning" else "✗"
        value = check.error or check.value
        if check.passed and quiet:
            continue
        print(f"  {mark} {check.name:<{width}}  {value}")
        if not check.passed:
            print(f"      {check.explanation}")

    passed = len(checks) - len(errors) - len(warnings)
    print(f"\n{passed}/{len(checks)} checks passed"
          f"{f', {len(warnings)} warning(s)' if warnings else ''}"
          f"{f', {len(errors)} FAILURE(S)' if errors else ''}")
    return not errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="railpulse verify",
        description="Assert the built database is internally consistent.",
    )
    parser.add_argument("--quiet", action="store_true",
                        help="only print checks that did not pass")
    args = parser.parse_args(argv)
    return 0 if verify(quiet=args.quiet) else 1


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
