"""Distil-Text2SQL — a local, distilled text-to-SQL inference engine.

Uses a small HuggingFace transformer model fine-tuned for text-to-SQL to
translate natural-language questions into SQLite queries, with full schema
context injection extracted dynamically from the live database.

The model runs entirely on CPU — no GPU required. First load downloads ~200 MB
and caches the model for subsequent runs.

Configuration
-------------
Set ``TEXT2SQL_MODEL`` in the environment (or ``.env``) to override the default:

  TEXT2SQL_MODEL=juierror/flan-t5-text2sql-with-schema-v2   (default)
  TEXT2SQL_MODEL=mrm8488/t5-small-finetuned-wikiSQL          (lighter, faster)

The default model is Flan-T5-small (0.2B params) fine-tuned on SPIDER/CoSQL.
It accepts multiple tables and handles the '<' operator.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Optional

import streamlit as st

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.environ.get(
    "TEXT2SQL_MODEL",
    "juierror/flan-t5-text2sql-with-schema-v2",
)

# --------------------------------------------------------------------------
# Execution safety limits. A text-to-SQL model can emit a perfectly valid but
# ruinously expensive query — a cartesian join over the 2.17 M-row fact table,
# or a bare `SELECT * FROM stop_time`. The read-only connection stops *writes*;
# these stop a runaway *read* from hanging or OOM-ing the dashboard.
# --------------------------------------------------------------------------
#: Hard ceiling on rows pulled into memory. One extra is fetched so the UI can
#: say "showing the first N of more".
MAX_RESULT_ROWS = int(os.environ.get("TEXT2SQL_MAX_ROWS", "5000"))
#: Wall-clock budget for a single query. A SQLite progress handler checks the
#: clock every few thousand VM ops and aborts the statement once it is exceeded,
#: so even a query that never returns a row cannot run forever.
QUERY_TIMEOUT_SECONDS = float(os.environ.get("TEXT2SQL_TIMEOUT", "10"))
#: How often (in SQLite virtual-machine instructions) the progress handler runs.
_PROGRESS_OPS = 100_000

PROSE_SCHEMA = """### SQLite SQL tables, with their properties:
#
# station(station_id TEXT PK, station_name TEXT, latitude REAL, longitude REAL, wheelchair_boarding INTEGER)
#   — 652 rail hubs (location_type=1). Names are in French (feed_lang='fr').
#   — wheelchair_boarding: 0=no info, 1=yes, 2=no. Only code 1 is a guarantee.
#
# platform(stop_id TEXT PK, station_id TEXT FK→station, platform_code TEXT, has_platform_code INTEGER)
#   — 2 243 boarding points. platform_code is NULL when no track allocated.
#   — has_platform_code=1 for real numbered platforms; JOIN to station for station_name.
#
# route(route_id TEXT PK, agency_id TEXT FK→agency, route_short_name TEXT, route_long_name TEXT, route_type INTEGER)
#   — 1 801 routes (1 531 Rail / route_type=2, 270 Bus / route_type=3).
#
# trip(trip_id TEXT PK, route_id TEXT FK→route, service_id TEXT FK→service, trip_headsign TEXT, direction_id INTEGER, wheelchair_accessible INTEGER, bikes_allowed INTEGER)
#   — 134 809 trips. trip_headsign is the terminal destination.
#   — bikes_allowed/wheelchair_accessible: 0=no info, 1=yes, 2=no.
#
# service(service_id TEXT PK, monday..sunday INTEGER, start_date TEXT, end_date TEXT, has_weekday_pattern INTEGER)
#   — 51 593 calendar patterns. All weekday flags are 0 — real dates are in service_date.
#
# service_date(service_id TEXT, service_date TEXT, exception_type INTEGER, day_of_week INTEGER)
#   — 4.7 M exploded operating dates. exception_type=1 means ADDED.
#   — day_of_week: 0=Sunday..6=Saturday.
#
# stop_time(trip_id TEXT, stop_sequence INTEGER, stop_id TEXT FK→platform, arrival_time TEXT, departure_time TEXT, departure_secs INTEGER, departure_hour INTEGER, day_offset INTEGER, pickup_type INTEGER, drop_off_type INTEGER, is_boardable INTEGER, is_alightable INTEGER)
#   — 2.17 M calls. departure_hour=0–23. is_boardable=1 means passengers may board.
#
# transfer(transfer_id INTEGER PK AUTO, from_stop_id TEXT, to_stop_id TEXT, transfer_type INTEGER, min_transfer_time INTEGER)
#
# text_translation(table_name TEXT, field_name TEXT, field_value TEXT, lang TEXT, translation TEXT)
#   — nl/de/en station/headsign names.
#
# agency(agency_id TEXT PK, agency_name TEXT, agency_timezone TEXT) — NMBS/SNCB.
# feed_info(feed_id TEXT PK, feed_publisher_name TEXT, feed_start_date TEXT, feed_end_date TEXT, feed_version TEXT)
# ref_route_type(route_type INTEGER PK, label TEXT) — 2=Rail, 3=Bus.
# ref_accessibility(code INTEGER PK, label TEXT, is_guaranteed INTEGER)
#
# ### VIEWS:
# v_departure — canonical departure event (is_boardable=1, departure_secs NOT NULL).
#   Columns: trip_id, stop_sequence, stop_id, station_id, station_name,
#     platform_code, departure_time, departure_secs, departure_hour, day_offset,
#     route_id, service_id, trip_headsign, route_short_name, route_long_name,
#     route_type, route_type_label
#
# v_trip_service_days(trip_id, operating_days, first_operating_day, last_operating_day)
#   — JOIN on trip_id and multiply by operating_days to annualise.
#
# v_trip_origin — first boardable call of every trip.
#   Columns: trip_id, stop_sequence, stop_id, station_id, station_name,
#     departure_time, departure_secs, departure_hour, day_offset,
#     route_id, service_id, trip_headsign, route_short_name, route_long_name, route_type
#
# v_service_frequency(service_id, distinct_weekdays, typical_days_per_week, frequency_class)
#   — frequency_class: 'High Frequency'(≥5), 'Medium Frequency'(2–4), 'Low Frequency/Special'(1).
#
# v_station_daily_departures(station_id, station_name, service_date, day_of_week, departure_count)
# v_trip_amenity(trip_id, bikes_allowed_label, wheelchair_accessible_label)
#
# ### CRITICAL RULES:
# 1. Use v_departure for departures, NOT stop_time (excludes pass-throughs).
# 2. Annualise: JOIN v_trip_service_days ON trip_id, multiply by operating_days.
# 3. Station names are in French (e.g. 'Anvers-Central', 'Bruxelles-Midi').
# 4. route_type=2 is Rail, route_type=3 is Bus.
# 5. wheelchair_accessible is 0 for EVERY trip (field unpopulated).
# 6. departure_hour = 0–23 (passenger-facing clock hour).
# 7. SQLite date functions: strftime(), julianday(), date().
# 8. String concatenation: || operator.
# 9. Percentages: ROUND(100.0 * part / total, 1).
# 10. printf() for zero-padding. Example: printf('%02d:00', departure_hour).
"""

_TABLE_WHITELIST = {
    "agency", "feed_info", "ingestion_run", "rejected_row",
    "platform", "route", "service", "service_date",
    "station", "stop_time", "transfer", "text_translation", "trip",
    "ref_accessibility", "ref_exception_type", "ref_location_type",
    "ref_pickup_drop", "ref_route_type", "ref_transfer_type",
}

_VIEW_WHITELIST = {
    "v_departure", "v_trip_service_days", "v_trip_origin",
    "v_service_frequency", "v_station_daily_departures", "v_trip_amenity",
}


def _get_db_connection():
    """Open a read-only connection to the RailPulse database.

    Imported lazily to avoid circular imports with app.py.
    """
    from railpulse.db import connect
    return connect(read_only=True)


def extract_table_schemas() -> dict[str, list[str]]:
    """Extract (table_name → [column_names]) from the live database.

    Only includes user tables and views (excludes sqlite_* internal tables).
    Returns the result as a dictionary usable by the text-to-SQL model.
    """
    conn = _get_db_connection()
    try:
        tables: dict[str, list[str]] = {}
        for table in sorted(_TABLE_WHITELIST):
            try:
                rows = conn.execute(f"SELECT * FROM \"{table}\" LIMIT 0")
                columns = [col[0] for col in rows.description or ()]
                if columns:
                    tables[table] = columns
            except Exception:
                pass
        for view in sorted(_VIEW_WHITELIST):
            try:
                rows = conn.execute(f"SELECT * FROM \"{view}\" LIMIT 0")
                columns = [col[0] for col in rows.description or ()]
                if columns:
                    tables[view] = columns
            except Exception:
                pass
        return tables
    finally:
        conn.close()


def format_schema_for_model(tables: dict[str, list[str]] | None = None) -> str:
    """Format table schemas into the model's expected prompt format.

    The default model expects::

        table_name(col1, col2), table2(col1, col2)

    Multiple tables are comma-separated.
    """
    if tables is None:
        tables = extract_table_schemas()
    parts = [f"{name}({','.join(cols)})" for name, cols in sorted(tables.items())]
    return ", ".join(parts)


def _clean_sql(raw: str) -> str:
    """Strip markdown fences, whitespace, and trailing semicolons."""
    text = raw.strip()
    text = re.sub(r"^```(?:sql|sqlite)?\s*\n?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip().rstrip(";").strip()
    while text.endswith(";"):
        text = text[:-1].strip()
    return text


#: Keywords that must never appear as a *statement verb* in generated SQL.
#: Matched as whole words anywhere in the statement, not just at the start.
_DESTRUCTIVE_KEYWORDS = (
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "REPLACE",
    "TRUNCATE", "ATTACH", "DETACH", "PRAGMA", "VACUUM", "REINDEX",
    "GRANT", "REVOKE",
)
_DESTRUCTIVE_RE = re.compile(
    r"\b(" + "|".join(_DESTRUCTIVE_KEYWORDS) + r")\b", re.IGNORECASE
)
#: A `;` followed by anything other than trailing whitespace is a second
#: statement — the stacked-query pattern (`SELECT 1; DROP TABLE x`).
_STACKED_RE = re.compile(r";\s*\S")
#: SQL comments can hide a keyword from a naive scan; strip them before checking.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
#: A single-quoted string literal, honouring SQL's doubled-quote ('') escape.
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line and /* block */ comments so they cannot hide keywords."""
    sql = _BLOCK_COMMENT_RE.sub(" ", sql)
    sql = _LINE_COMMENT_RE.sub(" ", sql)
    return sql


