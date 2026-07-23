"""``python -m railpulse <command>`` — one entry point for the whole pipeline.

    railpulse fetch       download the GTFS Static feed into data/raw/
    railpulse build       rebuild the database from that feed
    railpulse poll        append one GTFS-Realtime snapshot
    railpulse analyse     run sql/analysis/*.sql, write output/ and the report
    railpulse verify      assert the built database is internally consistent
    railpulse benchmark   measure index and SARGability effects
    railpulse info        what is in the database right now
    railpulse all         fetch + build + verify + analyse

Each subcommand delegates to its own module and forwards the remaining
arguments, so ``railpulse build --help`` shows the build options rather than
this list.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, config

COMMANDS = {
    "fetch": "download the GTFS Static feed into data/raw/",
    "build": "rebuild the database from the downloaded feed",
    "poll": "append one GTFS-Realtime snapshot (trip updates + alerts)",
    "analyse": "run sql/analysis/*.sql and publish results",
    "verify": "assert the built database is internally consistent",
    "benchmark": "measure index and SARGability effects",
    "info": "summarise what is currently in the database",
    "all": "fetch + build + verify + analyse",
}


def _cmd_fetch(argv: list[str]) -> int:
    from .api_client import BelgianMobilityClient
    from .ingest_static import fetch_static_feed

    parser = argparse.ArgumentParser(prog="railpulse fetch")
    parser.add_argument("--force", action="store_true",
                        help="ignore the cached Last-Modified and re-download")
    args = parser.parse_args(argv)
    fetch_static_feed(BelgianMobilityClient(), force=args.force)
    return 0


def _cmd_info(argv: list[str]) -> int:
    """A quick, honest status line: what is loaded, from when, how clean."""
    from .db import connect

    argparse.ArgumentParser(prog="railpulse info").parse_args(argv)

    if not config.DB_PATH.exists():
        print(f"{config.DB_PATH} does not exist. Run `railpulse build`.")
        return 1

    conn = connect(read_only=True)
    try:
        # The file may exist but be empty or half-built. `info` is the command a
        # user reaches for to diagnose exactly that, so it must not itself die
        # with a raw "no such table" — report the state and stop.
        has_schema = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            " WHERE type = 'table' AND name = 'feed_info'"
        ).fetchone()[0]
        if not has_schema:
            print(f"database        {config.DB_PATH}  "
                  f"({config.DB_PATH.stat().st_size / 1e6:,.0f} MB)")
            print("state           file exists but has no schema — "
                  "run `railpulse build`")
            return 1

        feed = conn.execute(
            "SELECT * FROM feed_info LIMIT 1").fetchone()
        run = conn.execute(
            "SELECT * FROM ingestion_run WHERE source = 'gtfs-static' "
            "   AND status = 'ok' ORDER BY run_id DESC LIMIT 1").fetchone()

        size_mb = config.DB_PATH.stat().st_size / 1e6
        print(f"database        {config.DB_PATH}  ({size_mb:,.0f} MB)")
        if feed:
            print(f"feed            {feed['feed_id']} "
                  f"version {feed['feed_version']}")
            print(f"timetable spans {feed['feed_start_date']} "
                  f"-> {feed['feed_end_date']}")
        if run:
            print(f"ingested        {run['started_at_utc']} "
                  f"(upstream {run['source_last_modified']})")
            print(f"quarantined     {run['rows_rejected']:,} rows")
        print(f"attribution     "
              f"{config.ATTRIBUTION_TEMPLATE.format(feed_date=feed['feed_version'] if feed else '?')}")
        print(f"licence         {config.DATA_LICENCE}")

        print("\ncore tables")
        for table in ("station", "platform", "route", "service", "service_date",
                      "trip", "stop_time", "transfer", "text_translation"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            print(f"  {table:<18} {count:>12,}")

        rt = conn.execute(
            "SELECT feed, COUNT(*) AS snapshots, MIN(fetched_at_utc) AS first, "
            "       MAX(fetched_at_utc) AS last "
            "  FROM rt_snapshot GROUP BY feed").fetchall()
        if rt:
            print("\nreal-time snapshots")
            for row in rt:
                print(f"  {row['feed']:<14} {row['snapshots']:>5}  "
                      f"{row['first']} -> {row['last']}")
        else:
            print("\nreal-time snapshots: none yet (run `railpulse poll`)")

        rejected = conn.execute(
            "SELECT rule_code, COUNT(*) AS n FROM rejected_row "
            " GROUP BY rule_code ORDER BY n DESC").fetchall()
        if rejected:
            print("\nquarantine")
            for row in rejected:
                print(f"  {row['rule_code']:<36} {row['n']:>8,}")
    finally:
        conn.close()
    return 0


def _cmd_all(argv: list[str]) -> int:
    from .analyse import main as analyse_main
    from .build import build
    from .verify import verify

    parser = argparse.ArgumentParser(prog="railpulse all")
    parser.add_argument("--offline", action="store_true",
                        help="skip the download and use the cached zip")
    args = parser.parse_args(argv)

    build(offline=args.offline)
    if not verify(quiet=True):
        print("\nverification failed — not publishing results.")
        return 1
    return analyse_main([])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    parser = argparse.ArgumentParser(
        prog="railpulse",
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="commands:\n" + "\n".join(
            f"  {name:<11} {help_text}" for name, help_text in COMMANDS.items()
        ),
    )
    parser.add_argument("command", nargs="?", choices=sorted(COMMANDS),
                        help="see the list below")
    parser.add_argument("--version", action="version",
                        version=f"railpulse {__version__}")
    known, rest = parser.parse_known_args(argv[:1])

    if not known.command:
        parser.print_help()
        return 0

    remaining = argv[1:]

    if known.command == "fetch":
        return _cmd_fetch(remaining)
    if known.command == "info":
        return _cmd_info(remaining)
    if known.command == "all":
        return _cmd_all(remaining)
    if known.command == "build":
        from .build import main as build_main
        return build_main(remaining)
    if known.command == "poll":
        from .ingest_realtime import main as poll_main
        return poll_main(remaining)
    if known.command == "analyse":
        from .analyse import main as analyse_main
        return analyse_main(remaining)
    if known.command == "verify":
        from .verify import main as verify_main
        return verify_main(remaining)
    if known.command == "benchmark":
        from .benchmark import main as benchmark_main
        return benchmark_main(remaining)

    parser.print_help()
    return 1


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
