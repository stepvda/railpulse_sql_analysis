"""Full rebuild of the RailPulse database, from zip to indexed core model.

    railpulse build            # download if needed, then rebuild everything
    railpulse build --offline  # rebuild from the zip already in data/raw
    railpulse build --keep-staging

The build is a fixed pipeline of SQL scripts, executed in order:

    02_schema.sql    drop + recreate the core model, seed reference tables
    01_staging.sql   create the staging tables
    (python)         stream the zip's CSV members into staging, verbatim
    03_transform.sql staging -> core, applying the nine DQ rules
    04_indexes.sql   secondary indexes + ANALYZE
    05_views.sql     the semantic layer
    06_realtime.sql  real-time landing tables (additive; never dropped)
    07_cleanup.sql   drop staging  [unless --keep-staging]

Python's entire contribution is the ordering, the CSV streaming, and the
progress reporting. It makes no decision about any individual value.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from . import config
from .api_client import BelgianMobilityClient
from .db import connect, run_sql_file, table_counts
from .ingest_static import (
    STATIC_ZIP_PATH,
    fetch_static_feed,
    stage_static_feed,
    utc_now,
)

#: Core tables reported in the build summary, in dependency order.
CORE_TABLES = (
    "agency", "station", "platform", "route", "service", "service_date",
    "trip", "stop_time", "transfer", "text_translation", "feed_info",
)


def _start_run(conn: sqlite3.Connection, source_url: str) -> int:
    """Open an ``ingestion_run`` row and return its id."""
    cursor = conn.execute(
        "INSERT INTO ingestion_run (started_at_utc, source, source_url, status) "
        "VALUES (?, 'gtfs-static', ?, 'running')",
        (utc_now(), source_url),
    )
    conn.commit()
    return int(cursor.lastrowid)


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    http_status: int | None = None,
    bytes_downloaded: int | None = None,
    source_last_modified: str | None = None,
    notes: str | None = None,
) -> None:
    rows_loaded = conn.execute(
        "SELECT (SELECT COUNT(*) FROM stop_time) + (SELECT COUNT(*) FROM trip) "
        "     + (SELECT COUNT(*) FROM service_date) AS n"
    ).fetchone()["n"]
    rows_rejected = conn.execute(
        "SELECT COUNT(*) AS n FROM rejected_row WHERE source_table LIKE 'stg%'"
    ).fetchone()["n"]
    conn.execute(
        "UPDATE ingestion_run "
        "   SET finished_at_utc = ?, status = ?, http_status = ?, "
        "       bytes_downloaded = ?, source_last_modified = ?, "
        "       rows_loaded = ?, rows_rejected = ?, notes = ? "
        " WHERE run_id = ?",
        (utc_now(), status, http_status, bytes_downloaded, source_last_modified,
         rows_loaded, rows_rejected, notes, run_id),
    )
    conn.commit()


def build(
    *,
    offline: bool = False,
    keep_staging: bool = False,
    force_download: bool = False,
    zip_path: Path = STATIC_ZIP_PATH,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Run the whole pipeline. Returns the final row count per core table."""
    started = time.perf_counter()
    target_db = Path(db_path) if db_path else config.DB_PATH
    print(f"\n=== RailPulse build -> {target_db} ===\n")

    # -- 1. acquire the feed -------------------------------------------------
    fetch = None
    if offline:
        if not zip_path.exists():
            raise SystemExit(
                f"--offline was requested but {zip_path} does not exist. "
                f"Run `railpulse fetch-static` first."
            )
        print(f"[1/8] offline: using {zip_path} "
              f"({zip_path.stat().st_size / 1e6:0.1f} MB)")
    else:
        print("[1/8] fetching GTFS Static feed…")
        fetch = fetch_static_feed(
            BelgianMobilityClient(), force=force_download
        )

    conn = connect(target_db, bulk=True)
    run_id: int | None = None
    try:
        # -- 2. core schema --------------------------------------------------
        print("\n[2/8] creating the core schema…")
        run_sql_file(conn, config.SQL_DIR / "02_schema.sql")

        run_id = _start_run(conn, config.GTFS_STATIC_URL)

        # -- 3. staging ------------------------------------------------------
        print(f"\n[3/8] staging the feed (run_id={run_id})…")
        staged = stage_static_feed(conn, zip_path, run_id=run_id)
        print(f"  staged {sum(staged.values()):,} rows across "
              f"{len(staged)} files")

        # -- 4. transform ----------------------------------------------------
        print("\n[4/8] transforming staging -> core model "
              "(single transaction)…")
        run_sql_file(conn, config.SQL_DIR / "03_transform.sql")

        # 03_transform.sql is a static file and cannot know which ingestion run
        # it belongs to, so the quarantine rows it writes come out with a NULL
        # run_id. Stamping them here closes the audit trail: every rejected row
        # can be traced to the run that produced it, and from there to the feed
        # version and upstream Last-Modified that run recorded.
        stamped = conn.execute(
            "UPDATE rejected_row SET run_id = ? "
            " WHERE run_id IS NULL AND source_table LIKE 'stg%'",
            (run_id,),
        ).rowcount
        conn.commit()
        if stamped:
            print(f"  ✓ linked {stamped:,} quarantined row(s) to run {run_id}")

        # -- 5. indexes ------------------------------------------------------
        print("\n[5/8] building indexes and refreshing statistics…")
        run_sql_file(conn, config.SQL_DIR / "04_indexes.sql", atomic=False)

        # -- 6. views + realtime ---------------------------------------------
        print("\n[6/8] creating views and real-time tables…")
        run_sql_file(conn, config.SQL_DIR / "05_views.sql")
        run_sql_file(conn, config.SQL_DIR / "06_realtime.sql")

        # -- 7. cleanup ------------------------------------------------------
        if keep_staging:
            print("\n[7/8] keeping staging tables (--keep-staging)")
        else:
            print("\n[7/8] dropping staging tables and compacting…")
            run_sql_file(conn, config.SQL_DIR / "07_cleanup.sql", atomic=False)
            # VACUUM cannot run inside a transaction, hence atomic=False above
            # and an explicit commit before it.
            conn.commit()
            conn.execute("VACUUM")
            conn.execute("ANALYZE")

        _finish_run(
            conn, run_id,
            status="ok",
            http_status=fetch.status_code if fetch else None,
            bytes_downloaded=fetch.bytes_downloaded if fetch else None,
            source_last_modified=fetch.last_modified if fetch else None,
            notes="offline rebuild" if offline else None,
        )

        # -- 8. summary ------------------------------------------------------
        print("\n[8/8] build summary")
        counts = table_counts(conn, CORE_TABLES)
        width = max(len(t) for t in counts)
        for table, count in counts.items():
            print(f"    {table:<{width}}  {count:>10,}")

        rejected = conn.execute(
            "SELECT rule_code, COUNT(*) AS n FROM rejected_row "
            " WHERE source_table LIKE 'stg%' "
            " GROUP BY rule_code ORDER BY n DESC"
        ).fetchall()
        if rejected:
            print("\n    quarantined rows (see docs/data_quality.md):")
            for row in rejected:
                print(f"      {row['rule_code']:<34} {row['n']:>8,}")
        else:
            print("\n    quarantined rows: none")

        size_mb = target_db.stat().st_size / 1e6
        elapsed = time.perf_counter() - started
        print(f"\n=== done in {elapsed:0.1f}s · database {size_mb:0.0f} MB ===\n")
        return counts

    except Exception:
        if run_id is not None:
            try:
                conn.rollback()
                _finish_run(conn, run_id, status="failed", notes="see traceback")
            except Exception:   # pragma: no cover - best effort only
                pass
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="railpulse build",
        description="Rebuild the RailPulse SQLite database from the GTFS feed.",
    )
    parser.add_argument("--offline", action="store_true",
                        help="skip the download and use data/raw/*.zip as-is")
    parser.add_argument("--keep-staging", action="store_true",
                        help="do not drop the stg_* tables after transforming")
    parser.add_argument("--force-download", action="store_true",
                        help="ignore the cached Last-Modified and re-download")
    parser.add_argument("--db", type=Path, default=None,
                        help="write to this database file instead of the default")
    args = parser.parse_args(argv)

    build(
        offline=args.offline,
        keep_staging=args.keep_staging,
        force_download=args.force_download,
        db_path=args.db,
    )
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