def _mask_string_literals(sql: str) -> str:
    """Blank out single-quoted string contents before the keyword scan.

    A legitimate value filter — ``WHERE station_name = 'La Louvière'`` or even
    ``= 'DROP'`` — must not be mistaken for a destructive statement. Masking the
    literal (rather than deleting it) preserves statement structure so the
    stacked-statement and verb checks still see the real SQL. Destructive verbs
    never live inside a string literal, so nothing is lost.
    """
    return _STRING_LITERAL_RE.sub("''", sql)


def _is_safe_sql(sql: str) -> bool:
    """True only for a single read-only SELECT / WITH…SELECT statement.

    This is the guardrail layer of a defence in depth (the other two being the
    read-only connection and SQLite's one-statement-per-execute limit). Unlike a
    first-word check, it inspects the WHOLE statement, because a valid-looking
    ``SELECT`` can still smuggle a destructive verb — in a stacked statement, a
    subquery, or behind a comment. Rejection rules, in order:

    * strip comments first, so ``SELECT 1 --\\nDROP TABLE x`` cannot hide the DROP;
    * mask string literals, so ``WHERE name = 'DROP'`` is not a false positive;
    * no stacked statements (a ``;`` followed by more SQL);
    * no destructive keyword anywhere, as a whole word;
    * must actually begin with SELECT or WITH.

    A false negative here is harmless (the query is refused and the user
    rephrases); a false positive is caught downstream by the read-only handle.
    The verb/stacked checks run on the comment-stripped, literal-masked text; the
    SELECT/WITH prefix check runs on the same, so a leading string literal cannot
    disguise the statement.
    """
    scan = _mask_string_literals(_strip_sql_comments(sql)).strip()
    if not scan:
        return False
    if _STACKED_RE.search(scan):
        return False
    if _DESTRUCTIVE_RE.search(scan):
        return False
    upper = scan.upper()
    return upper.startswith("SELECT") or upper.startswith("WITH")


