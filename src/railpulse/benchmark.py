"""Measured evidence that the indexes and the derived columns actually pay.

    python -m railpulse.benchmark              # read-only SARGability pairs
    python -m railpulse.benchmark --with-index-drops

The nice-to-have asks us to "prove you can speed up lookups". A query plan is
an argument; an elapsed time is proof. This module produces both.

Two suites:

*SARGability pairs* (read-only, always safe). Each pair computes the SAME
answer two ways — once against a column that was materialised at load time, and
once by wrapping a column in a function so no index can be used. The gap is the
cost of a SARGable violation, measured rather than asserted.

*Index drop/restore* (``--with-index-drops``, writes to the database). Times a
query, DROPs the index it depends on, times it again, then restores every index
by re-running ``sql/04_indexes.sql`` — which is idempotent, so the database
ends exactly as it started. Guarded behind a flag because it briefly leaves the
database slower, and it is pointless to run against a database another process
is querying.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from dataclasses import dataclass, field

from . import config
from .db import connect, run_sql_file


@dataclass
class Case:
    """One measurable query."""

    label: str
    sql: str
    #: index this case depends on, for the drop/restore suite
    index: str | None = None


@dataclass
class Pair:
    """A SARGable query and the equivalent violation."""

    name: str
    good: Case
    bad: Case
    note: str = ""


SARGABILITY_PAIRS: list[Pair] = [
    Pair(
        name="Q1 hourly histogram (1.58 M boardable calls)",
        good=Case(
            "materialised departure_hour",
            "SELECT departure_hour, COUNT(*) FROM stop_time "
            " WHERE is_boardable = 1 GROUP BY departure_hour",
            index="ix_stop_time_boardable_hour",
        ),
        bad=Case(
            "strftime() evaluated per row",
            "SELECT CAST(strftime('%H', departure_time) AS INTEGER) AS h, "
            "       COUNT(*) FROM stop_time "
            " WHERE is_boardable = 1 GROUP BY h",
        ),
        note="The index can no longer supply the grouping order, so SQLite "
             "falls back to a temporary B-tree over every row.",
    ),
    Pair(
        name="Q2 single-platform lookup",
        good=Case(
            "equality on the indexed stop_id",
            "SELECT COUNT(*) FROM stop_time "
            " WHERE stop_id = 'gs:nmbssncb:8813003_4' AND is_boardable = 1",
            index="ix_stop_time_stop_boardable",
        ),
        bad=Case(
            "substr() on the indexed stop_id",
            "SELECT COUNT(*) FROM stop_time "
            " WHERE substr(stop_id, 1, 21) = 'gs:nmbssncb:8813003_4' "
            "   AND is_boardable = 1",
        ),
        note="SEARCH becomes SCAN: the engine must decode and test every "
             "index entry instead of seeking to one contiguous slice.",
    ),
    Pair(
        name="Q4 weekday counts (4.70 M service days)",
        good=Case(
            "materialised day_of_week",
            "SELECT day_of_week, COUNT(*) FROM service_date "
            " WHERE exception_type = 1 GROUP BY day_of_week",
        ),
        bad=Case(
            "strftime('%w') evaluated per row",
            "SELECT CAST(strftime('%w', service_date) AS INTEGER) AS d, "
            "       COUNT(*) FROM service_date "
            " WHERE exception_type = 1 GROUP BY d",
        ),
        note="This is the WHERE strftime('%Y', scheduled_time) = '2026' "
             "anti-pattern from the study guide, in its GROUP BY form.",
    ),
]

INDEX_CASES: list[Case] = [
    Case(
        "Q1 hourly histogram",
        "SELECT departure_hour, COUNT(*) FROM stop_time "
        " WHERE is_boardable = 1 GROUP BY departure_hour",
        index="ix_stop_time_boardable_hour",
    ),
    Case(
        "Q2 Bruxelles-Central platform counts",
        "SELECT st.stop_id, COUNT(*) FROM stop_time st "
        " WHERE st.stop_id IN (SELECT stop_id FROM platform "
        "                       WHERE station_id = 'gs:nmbssncb:S8813003') "
        "   AND st.is_boardable = 1 GROUP BY st.stop_id",
        index="ix_stop_time_stop_boardable",
    ),
    Case(
        "Q5 amenity ratio per route",
        "SELECT route_id, COUNT(*), SUM(bikes_allowed = 1) FROM trip "
        " GROUP BY route_id",
        index="ix_trip_route_amenity",
    ),
]


def time_query(conn: sqlite3.Connection, sql: str, runs: int = 3) -> float:
    """Best-of-N elapsed seconds, after one untimed warm-up run.

    Best-of rather than mean: we are trying to measure the query, and the
    slowest run is mostly measuring what else the operating system was doing.
    The warm-up run exists so the first case in a suite is not penalised for
    paying the page-cache cost that every later case inherits.
    """
    conn.execute(sql).fetchall()          # warm-up, not timed
    best = float("inf")
    for _ in range(runs):
        started = time.perf_counter()
        conn.execute(sql).fetchall()
        best = min(best, time.perf_counter() - started)
    return best


def explain(conn: sqlite3.Connection, sql: str) -> str:
    """The query plan as a single line.

    Note for anyone extending this: after DDL you must obtain the plan from a
    connection opened *since* the DDL ran. Python's sqlite3 caches prepared
    statements per connection, and a cached EXPLAIN QUERY PLAN will happily
    keep reporting an index that has just been dropped — which is exactly the
    kind of "evidence" that makes a benchmark worse than no benchmark.
    :func:`run_index_suite` reconnects rather than risking it.
    """
    rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
    return " | ".join(r["detail"] for r in rows)


def run_sargability_suite(conn: sqlite3.Connection, runs: int) -> None:
    print("\n" + "=" * 78)
    print("SARGABILITY — the same answer, computed two ways")
    print("=" * 78)
    for pair in SARGABILITY_PAIRS:
        good = time_query(conn, pair.good.sql, runs)
        bad = time_query(conn, pair.bad.sql, runs)
        ratio = bad / good if good > 0 else float("inf")

        print(f"\n{pair.name}")
        print(f"  SARGable    {good:8.4f}s   {pair.good.label}")
        print(f"              plan: {explain(conn, pair.good.sql)}")
        print(f"  violation   {bad:8.4f}s   {pair.bad.label}")
        print(f"              plan: {explain(conn, pair.bad.sql)}")
        print(f"  -> the violation costs {ratio:,.0f}x more time")
        if pair.note:
            print(f"     {pair.note}")


def run_index_suite(conn: sqlite3.Connection, runs: int) -> None:
    print("\n" + "=" * 78)
    print("INDEX BENEFIT — timed with the index, then without it")
    print("=" * 78)
    print("The database is restored by re-running sql/04_indexes.sql at the end.\n")

    def fresh() -> sqlite3.Connection:
        """A connection opened after the most recent DDL, so plans are real."""
        handle = connect()
        handle.execute("PRAGMA cache_size = -262144")
        return handle

    results: list[tuple[str, float, float, str, str]] = []
    for case in INDEX_CASES:
        if not case.index:
            continue

        with_conn = fresh()
        with_index = time_query(with_conn, case.sql, runs)
        plan_with = explain(with_conn, case.sql)
        with_conn.close()

        conn.execute(f"DROP INDEX IF EXISTS {case.index}")
        conn.commit()

        without_conn = fresh()
        without_index = time_query(without_conn, case.sql, runs)
        plan_without = explain(without_conn, case.sql)
        without_conn.close()

        # Restore immediately, so a crash mid-suite cannot leave the database
        # missing an index the rest of the project depends on.
        run_sql_file(conn, config.SQL_DIR / "04_indexes.sql", atomic=False)
        conn.commit()

        results.append((case.label, with_index, without_index,
                        plan_with, plan_without))
        print(f"{case.label}")
        print(f"  with    {case.index:<32} {with_index:8.4f}s")
        print(f"          {plan_with}")
        print(f"  without {case.index:<32} {without_index:8.4f}s")
        print(f"          {plan_without}")
        speedup = without_index / with_index if with_index > 0 else float("inf")
        print(f"  -> the index is worth {speedup:,.1f}x\n")

    print("all indexes restored (sql/04_indexes.sql is idempotent)")

    print("\nsummary")
    print(f"  {'query':<42} {'with':>10} {'without':>10} {'speed-up':>10}")
    for label, with_index, without_index, _, _ in results:
        speedup = without_index / with_index if with_index > 0 else float("inf")
        print(f"  {label:<42} {with_index:>9.4f}s {without_index:>9.4f}s "
              f"{speedup:>9.1f}x")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="railpulse benchmark",
        description="Measure the cost of SARGable violations and the value of "
                    "each index.",
    )
    parser.add_argument("--runs", type=int, default=3,
                        help="timed repetitions per query; best is reported")
    parser.add_argument("--with-index-drops", action="store_true",
                        help="also DROP and restore indexes to measure their "
                             "benefit (writes to the database)")
    args = parser.parse_args(argv)

    if not config.DB_PATH.exists():
        raise SystemExit(f"{config.DB_PATH} does not exist — run `make build` first.")

    read_only = not args.with_index_drops
    conn = connect(read_only=read_only)
    conn.execute("PRAGMA cache_size = -262144")
    try:
        print(f"database: {config.DB_PATH} "
              f"({config.DB_PATH.stat().st_size / 1e6:,.0f} MB, "
              f"{'read-only' if read_only else 'read-write'})")
        run_sargability_suite(conn, args.runs)
        if args.with_index_drops:
            run_index_suite(conn, args.runs)
        else:
            print("\n(run with --with-index-drops to also measure each index "
                  "by removing it)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
