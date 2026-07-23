"""GTFS Static zip  ->  ``stg_*`` staging tables.

THE ONE RULE THIS MODULE OBEYS
------------------------------
It copies. It does not think.

Not a single value is inspected, cast, trimmed, filtered, defaulted or
reordered here. A cell that reads ``"87:16:00"`` in the file arrives in the
staging table as the string ``"87:16:00"``. Whether that is a valid departure
time is a question for ``sql/03_transform.sql``, which can quarantine the row
with an explanation — something a Python-side ``if`` could only do by throwing
information away.

That discipline is what keeps the project inside the challenge's constraint
("Python must *only* be used for the network requests and executing raw SQL")
and, more usefully, it means the cleaning rules are all readable in one place.

WHY COLUMNS ARE MAPPED BY NAME
------------------------------
The SNCB feed ships its CSV columns in **alphabetical** order, not GTFS spec
order — ``stops.txt`` starts with ``location_type``, and ``stop_times.txt``
starts with ``arrival_time``. Any positional loader would quietly write
latitudes into the ``zone_id`` column. So the loader reads the header row and
builds the INSERT from it, and reports any column it does not recognise instead
of guessing.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from . import config
from .api_client import BelgianMobilityClient, FetchResult
from .db import connect, executemany_batched, run_sql_file, transaction

# csv fields can be long (headsigns, alert texts); lift the default 128 KB cap.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

STATIC_ZIP_PATH = config.RAW_DIR / f"{config.OPERATOR}_gtfs_static.zip"
LAST_MODIFIED_PATH = config.RAW_DIR / f"{config.OPERATOR}_gtfs_static.last_modified"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def fetch_static_feed(
    client: BelgianMobilityClient | None = None,
    *,
    force: bool = False,
) -> FetchResult:
    """Download the GTFS Static zip, skipping the transfer when unchanged.

    The upstream ``Last-Modified`` from the previous run is cached next to the
    zip and replayed as ``If-Modified-Since``. The feed is regenerated once a
    day (03:05 UTC in practice), so re-running later the same day costs one
    round trip instead of 26 MB.
    """
    client = client or BelgianMobilityClient()
    print(f"  auth: {client.describe_auth()}")

    previous = None
    if not force and LAST_MODIFIED_PATH.exists() and STATIC_ZIP_PATH.exists():
        previous = LAST_MODIFIED_PATH.read_text(encoding="utf-8").strip() or None

    def _progress(written: int, total: int | None) -> None:
        if total:
            pct = 100.0 * written / total
            print(f"\r    downloading… {written / 1e6:6.1f} / {total / 1e6:0.1f} MB "
                  f"({pct:5.1f}%)", end="", flush=True)

    result = client.download_gtfs_static(
        STATIC_ZIP_PATH, if_modified_since=previous, progress=_progress
    )
    print()  # terminate the progress line

    if result.last_modified and not result.not_modified:
        LAST_MODIFIED_PATH.write_text(result.last_modified, encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# Row streaming
# ---------------------------------------------------------------------------
def _staging_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Column names of a staging table, excluding the synthetic line number."""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r["name"] for r in rows if r["name"] != "src_line_no"]


def _stream_rows(
    handle: io.TextIOBase,
    columns: Sequence[str],
    filename: str,
) -> Iterator[tuple[object, ...]]:
    """Yield one tuple per CSV row, aligned to *columns*, plus the line number.

    Missing columns become ``None`` (the file simply does not carry that
    optional GTFS field). Extra columns are reported once and ignored — the
    staging schema, not the feed, defines what we store.
    """
    reader = csv.DictReader(handle)
    header = reader.fieldnames or []

    # DictReader keeps the BOM on the first field name if one is present.
    if header and header[0].startswith("﻿"):
        header[0] = header[0].lstrip("﻿")
        reader.fieldnames = header

    known = set(columns)
    unexpected = [c for c in header if c and c not in known]
    if unexpected:
        print(f"    note: {filename} carries column(s) not in the staging "
              f"schema, ignored: {', '.join(unexpected)}")
    absent = [c for c in columns if c not in header]
    if absent:
        print(f"    note: {filename} omits optional column(s), stored NULL: "
              f"{', '.join(absent)}")

    # line 1 is the header, so the first data row is physical line 2
    for line_no, row in enumerate(reader, start=2):
        yield (line_no, *(row.get(column) for column in columns))


def load_zip_into_staging(
    conn: sqlite3.Connection,
    zip_path: Path = STATIC_ZIP_PATH,
) -> dict[str, int]:
    """Copy every recognised member of the GTFS zip into its staging table.

    Returns ``{filename: rows_loaded}``.
    """
    counts: dict[str, int] = {}

    with zipfile.ZipFile(zip_path) as archive:
        members = {Path(n).name for n in archive.namelist()}
        missing = [f for f in config.REQUIRED_GTFS_FILES if f not in members]
        if missing:
            raise RuntimeError(
                f"{zip_path.name} is missing required GTFS file(s): "
                f"{', '.join(missing)}"
            )
        extra = sorted(members - set(config.GTFS_FILE_TO_STAGING_TABLE))
        if extra:
            print(f"    note: zip members with no staging table, skipped: "
                  f"{', '.join(extra)}")

        for filename, table in config.GTFS_FILE_TO_STAGING_TABLE.items():
            if filename not in members:
                print(f"    – {filename}: absent from feed, skipped")
                counts[filename] = 0
                continue

            columns = _staging_columns(conn, table)
            quoted = ", ".join(f'"{c}"' for c in columns)
            placeholders = ", ".join(["?"] * (len(columns) + 1))
            statement = (
                f'INSERT INTO "{table}" (src_line_no, {quoted}) '
                f"VALUES ({placeholders})"
            )

            with archive.open(filename) as raw:
                # utf-8-sig strips the BOM some GTFS producers emit.
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                with transaction(conn):
                    loaded = executemany_batched(
                        conn, statement, _stream_rows(text, columns, filename)
                    )

            counts[filename] = loaded
            print(f"    – {filename:<22} -> {table:<20} {loaded:>9,} rows")

    return counts


# ---------------------------------------------------------------------------
# Orchestration for the staging half of the build
# ---------------------------------------------------------------------------
def stage_static_feed(
    conn: sqlite3.Connection,
    zip_path: Path = STATIC_ZIP_PATH,
    *,
    run_id: int | None = None,
) -> dict[str, int]:
    """Create the staging tables and fill them from *zip_path*."""
    print("  creating staging tables…")
    run_sql_file(conn, config.SQL_DIR / "01_staging.sql")
    print("  loading GTFS members…")
    counts = load_zip_into_staging(conn, zip_path)

    if run_id is not None:
        conn.execute(
            "UPDATE ingestion_run SET rows_staged = ? WHERE run_id = ?",
            (sum(counts.values()), run_id),
        )
        conn.commit()
    return counts


def utc_now() -> str:
    """ISO-8601 UTC timestamp used for every ``*_at_utc`` column."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "STATIC_ZIP_PATH",
    "fetch_static_feed",
    "load_zip_into_staging",
    "stage_static_feed",
    "utc_now",
]