@st.cache_resource(show_spinner="Loading Distil-Text2SQL model (first run downloads ~200 MB)…")
def _load_model(model_name: str = DEFAULT_MODEL):
    """Load the HuggingFace text-to-SQL pipeline once and cache it.

    Uses ``@st.cache_resource`` so the model survives Streamlit reruns.
    """
    from transformers import pipeline

    logger.info("Loading text-to-SQL model: %s", model_name)
    pipe = pipeline(
        "text2text-generation",
        model=model_name,
        device=-1,
    )
    logger.info("Model loaded: %s", model_name)
    return pipe


#: Which schema representation to put in the prompt.
#:   "compact" — `table(col1,col2), ...`  the terse form the small default model
#:               was fine-tuned on. Fits its context window; carries no semantics.
#:   "rich"    — the full PROSE_SCHEMA with row counts, code meanings and the
#:               CRITICAL RULES (use v_departure, annualise, French names, …).
#:               Only useful for a model big enough to read and follow it.
#: Default "compact" so the shipped default model behaves as trained; set
#: TEXT2SQL_SCHEMA_MODE=rich when you point TEXT2SQL_MODEL at a capable model.
SCHEMA_MODE = os.environ.get("TEXT2SQL_SCHEMA_MODE", "compact").strip().lower()


