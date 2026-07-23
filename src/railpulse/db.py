"""SQLite connection management and SQL-script execution.

This is the only module that talks to the database engine. It exists so that
every connection in the project gets the same PRAGMAs — in particular
``foreign_keys = ON``, which SQLite disables by default and which the entire
integrity story of this project depends on.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from . import config

# --------------------------------------------------------------------------
# PRAGMAs applied to every connection
# --------------------------------------------------------------------------
# foreign_keys   SQLite ships with FK enforcement OFF for backwards
#                compatibility. Without this line every REFERENCES clause in
#                02_schema.sql is decorative documentation rather than a
#                constraint, and orphan rows load silently.
# journal_mode   WAL lets the Streamlit dashboard read while an ingestion job
#                writes, instead of hitting "database is locked".
# synchronous    NORMAL is the right trade for a rebuildable analytical store:
#                a power cut costs us a `make build`, not customer data.
# temp_store     DEFAULT (file-backed) on purpose — the stop_time screening
#                step materialises ~2.2 M rows and would otherwise fight the
#                OS for RAM.
# --------------------------------------------------------------------------
CONNECTION_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("foreign_keys", "ON"),
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("cache_size", "-262144"),   # negative = KiB, so 256 MiB of page cache
    ("busy_timeout", "30000"),
)

#: Extra PRAGMAs used only while bulk-loading. They trade durability for speed
#: and are never applied to a read/analysis connection.
BULK_LOAD_PRAGMAS: tuple[tuple[str, str], ...] = (
    ("synchronous", "OFF"),
)


def connect(
    db_path: Path | str | None = None,
    *,
    read_only: bool = False,
    bulk: bool = False,
) -> sqlite3.Connection:
    """Open a connection with the project's standard PRAGMAs applied.

    Parameters
    ----------
    db_path:
        Database file. Defaults to :data:`railpulse.config.DB_PATH`.
    read_only:
        Open through a ``file:...?mode=ro`` URI. Used by the dashboard and the
        analysis runner so an accidental ``DELETE`` in a hand-written query
        cannot damage the store.
    bulk:
        Also apply :data:`BULK_LOAD_PRAGMAS`. Only the ingestion jobs pass this.
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)

    conn.row_factory = sqlite3.Row
    for pragma, value in CONNECTION_PRAGMAS:
        # journal_mode is a no-op (and errors) on a read-only handle.
        if read_only and pragma in {"journal_mode", "synchronous"}:
            continue
        conn.execute(f"PRAGMA {pragma} = {value}")
    if bulk and not read_only:
        for pragma, value in BULK_LOAD_PRAGMAS:
            conn.execute(f"PRAGMA {pragma} = {value}")
    return conn


def iter_statements(sql_text: str) -> Iterator[str]:
    """Split a SQL script into individually executable statements.

    Naively splitting on ``;`` breaks the moment a semicolon appears inside a
    string literal or a comment — and this project's SQL is heavily commented.
    :func:`sqlite3.complete_statement` is the engine's own tokenizer-aware
    check for "is this a whole statement yet?", so it is used instead.

    Splitting matters because :meth:`sqlite3.Connection.executescript` issues an
    implicit ``COMMIT`` before it runs, which would silently break the
    all-or-nothing guarantee that :func:`run_sql_file` advertises.
    """
    buffer: list[str] = []
    for line in sql_text.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            buffer.clear()
            if statement and not _is_only_comments(statement):
                yield statement
    trailing = "".join(buffer).strip()
    if trailing and not _is_only_comments(trailing):
        yield trailing


def _is_only_comments(statement: str) -> bool:
    """True when *statement* contains nothing but ``--`` comments/whitespace."""
    for raw_line in statement.splitlines():
        line = raw_line.strip()
        if line and not line.startswith("--"):
            return False
    return True


def run_sql_file(
    conn: sqlite3.Connection,
    path: Path,
    *,
    atomic: bool = True,
    echo: bool = False,
) -> int:
    """Execute every statement in *path*.

    With ``atomic=True`` (the default) the whole file runs inside a single
    transaction: either every statement lands or none does. This is what makes
    ``03_transform.sql`` safe to re-run — a failure halfway through cannot leave
    the core model half-populated.

    Returns the number of statements executed.
    """
    sql_text = Path(path).read_text(encoding="utf-8")
    statements = list(iter_statements(sql_text))

    started = time.perf_counter()
    if atomic:
        conn.execute("BEGIN")
    try:
        for index, statement in enumerate(statements, start=1):
            if echo:
                first_line = statement.splitlines()[0][:100]
                print(f"    [{index}/{len(statements)}] {first_line}")
            conn.execute(statement)
    except Exception:
        if atomic:
            conn.rollback()
        raise
    else:
        if atomic:
            conn.commit()

    elapsed = time.perf_counter() - started
    print(f"  ✓ {path.name}: {len(statements)} statements in {elapsed:0.1f}s")
    return len(statements)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit BEGIN/COMMIT block with rollback on error."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def executemany_batched(
    conn: sqlite3.Connection,
    statement: str,
    rows: Iterable[Sequence[object]],
    *,
    batch_size: int | None = None,
) -> int:
    """Stream *rows* into the database in batches, returning the row count.

    The 2.2 M-row ``stop_times.txt`` never exists in memory as a whole: the
    caller hands over a generator and this function drains it one batch at a
    time.
    """
    size = batch_size or config.INSERT_BATCH_SIZE
    batch: list[Sequence[object]] = []
    total = 0
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            conn.executemany(statement, batch)
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(statement, batch)
        total += len(batch)
    return total


def table_counts(conn: sqlite3.Connection, tables: Sequence[str]) -> dict[str, int]:
    """Row count per table, for build summaries and the verification step."""
    counts: dict[str, int] = {}
    for table in tables:
        try:
            row = conn.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()
            counts[table] = int(row["n"])
        except sqlite3.Error:
            counts[table] = -1        # table absent — reported, not fatal
    return counts


def user_tables(conn: sqlite3.Connection, prefix: str = "") -> list[str]:
    """All non-internal table names, optionally filtered by *prefix*."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        " WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        " ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows if r["name"].startswith(prefix)]