def build_prompt(
    question: str,
    schema_tables: dict[str, list[str]] | None = None,
    *,
    schema_mode: str | None = None,
) -> str:
    """Assemble the prompt sent to the model.

    ``schema_mode`` (default from ``TEXT2SQL_SCHEMA_MODE``) selects how the schema
    is described:

    * ``"compact"`` injects the terse ``table(cols)`` list the default flan-t5
      model expects — it fits that model's small context but conveys no
      semantics, so the model cannot know to prefer ``v_departure`` or to
      annualise.
    * ``"rich"`` injects the full :data:`PROSE_SCHEMA`, including row counts, the
      meaning of every code, and the CRITICAL RULES. This is what makes a
      *capable* model produce correct multi-table SQL. It is far longer than the
      default 0.2B model can use, which is exactly why it is opt-in.

    Either way the schema is genuinely sent to the model; ``PROSE_SCHEMA`` is no
    longer display-only. Override the wrapper text via ``TEXT2SQL_PROMPT_TEMPLATE``.
    """
    mode = (schema_mode or SCHEMA_MODE)
    if mode == "rich":
        schema = PROSE_SCHEMA
    else:
        schema = format_schema_for_model(schema_tables)
    template = os.environ.get(
        "TEXT2SQL_PROMPT_TEMPLATE",
        "convert question and table into SQL query. tables: {schema}. question: {question}",
    )
    return template.format(question=question.strip(), schema=schema)


def generate_sql(
    question: str,
    model_name: str | None = None,
    schema: dict[str, list[str]] | None = None,
) -> str:
    """Translate a natural-language question into a SQLite SELECT statement.

    Parameters
    ----------
    question:
        The user's question in natural language.
    model_name:
        HuggingFace model ID. Defaults to ``TEXT2SQL_MODEL`` env var.
    schema:
        Table schemas dict. Auto-extracted from the database if omitted.

    Returns
    -------
    A cleaned SQL SELECT statement ready for execution.

    Raises
    ------
    ValueError
        If the model produced empty output or a non-SELECT statement.
    RuntimeError
        If the transformers library is not installed.
    """
    try:
        pipe = _load_model(model_name or DEFAULT_MODEL)
    except ImportError:
        raise RuntimeError(
            "SQL Chat needs the text-to-SQL model stack (torch + transformers), "
            "which is not installed. It is kept separate from the dashboard "
            "because it is ~2 GB. Install it with:\n\n"
            "    make setup-chat        (or: pip install -e \".[chat]\")\n\n"
            "then reload this page. The rest of the dashboard works without it."
        ) from None

    tables = schema if schema is not None else extract_table_schemas()
    prompt = build_prompt(question, tables)
    result = pipe(prompt, max_length=512, do_sample=False)[0]["generated_text"]

    sql = _clean_sql(result)

    if not sql:
        raise ValueError(
            f"The model returned an empty response for:\n\n  {question}\n\n"
            "Try rephrasing the question or adding more detail."
        )

    if not _is_safe_sql(sql):
        raise ValueError(
            f"The model generated a non-SELECT statement:\n\n```sql\n{sql}\n```\n\n"
            "Only read-only SELECT queries are allowed."
        )

    return sql


def sanitize_for_execution(sql: str) -> str:
    """Normalise SQL artefacts for SQLite execution.

    Strips semicolons and replaces backtick quoting with double-quote.
    """
    sql = _clean_sql(sql)
    sql = sql.replace("``", '"')
    sql = sql.replace("`", '"')
    return sql


class QueryTimeout(Exception):
    """Raised when a generated query exceeds its wall-clock budget."""


def execute_readonly_capped(
    sql: str,
    *,
    max_rows: int = MAX_RESULT_ROWS,
    timeout_seconds: float = QUERY_TIMEOUT_SECONDS,
) -> tuple[list[str], list[tuple], bool]:
    """Run *sql* against the read-only database with time and row limits.

    This is the execution half of the guardrail. `generate_sql` already checked
    the statement is a read-only SELECT; here we make sure that even a valid but
    pathological SELECT — a cartesian join, or `SELECT * FROM stop_time` — cannot
    hang the dashboard or exhaust memory.

    Two independent limits:

    * **Time.** A SQLite *progress handler* fires every ``_PROGRESS_OPS`` virtual
      -machine instructions and aborts the statement (raising
      :class:`QueryTimeout`) once ``timeout_seconds`` have elapsed. This catches
      a query that spins without ever yielding a row — the cartesian-join case,
      which no row cap alone would stop.
    * **Rows.** We ``fetchmany(max_rows + 1)`` rather than ``fetchall()``, so at
      most ``max_rows + 1`` rows ever reach memory. The extra row is the signal
      that the result was truncated.

    Returns ``(columns, rows, truncated)``. Raises :class:`QueryTimeout` on
    timeout and lets any :class:`sqlite3.Error` propagate to the caller.
    """
    conn = _get_db_connection()
    deadline = time.monotonic() + timeout_seconds
    timed_out = {"flag": False}

    def _progress() -> int:
        # Returning non-zero from a progress handler aborts the current
        # statement. We record why so the caller can distinguish a timeout from
        # a genuine SQL error.
        if time.monotonic() > deadline:
            timed_out["flag"] = True
            return 1
        return 0

    try:
        conn.set_progress_handler(_progress, _PROGRESS_OPS)
        try:
            cursor = conn.execute(sql)
            columns = [col[0] for col in cursor.description or ()]
            rows = [tuple(r) for r in cursor.fetchmany(max_rows + 1)]
        except sqlite3.OperationalError as exc:
            # A progress-handler abort surfaces here as "interrupted".
            if timed_out["flag"]:
                raise QueryTimeout(
                    f"Query exceeded the {timeout_seconds:.0f}s budget and was "
                    f"cancelled. It is probably a very large join — add a filter "
                    f"or a LIMIT and try again."
                ) from None
            raise
        finally:
            conn.set_progress_handler(None, 0)

        truncated = len(rows) > max_rows
        if truncated:
            rows = rows[:max_rows]
        return columns, rows, truncated
    finally:
        conn.close()
