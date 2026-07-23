"""RailPulse — Streamlit report layer over the SNCB/NMBS GTFS database.

WHAT THIS FILE IS ALLOWED TO DO

The challenge forbids using a data-frame engine to filter or aggregate. This
dashboard therefore contains no analysis at all: it is a renderer. Every figure
on every page is produced by SQL — either a labelled block loaded verbatim from
``sql/analysis/qN_*.sql``, or one of the module-level SQL constants below, which
exist only where the analysis files could not be reused (parameterised station
lookups, provenance, the KPI header, the data-quality evidence table).

Nothing is recomputed in Python. If a number looks wrong, the query that
produced it is one ``Show the SQL`` expander away.

Run it from the repository root:

    streamlit run dashboard/app.py
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

# --------------------------------------------------------------------------
# Import bootstrap
# --------------------------------------------------------------------------
# `streamlit run dashboard/app.py` puts dashboard/ on sys.path, not the repo
# root, and the package lives under src/ rather than at the top level. Without
# this the very first `from railpulse...` line fails with ModuleNotFoundError,
# which is a confusing way to learn that the layout is src/. Done before any
# project import, so the order of the lines below matters.
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
# Also add the repo root so `dashboard` is importable as a package (needed by
# the SQL Chat page which lives inside the dashboard/ directory).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import altair as alt

# pandas is used ONLY to hand already-aggregated SQL result rows to Streamlit
# and Altair for rendering. No filtering, no groupby, no merge, no pivot, no
# resampling happens in pandas anywhere in this file — every number displayed
# comes out of the database already aggregated. The DataFrame is a transport
# container, nothing more.
import pandas as pd
import streamlit as st

from railpulse import config
from railpulse.db import connect, iter_statements

from dashboard.sql_chat_page import page_sql_chat

# --------------------------------------------------------------------------
# A note on `use_container_width=True`
# --------------------------------------------------------------------------
# Streamlit deprecated that argument at the end of 2025 in favour of
# width="stretch", and current releases log a warning for every element that
# still uses it. It is kept anyway, deliberately: requirements-dashboard.txt
# pins streamlit>=1.36,<2, and width="stretch" does not exist on the older end
# of that range — st.dataframe there takes `width` as a pixel count, so the new
# spelling would not warn, it would fail. Switching on a parsed version number
# to pick the spelling trades a cosmetic warning for a way to break every table
# in the app on a version nobody tested. The deprecated form works across the
# whole pinned range; revisit it when the floor moves past 1.47.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------
# Two-series comparisons everywhere in this report are "the naive reading" vs
# "the corrected reading", so they need to survive a monochrome print-out and a
# colour-vision deficiency. Blue/orange is used rather than red/green for
# exactly that reason, and the two are never the only distinction — the series
# is always named in the legend and repeated in the table underneath.
# --------------------------------------------------------------------------
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#0d366b"]

FREQUENCY_CLASS_ORDER = ["High Frequency", "Medium Frequency", "Low Frequency/Special"]
FREQUENCY_CLASS_COLOURS = [BLUE, ORANGE, AQUA]


# ===========================================================================
# SQL constants
# ===========================================================================
# Everything below is inline SQL that has no equivalent in sql/analysis/.
# Each one is a module-level constant rather than an f-string built at the call
# site so that it can be read, diffed and pasted into sqlite3 by a reviewer.
# ===========================================================================

# Provenance. The licence obliges attribution with the feed date, so the feed
# version and the load timestamp are shown together — "the data is from 2026-07-20
# and we fetched it at 08:06Z" is two different facts and both belong on screen.
SQL_FEED_PROVENANCE = """
SELECT
    f.feed_publisher_name,
    f.feed_publisher_url,
    f.feed_lang,
    f.feed_start_date,
    f.feed_end_date,
    f.feed_version,
    CAST(julianday(f.feed_end_date) - julianday(f.feed_start_date) AS INTEGER)
        AS feed_window_days,
    (SELECT a.agency_name FROM agency a ORDER BY a.agency_id LIMIT 1)
        AS agency_name,
    (SELECT a.agency_timezone FROM agency a ORDER BY a.agency_id LIMIT 1)
        AS agency_timezone
FROM feed_info f;
"""

# The ingestion audit trail. One row per attempted load, so a failed or partial
# run stays visible instead of being overwritten by the next success.
SQL_INGESTION_RUNS = """
SELECT
    run_id,
    source,
    started_at_utc,
    finished_at_utc,
    status,
    http_status,
    rows_staged,
    rows_loaded,
    rows_rejected,
    notes
FROM ingestion_run
ORDER BY run_id DESC;
"""

# The KPI header. Deliberately one statement rather than nine round-trips: the
# expensive part is the last subquery (a 1.45 M-row join to the trip calendars)
# and running it inside the same statement keeps the whole tile row on one cache
# entry.
SQL_HEADLINE_COUNTS = """
SELECT
    (SELECT COUNT(*) FROM station)      AS stations,
    (SELECT COUNT(*) FROM platform)     AS platforms,
    (SELECT COUNT(*) FROM platform WHERE has_platform_code = 1)
                                        AS numbered_platforms,
    (SELECT COUNT(*) FROM route)        AS routes,
    (SELECT COUNT(*) FROM trip)         AS trips,
    (SELECT COUNT(*) FROM service)      AS services,
    (SELECT COUNT(*) FROM service_date) AS service_dates,
    (SELECT COUNT(*) FROM stop_time)    AS stop_times,
    (SELECT COUNT(*) FROM v_departure)  AS boardable_calls,
    (SELECT SUM(tsd.operating_days)
       FROM v_departure d
       JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id)
                                        AS annual_departures
;
"""

# Q1 — the method comparison in long form, for a single grouped bar chart.
#
# The two series are reported as SHARE of their own total, not as absolute
# counts. 950 651 annualised departures and 94 323 timetable rows differ by an
# order of magnitude, so plotting both against one y-axis would flatten the
# naive series into the baseline, and giving each its own y-axis would be a
# dual-axis chart — the standard way to make two unrelated scales look
# comparable. Normalising both to "% of that method's day" puts them in the same
# unit honestly. The absolute numbers are on the same page, in the table.
SQL_Q1_METHOD_COMPARISON = """
WITH hourly AS (
    SELECT
        d.departure_hour        AS h,
        SUM(tsd.operating_days) AS annual_departures,
        COUNT(*)                AS timetabled_calls
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    GROUP BY d.departure_hour
)
SELECT
    printf('%02d:00', h) AS hour_band,
    h                    AS departure_hour,
    'Annualised departures' AS method,
    annual_departures       AS departures,
    ROUND(100.0 * annual_departures / SUM(annual_departures) OVER (), 3)
        AS pct_of_method_total
FROM hourly
UNION ALL
SELECT
    printf('%02d:00', h),
    h,
    'Naive timetable rows',
    timetabled_calls,
    ROUND(100.0 * timetabled_calls / SUM(timetabled_calls) OVER (), 3)
FROM hourly
ORDER BY departure_hour, method;
"""

# Q3 — how far past noon a "morning trip" actually runs.
#
# The Q3 page has to justify why the before-12:00 filter is applied to the
# trip's origin rather than to every call, and the honest way to do that is to
# measure it rather than to assert it with an invented example. This counts the
# morning trips that are still making boardable calls after noon: every one of
# them would be miscounted, once per afternoon station, by the naive filter.
#
# MIN(departure_secs) is used as the origin rather than joining v_trip_origin.
# GTFS times are monotonically non-decreasing along a trip and departure_secs
# preserves that (a 24:20 call is 87 600 s, not 1 200), so the earliest boardable
# departure of a trip *is* its first boardable call. That turns a windowed view
# joined back onto 1.45 M rows into one grouped scan, and the two forms were
# checked against each other: both answer 54 367 / 10 325 / 19.0 % / 15:06.
# The `< 12 * 3600` predicate also subsumes q3's `day_offset = 0`, since any
# origin published as 24:xx or later is already above 43 200 s.
SQL_Q3_MORNING_TRIP_SPAN = """
WITH trip_span AS (
    SELECT
        trip_id,
        MIN(departure_secs) AS origin_secs,
        MAX(departure_secs) AS last_secs
    FROM v_departure
    GROUP BY trip_id
)
SELECT
    COUNT(*) AS morning_trips,
    SUM(CASE WHEN last_secs >= 12 * 3600 THEN 1 ELSE 0 END)
        AS still_calling_after_noon,
    ROUND(100.0 * SUM(CASE WHEN last_secs >= 12 * 3600 THEN 1 ELSE 0 END)
          / COUNT(*), 1) AS pct_still_calling_after_noon,
    printf('%02d:%02d', MAX(last_secs) / 3600, (MAX(last_secs) % 3600) / 60)
        AS latest_call_by_a_morning_trip
FROM trip_span
WHERE origin_secs < 12 * 3600;
"""

# Q2 — station picker. Costs nothing: it never touches stop_time, only the 652
# stations and their 2 243 child stops. The dropdown label is assembled here in
# SQL rather than in Python, so the widget needs no lookup back into the result
# set — it addresses rows by position and reads the label straight off them.
SQL_STATION_PICKER = """
SELECT
    s.station_name,
    COUNT(p.stop_id) FILTER (WHERE p.has_platform_code = 1) AS numbered_platforms,
    s.station_name || ' — '
        || COUNT(p.stop_id) FILTER (WHERE p.has_platform_code = 1)
        || ' numbered platforms' AS picker_label
FROM station s
LEFT JOIN platform p ON p.station_id = s.station_id
GROUP BY s.station_id, s.station_name
ORDER BY numbered_platforms DESC, s.station_name;
"""

# Q2 — the platform ranking of sql/analysis/q2_platform_bottlenecks.sql with the
# station bound as a parameter instead of hard-coded, so the explorer can point
# at any of the 652 stations. The graded Bruxelles-Central answer on the same
# page is still run from the analysis file itself, unmodified.
SQL_STATION_PLATFORM_RANKING = """
WITH platform_load AS (
    SELECT
        d.platform_code,
        COUNT(*)                        AS timetabled_calls,
        SUM(tsd.operating_days)         AS annual_departures,
        COUNT(DISTINCT d.route_id)      AS routes_served,
        COUNT(DISTINCT d.trip_headsign) AS distinct_destinations,
        MIN(d.departure_time)           AS first_departure,
        MAX(d.departure_time)           AS last_departure
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    WHERE d.station_name = ?
      AND d.has_platform_code = 1
    GROUP BY d.platform_code
)
SELECT
    RANK() OVER (ORDER BY annual_departures DESC) AS rank_annualised,
    RANK() OVER (ORDER BY timetabled_calls DESC)  AS rank_timetabled,
    'Platform ' || platform_code AS platform,
    annual_departures,
    ROUND(100.0 * annual_departures / SUM(annual_departures) OVER (), 1)
        AS pct_of_station,
    timetabled_calls,
    routes_served,
    distinct_destinations,
    first_departure,
    last_departure
FROM platform_load
ORDER BY annual_departures DESC;
"""

# Q2 — the hour x platform grid behind the heat-map. Counted in timetable rows
# rather than annualised departures on purpose: this chart answers "when in the
# day does this platform work", which is a shape, and the shape is the same
# under either weighting while the row count runs in a fraction of the time.
SQL_STATION_HOUR_PLATFORM = """
SELECT
    d.platform_code,
    d.departure_hour,
    printf('%02d:00', d.departure_hour) AS hour_band,
    COUNT(*) AS calls,
    ROUND(100.0 * COUNT(*)
          / SUM(COUNT(*)) OVER (PARTITION BY d.platform_code), 1)
        AS pct_of_platform_day
FROM v_departure d
WHERE d.station_name = ?
  AND d.has_platform_code = 1
GROUP BY d.platform_code, d.departure_hour
-- platform_code is TEXT and is not always a number: alongside 1..21,
-- Bruxelles-Midi publishes 'TE BEPAL' (Dutch for "to be determined") as its
-- 22nd numbered platform, and other stations publish 'A', 'VARIA' or a
-- placeholder string. A plain lexical sort would file platform 2 after
-- platform 19, so numeric codes are ordered numerically and everything else
-- is pushed to the end in name order.
ORDER BY
    CASE WHEN d.platform_code GLOB '[0-9]*' THEN 0 ELSE 1 END,
    CAST(d.platform_code AS INTEGER),
    d.platform_code,
    d.departure_hour;
"""

# Q2 — completeness check for the selected station. Calls with no track
# allocated are excluded from the ranking above, so their count has to be
# visible or the platform totals will be read as the station total.
SQL_STATION_COVERAGE = """
SELECT
    ? AS station_name,
    COUNT(*) AS all_boardable_calls,
    SUM(CASE WHEN has_platform_code = 1 THEN 1 ELSE 0 END) AS calls_with_platform,
    SUM(CASE WHEN has_platform_code = 0 THEN 1 ELSE 0 END) AS calls_without_platform,
    ROUND(100.0 * SUM(CASE WHEN has_platform_code = 0 THEN 1 ELSE 0 END)
          / COUNT(*), 2) AS pct_unallocated,
    COUNT(DISTINCT route_id)      AS routes_served,
    COUNT(DISTINCT trip_headsign) AS distinct_destinations
FROM v_departure
WHERE station_name = ?;
"""

# Data quality — the evidence behind each DQ rule, counted live rather than
# transcribed into markdown. A hard-coded "12 rows quarantined" in a report goes
# stale the moment the feed changes; this does not.
SQL_DQ_EVIDENCE = """
SELECT 'DQ-01' AS rule, 'calendar.txt publishes no weekly pattern' AS finding,
       (SELECT COUNT(*) FROM service WHERE has_weekday_pattern = 0) AS rows_affected,
       (SELECT COUNT(*) FROM service) AS rows_examined,
       'Weekly frequency is derived from calendar_dates (v_service_frequency).'
           AS handling
UNION ALL
SELECT 'DQ-02', 'Accessibility code 0 means "no information", not "no"',
       (SELECT COUNT(*) FROM trip
         WHERE bikes_allowed = 0 OR wheelchair_accessible = 0),
       (SELECT COUNT(*) FROM trip),
       'Q5 counts only code 1 as an explicit guarantee.'
UNION ALL
SELECT 'DQ-03', 'Calls 48 h or more into their own service day',
       (SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-03%'),
       (SELECT COUNT(*) FROM stop_time)
         + (SELECT COUNT(*) FROM rejected_row WHERE source_table = 'stg_stop_times'),
       'Quarantined in rejected_row; never loaded into stop_time.'
UNION ALL
SELECT 'DQ-04', 'Rows referencing a parent that does not exist',
       (SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-04%'),
       (SELECT COUNT(*) FROM stop_time),
       'Quarantine rule in place; no row in this feed triggered it.'
UNION ALL
SELECT 'DQ-05', 'Duplicate primary keys',
       (SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-05%'),
       (SELECT COUNT(*) FROM stop_time),
       'Quarantine rule in place; no row in this feed triggered it.'
UNION ALL
SELECT 'DQ-06', 'Dates arrive as YYYYMMDD, with a leading space in feed_info',
       (SELECT COUNT(*) FROM feed_info
         WHERE feed_start_date LIKE '____-__-__'),
       (SELECT COUNT(*) FROM feed_info),
       'TRIM + substr into ISO YYYY-MM-DD at transform time.'
UNION ALL
SELECT 'DQ-07', 'Translations are keyed by value; record_id is empty throughout',
       (SELECT COUNT(*) FROM text_translation),
       (SELECT COUNT(*) FROM text_translation),
       'text_translation joins on field_value rather than a record id.'
UNION ALL
-- The count is deliberately platform_code rather than stop_code/stop_url/
-- zone_id: those three are blank on every row and are simply not carried into
-- the core model, so there is nothing left to count. platform_code is the one
-- place where the empty-string-to-NULL normalisation is still visible.
SELECT 'DQ-08', 'Empty string is not NULL: blank platform_code became NULL',
       (SELECT COUNT(*) FROM platform WHERE platform_code IS NULL),
       (SELECT COUNT(*) FROM platform),
       'stop_code, stop_url and zone_id are blank throughout and were dropped.'
UNION ALL
SELECT 'DQ-09', 'Trips referencing an unknown route or service',
       (SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-09%'),
       (SELECT COUNT(*) FROM trip),
       'Quarantine rule in place; no row in this feed triggered it.'
;
"""

# Data quality — two feed characteristics that are not defects but change every
# count in the report if they are missed.
SQL_FEED_CHARACTERISTICS = """
SELECT
    'Technical pass-throughs (pickup_type = 1 AND drop_off_type = 1)' AS characteristic,
    (SELECT COUNT(*) FROM stop_time WHERE is_boardable = 0 AND is_alightable = 0)
        AS calls,
    (SELECT COUNT(*) FROM stop_time) AS of_total_calls,
    'Excluded from v_departure: the train serves the platform, nobody may board.'
        AS effect
UNION ALL
SELECT
    'Calls published at 24:00:00 or later',
    (SELECT COUNT(*) FROM stop_time WHERE day_offset > 0),
    (SELECT COUNT(*) FROM stop_time),
    'Mapped to the clock hour a passenger reads, so 24:20 counts towards 00:00.'
UNION ALL
SELECT
    'Boardable calls with a published departure (v_departure)',
    (SELECT COUNT(*) FROM v_departure),
    (SELECT COUNT(*) FROM stop_time),
    'The denominator for every "timetabled calls" figure in this report.'
;
"""

# Data quality — rejected rows grouped by rule, with a worked example.
SQL_REJECTED_ROWS = """
SELECT
    r.rule_code,
    r.source_table,
    COUNT(*) AS rows_rejected,
    MIN(r.src_line_no) AS first_source_line,
    MAX(r.src_line_no) AS last_source_line,
    MIN(r.reason) AS reason
FROM rejected_row r
GROUP BY r.rule_code, r.source_table
ORDER BY rows_rejected DESC, r.rule_code;
"""

# Foreign-key integrity, live. PRAGMA foreign_key_check returns one row per
# violation, so an empty result is the pass condition.
SQL_FK_CHECK = "PRAGMA foreign_key_check;"


# ===========================================================================
# Connection and query plumbing
# ===========================================================================


class ConnectionPool:
    """One read-only connection per Streamlit script-runner thread.

    ``@st.cache_resource`` exists to keep an expensive handle alive across
    reruns, but sqlite3 refuses to let a connection cross threads and
    ``railpulse.db.connect`` deliberately does not expose ``check_same_thread``
    — turning that guard off globally would be the wrong fix for a dashboard.
    Streamlit reruns the script on whichever runner thread is free, so a single
    cached handle eventually raises ``ProgrammingError``. Caching the pool
    instead of the handle keeps connections alive across reruns without ever
    sharing one between threads.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(read_only=True)
            self._local.conn = conn
        return conn


@st.cache_resource(show_spinner=False)
def get_pool() -> ConnectionPool:
    return ConnectionPool()


@st.cache_data(show_spinner="Running SQL against railpulse.db…")
def run_sql(sql: str, params: tuple = ()) -> pd.DataFrame:
    """Execute *sql* and wrap the already-aggregated rows for rendering.

    The DataFrame is built straight from ``cursor.description`` and the fetched
    rows. Nothing is derived, joined or filtered here — see the note at the
    pandas import.
    """
    cursor = get_pool().connection().execute(sql, params)
    columns = [column[0] for column in cursor.description or ()]
    rows = [tuple(row) for row in cursor.fetchall()]
    return pd.DataFrame(rows, columns=columns)


@st.cache_data(show_spinner=False)
def sql_blocks(relative_path: str) -> dict[str, dict[str, str]]:
    """Parse an ``sql/analysis`` file into its labelled query blocks.

    The analysis files annotate every statement with ``-- @label:``,
    ``-- @title:`` and ``-- @description:``. Loading them here rather than
    retyping the SQL into this module is the point: the dashboard and the
    graded ``.sql`` deliverables cannot drift apart, because they are the same
    text.

    Statement splitting uses ``railpulse.db.iter_statements``, which asks
    SQLite's own tokenizer whether a buffer is a complete statement. Splitting
    on ``;`` would break on the first semicolon inside a comment, and these
    files are heavily commented.
    """
    path = config.ANALYSIS_SQL_DIR / relative_path
    blocks: dict[str, dict[str, str]] = {}
    for statement in iter_statements(path.read_text(encoding="utf-8")):
        label, title, description, body = _parse_block(statement)
        if label:
            blocks[label] = {"title": title, "description": description, "sql": body}
    return blocks


def _parse_block(statement: str) -> tuple[str, str, str, str]:
    """Split one annotated statement into (label, title, description, sql).

    Header comments are consumed until the first line of actual SQL; from there
    everything is body, so comments *inside* a query survive into the SQL shown
    in the ``Show the SQL`` expander instead of being stripped out of it.
    """
    label = title = ""
    description_lines: list[str] = []
    body_lines: list[str] = []
    reading_description = False
    in_body = False

    for line in statement.splitlines():
        stripped = line.strip()
        if not in_body and (stripped.startswith("--") or not stripped):
            comment = stripped[2:].strip() if stripped.startswith("--") else ""
            if comment.startswith("@label:"):
                label = comment[len("@label:") :].strip()
                reading_description = False
            elif comment.startswith("@title:"):
                title = comment[len("@title:") :].strip()
                reading_description = False
            elif comment.startswith("@description:"):
                description_lines = [comment[len("@description:") :].strip()]
                reading_description = True
            elif reading_description and comment:
                description_lines.append(comment)
            else:
                reading_description = False
            continue
        in_body = True
        body_lines.append(line)

    description = " ".join(part for part in description_lines if part)
    return label, title, description, "\n".join(body_lines).strip()


@st.cache_data(show_spinner="Running SQL against railpulse.db…")
def run_block(relative_path: str, label: str) -> pd.DataFrame:
    """Run one labelled block from an analysis file, verbatim."""
    blocks = sql_blocks(relative_path)
    if label not in blocks:
        raise KeyError(
            f"{relative_path} has no block labelled '{label}'. "
            f"Available: {', '.join(sorted(blocks)) or '(none)'}"
        )
    return run_sql(blocks[label]["sql"])


def block(relative_path: str, label: str) -> tuple[pd.DataFrame, dict[str, str]]:
    """Fetch a labelled block's result and its annotations together."""
    return run_block(relative_path, label), sql_blocks(relative_path)[label]


def show_block(
    relative_path: str,
    label: str,
    *,
    caption: bool = True,
    table: bool = True,
    height: int | None = None,
) -> pd.DataFrame:
    """Render a labelled block: its title, its own description, then its rows."""
    frame, meta = block(relative_path, label)
    if meta["title"]:
        st.markdown(f"**{meta['title']}**")
    if caption and meta["description"]:
        st.caption(meta["description"])
    if table:
        # `height` is optional here, but Streamlit >= 1.55 rejects an explicit
        # height=None rather than treating it as "use the default", so the
        # keyword has to be omitted entirely when no height was requested.
        extra = {"height": height} if height is not None else {}
        st.dataframe(frame, use_container_width=True, hide_index=True, **extra)
    with st.expander(f"Show the SQL — sql/analysis/{relative_path} · {label}"):
        st.code(meta["sql"], language="sql")
    return frame


def fmt(value: object) -> str:
    """Thousands-separated integer, matching the spacing used in the SQL files.

    A SQL NULL arrives as None or, in a column pandas typed as float, as NaN —
    an unfinished ingestion_run has both. Neither is an error worth crashing a
    KPI tile over, so both render as 'n/a'.
    """
    try:
        return f"{int(value):,}".replace(",", " ")  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "n/a"


def scalar(frame: pd.DataFrame, column: str, row: int = 0) -> object:
    """One cell out of a result set. Positional lookup, not a computation."""
    if frame.empty or column not in frame.columns:
        return None
    return frame[column].iloc[row]


# ===========================================================================
# Chart helpers
# ===========================================================================


def hour_bar(
    frame: pd.DataFrame,
    *,
    y: str,
    y_title: str,
    colour: str = BLUE,
    title: str = "",
    x: str = "hour_band",
    x_title: str = "Hour of the day (local clock time)",
) -> alt.Chart:
    """Single-series bar chart over the 24 hours of the service day.

    The x axis is sorted ascending rather than left in the result-set order.
    Several of the analysis queries deliberately return their rows ranked by
    volume — that is the right order for their table — and plotting an hour
    axis in rank order would produce a chart that looks like a distribution but
    is not one. Hour bands are zero-padded ('07:00'), so a lexical ascending
    sort is a chronological sort.
    """
    return (
        alt.Chart(frame, title=title)
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4, color=colour)
        .encode(
            x=alt.X(f"{x}:N", title=x_title, sort="ascending"),
            y=alt.Y(f"{y}:Q", title=y_title),
            tooltip=list(frame.columns),
        )
        .properties(height=280)
    )


def category_bar(
    frame: pd.DataFrame,
    *,
    x: str,
    x_title: str,
    y: str,
    y_title: str,
    colour: str = BLUE,
    title: str = "",
    height: int = 320,
) -> alt.Chart:
    """Horizontal bar chart for named categories, ordered as the SQL returned."""
    return (
        alt.Chart(frame, title=title)
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color=colour)
        .encode(
            x=alt.X(f"{x}:Q", title=x_title),
            y=alt.Y(f"{y}:N", title=y_title, sort=None),
            tooltip=list(frame.columns),
        )
        .properties(height=height)
    )


# ===========================================================================
# Guard: the database has to exist before anything else is worth doing
# ===========================================================================

st.set_page_config(
    page_title="RailPulse — Belgian transit SQL analysis",
    layout="wide",
    initial_sidebar_state="expanded",
)

if not config.DB_PATH.exists():
    st.title("RailPulse")
    st.error(
        f"No database at `{config.DB_PATH}`.\n\n"
        "The dashboard reads a pre-built SQLite file; it does not build one, and "
        "it never writes to the database.\n\n"
        "Build it first, from the repository root:\n\n"
        "```\n"
        "make build\n"
        "```\n\n"
        "or, without make:\n\n"
        "```\n"
        "python -m railpulse build\n"
        "```\n\n"
        "The full rebuild downloads the GTFS Static feed from the Belgian "
        "Mobility Open Data portal and takes a few minutes."
    )
    st.stop()


# ===========================================================================
# Pages
# ===========================================================================


def page_overview() -> None:
    st.title("RailPulse — Belgian transit SQL analysis")
    st.markdown(
        "SNCB/NMBS scheduled services, modelled from the GTFS Static feed into a "
        "normalised SQLite database. Every figure on every page of this report is "
        "the output of a SQL query; nothing is recomputed in Python."
    )

    provenance = run_sql(SQL_FEED_PROVENANCE)
    counts = run_sql(SQL_HEADLINE_COUNTS)
    runs = run_sql(SQL_INGESTION_RUNS)

    st.subheader("Data as of")
    left, right = st.columns([2, 3])
    with left:
        st.metric("Feed version", str(scalar(provenance, "feed_version")))
        st.metric(
            "Timetable window",
            f"{scalar(provenance, 'feed_start_date')} → "
            f"{scalar(provenance, 'feed_end_date')}",
            delta=f"{fmt(scalar(provenance, 'feed_window_days'))} days",
            delta_color="off",
        )
        # runs is ordered run_id DESC, so row 0 is the most recent attempt —
        # which is not necessarily the most recent *success*, hence the status.
        st.metric(
            "Latest load finished (UTC)",
            str(scalar(runs, "finished_at_utc")),
            delta=f"status: {scalar(runs, 'status')}",
            delta_color="off",
        )
    with right:
        st.dataframe(provenance, use_container_width=True, hide_index=True)
        st.caption(
            "Publisher, language and validity window come from feed_info; the "
            "operator row comes from agency. The feed is published in French "
            "(feed_lang = 'fr'), so every station and headsign name in this "
            "report is the French form — the Dutch, German and English variants "
            "live in text_translation and are shown on the Q3 page."
        )

    st.subheader("The network in numbers")
    row_one = st.columns(4)
    row_one[0].metric("Stations", fmt(scalar(counts, "stations")))
    row_one[1].metric(
        "Platforms",
        fmt(scalar(counts, "platforms")),
        delta=f"{fmt(scalar(counts, 'numbered_platforms'))} numbered",
        delta_color="off",
    )
    row_one[2].metric("Routes", fmt(scalar(counts, "routes")))
    row_one[3].metric("Trips", fmt(scalar(counts, "trips")))

    row_two = st.columns(4)
    row_two[0].metric("Service calendars", fmt(scalar(counts, "services")))
    row_two[1].metric("Service dates", fmt(scalar(counts, "service_dates")))
    row_two[2].metric("Timetabled calls", fmt(scalar(counts, "stop_times")))
    row_two[3].metric(
        "Boardable calls",
        fmt(scalar(counts, "boardable_calls")),
        help=(
            "Calls where a passenger may actually board and a departure time is "
            "published — the definition v_departure enforces. The difference "
            "against 'Timetabled calls' is technical pass-throughs and terminal "
            "arrivals."
        ),
    )

    st.metric(
        "Annual departures across the feed window",
        fmt(scalar(counts, "annual_departures")),
        help=(
            "Each boardable call multiplied by the number of dates its service "
            "actually operates. This, not the raw row count, is the number of "
            "trains that really leave a platform."
        ),
    )
    with st.expander("Show the SQL — dashboard/app.py · SQL_HEADLINE_COUNTS"):
        st.code(SQL_HEADLINE_COUNTS, language="sql")

    st.subheader("What this report answers")
    st.markdown(
        """
| Page | Question | Headline |
|---|---|---|
| Q1 | Which hour carries the most scheduled departures? | The naive and the annualised readings disagree, and the annualised one is right |
| Q2 | The three busiest platforms at Bruxelles-Central | Same three platforms under both methods, different order |
| Q3 | Top terminal destinations for trips departing before 12:00 | Anvers-Central leads on both measures |
| Q4 | Weekly frequency class per service | Derived from calendar_dates — calendar.txt is empty (DQ-01) |
| Q5 | Amenity guarantees per route | A clean mode split, and one field that is entirely unpopulated |
| Leaderboard | The five main hubs compared | Structural always; punctuality only once the poller has collected more than a sample |
| Data quality | What was cleaned, rejected and why | Nine DQ rules, counted live |
"""
    )

    st.divider()
    st.caption(
        "Source: SNCB/NMBS GTFS Static, Belgian Mobility Open Data portal "
        "(api-management-discovery-production.azure-api.net). Licence: "
        f"{config.DATA_LICENCE}. Attribution: "
        f"{config.ATTRIBUTION_TEMPLATE.format(feed_date=scalar(provenance, 'feed_version'))}."
    )


def page_q1() -> None:
    st.title("Q1 — The peak hour problem")
    st.markdown(
        "*What hour of the day experiences the highest volume of scheduled train "
        "departures across the entire network?*"
    )

    headline = run_block("q1_peak_hour.sql", "q1_peak_hour_headline")
    naive = run_block("q1_peak_hour.sql", "q1_naive_timetable_rows_by_hour")
    annualised = run_block("q1_peak_hour.sql", "q1_annualised_departures_by_hour")

    left, right = st.columns(2)
    left.metric(
        "Peak hour — annualised (the answer)",
        str(scalar(headline, "peak_hour")),
        delta=f"{fmt(scalar(headline, 'annual_departures'))} departures "
        f"({scalar(headline, 'pct_of_all_departures')}% of the network day)",
        delta_color="off",
    )
    right.metric(
        "Peak hour — naive COUNT(*)",
        str(scalar(naive, "hour_band")),
        delta=f"{fmt(scalar(naive, 'timetabled_calls'))} timetable rows",
        delta_color="off",
    )

    st.info(
        "**The two methods do not agree, and that is the finding.** The SNCB feed "
        "is a year-long timetable: a summer-Sunday excursion and a Monday-to-Friday "
        "commuter train are one row each in stop_times, but the second one departs "
        "on roughly 250 times more days. `COUNT(*)` therefore measures rows in the "
        "timetable file, not trains that depart. Weighting every call by the number "
        "of dates its service actually runs moves the answer to the evening "
        "commuter peak, which is where a rail planner would expect it."
    )

    st.subheader("The comparison")
    comparison = run_sql(SQL_Q1_METHOD_COMPARISON)
    grouped = (
        alt.Chart(comparison)
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X(
                "hour_band:N",
                title="Hour of the day (local clock time)",
                sort="ascending",
            ),
            xOffset=alt.XOffset("method:N", sort=["Annualised departures", "Naive timetable rows"]),
            y=alt.Y("pct_of_method_total:Q", title="Share of that method's total (%)"),
            color=alt.Color(
                "method:N",
                title="Method",
                sort=["Annualised departures", "Naive timetable rows"],
                scale=alt.Scale(
                    domain=["Annualised departures", "Naive timetable rows"],
                    range=[BLUE, ORANGE],
                ),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=["hour_band", "method", "departures", "pct_of_method_total"],
        )
        .properties(height=340)
    )
    st.altair_chart(grouped, use_container_width=True)
    st.caption(
        "Both series are shown as a share of their own total. The absolute counts "
        "differ by an order of magnitude (950 651 annualised departures against "
        "94 323 timetable rows in the respective peak hours), so plotting them "
        "against a shared axis would flatten the naive series and plotting them "
        "against two axes would be a dual-axis chart. Normalising is the honest "
        "way to put them in one picture; the absolute numbers are in the tables "
        "below."
    )
    with st.expander("Show the SQL — dashboard/app.py · SQL_Q1_METHOD_COMPARISON"):
        st.code(SQL_Q1_METHOD_COMPARISON, language="sql")

    st.subheader("The same data in absolute terms, on separate scales")
    left, right = st.columns(2)
    with left:
        st.altair_chart(
            hour_bar(
                annualised,
                y="annual_departures",
                y_title="Annual departures (trains)",
                colour=BLUE,
                title="Annualised — what actually departs",
            ),
            use_container_width=True,
        )
    with right:
        st.altair_chart(
            hour_bar(
                naive,
                y="timetabled_calls",
                y_title="Timetable rows (calls)",
                colour=ORANGE,
                title="Naive — rows in the timetable file",
            ),
            use_container_width=True,
        )

    st.subheader("Rank divergence")
    divergence = show_block("q1_peak_hour.sql", "q1_rank_divergence", table=False)
    st.dataframe(
        divergence,
        use_container_width=True,
        hide_index=True,
        column_config={
            "rank_improvement": st.column_config.NumberColumn(
                "rank_improvement",
                help="naive rank minus annualised rank. Positive = busier in "
                "reality than the timetable file suggests.",
            ),
            "avg_days_per_call": st.column_config.NumberColumn(
                "avg_days_per_call",
                help="Average number of dates a call in this hour actually runs.",
            ),
        },
    )
    st.altair_chart(
        alt.Chart(divergence, title="How far each hour moves between the two methods")
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("hour_band:N", title="Hour of the day", sort="ascending"),
            y=alt.Y("rank_improvement:Q", title="Naive rank − annualised rank"),
            color=alt.Color(
                "rank_improvement:Q",
                title="Rank shift",
                scale=alt.Scale(scheme="blueorange", domainMid=0),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=list(divergence.columns),
        )
        .properties(height=260),
        use_container_width=True,
    )
    st.caption(
        "Bars above the line are hours the raw timetable under-states. The "
        "colour repeats the height rather than encoding anything new, so the "
        "chart still reads in greyscale."
    )

    st.subheader("Weekday against weekend")
    daytype = show_block("q1_peak_hour.sql", "q1_peak_by_daytype", table=False)
    st.altair_chart(
        alt.Chart(daytype, title="Departures by hour and day type")
        .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
        .encode(
            x=alt.X("hour_band:N", title="Hour of the day", sort="ascending"),
            y=alt.Y("departures:Q", title="Annual departures (trains)"),
            color=alt.Color(
                "day_type:N",
                title="Day type",
                scale=alt.Scale(domain=["Weekday", "Weekend"], range=[BLUE, ORANGE]),
                legend=alt.Legend(orient="top"),
            ),
            row=alt.Row("day_type:N", title=None),
            tooltip=["day_type", "hour_band", "departures", "pct_of_day_type"],
        )
        .properties(height=200),
        use_container_width=True,
    )
    st.caption(
        "Faceted rather than overlaid: the weekend network is much smaller, and "
        "the point is the shape of each day rather than the ratio between them. "
        "The weekday profile has two commuter spikes; the weekend has none."
    )
    st.dataframe(daytype, use_container_width=True, hide_index=True, height=320)

    st.subheader("Full hourly detail")
    st.dataframe(annualised, use_container_width=True, hide_index=True, height=400)


def page_q2() -> None:
    st.title("Q2 — Platform bottlenecks")
    st.markdown("*Identify the top 3 busiest platforms in Brussels-Central.*")

    top3 = run_block("q2_platform_bottlenecks.sql", "q2_brussels_central_top3")
    columns = st.columns(3)
    for position, column in enumerate(columns):
        if position < len(top3):
            column.metric(
                f"#{int(top3['position'].iloc[position])}",
                str(top3["busiest_platform"].iloc[position]),
                delta=f"{fmt(top3['annual_departures'].iloc[position])} departures/yr",
                delta_color="off",
            )

    st.info(
        "**The top three are the same set under either method; the order inside "
        "them is not.** Ranked by annualised departures it is platforms 4, 3, 2. "
        "Ranked by raw timetable rows the first two swap: 3 then 4. Platform 4 "
        "carries fewer distinct scheduled calls, but they run on more days. The "
        "station is `gs:nmbssncb:S8813003`, published in French as "
        "Bruxelles-Central, and it has six numbered platforms."
    )

    st.subheader("The graded answer — Bruxelles-Central")
    ranking = show_block(
        "q2_platform_bottlenecks.sql", "q2_brussels_central_platform_ranking"
    )

    left, right = st.columns(2)
    with left:
        st.altair_chart(
            category_bar(
                ranking,
                x="annual_departures",
                x_title="Annual departures (trains)",
                y="platform",
                y_title="",
                colour=BLUE,
                title="Ranked by annualised departures",
                height=260,
            ),
            use_container_width=True,
        )
    with right:
        st.altair_chart(
            category_bar(
                ranking,
                x="timetabled_calls",
                x_title="Timetable rows (calls)",
                y="platform",
                y_title="",
                colour=ORANGE,
                title="Ranked by raw timetable rows",
                height=260,
            ),
            use_container_width=True,
        )
    st.caption(
        "Two separate charts on two separate scales rather than one chart with "
        "two axes. Read them as a pair: the bars are ordered by the annualised "
        "ranking in both, so the orange chart being out of order is the "
        "disagreement between the methods."
    )

    show_block("q2_platform_bottlenecks.sql", "q2_unallocated_platform_calls")
    show_block("q2_platform_bottlenecks.sql", "q2_platform_peak_pressure")

    st.divider()
    st.subheader("Inspect any station")
    st.caption(
        "The same ranking query with the station bound as a parameter instead of "
        "hard-coded. Stations are listed by numbered-platform count; names are the "
        "French forms the feed publishes. Note that a platform code is text and is "
        "not always a number — Bruxelles-Midi's 22nd is published as 'TE BEPAL' "
        "(Dutch for \"to be determined\"), and other stations use 'A' or 'VARIA'. "
        "These are shown as the feed publishes them rather than being quietly "
        "merged into the unallocated bucket."
    )

    stations = run_sql(SQL_STATION_PICKER)
    # The widget carries row positions, not values, and the label comes from the
    # picker_label column the query already built. Looking a station up with a
    # boolean mask would be filtering in pandas, which this project does not do.
    station_names = list(stations["station_name"])
    default_index = (
        station_names.index(config.FOCUS_STATION)
        if config.FOCUS_STATION in station_names
        else 0
    )
    position = st.selectbox(
        "Station",
        range(len(stations)),
        index=default_index,
        format_func=lambda index: str(stations["picker_label"].iloc[index]),
    )
    selected = str(stations["station_name"].iloc[position])

    coverage = run_sql(SQL_STATION_COVERAGE, (selected, selected))
    metrics = st.columns(4)
    metrics[0].metric("Boardable calls", fmt(scalar(coverage, "all_boardable_calls")))
    metrics[1].metric(
        "With a platform allocated", fmt(scalar(coverage, "calls_with_platform"))
    )
    metrics[2].metric(
        "No platform allocated",
        fmt(scalar(coverage, "calls_without_platform")),
        delta=f"{scalar(coverage, 'pct_unallocated')}% of the station",
        delta_color="off",
        help=(
            "Every station in this feed has exactly one child stop with a NULL "
            "platform_code, used when no track has been allocated. These calls "
            "are real departures and are excluded from the platform ranking, so "
            "the platform totals do not add up to the station total."
        ),
    )
    metrics[3].metric(
        "Distinct destinations", fmt(scalar(coverage, "distinct_destinations"))
    )

    station_ranking = run_sql(SQL_STATION_PLATFORM_RANKING, (selected,))
    if station_ranking.empty:
        st.warning(
            f"{selected} has no numbered platforms in this feed — every call "
            "there sits on the NULL-platform child stop, so there is nothing to "
            "rank."
        )
    else:
        st.dataframe(station_ranking, use_container_width=True, hide_index=True)
        st.altair_chart(
            category_bar(
                station_ranking,
                x="annual_departures",
                x_title="Annual departures (trains)",
                y="platform",
                y_title="",
                colour=BLUE,
                title=f"{selected} — platforms by annualised departures",
                height=max(200, 34 * len(station_ranking)),
            ),
            use_container_width=True,
        )

        grid = run_sql(SQL_STATION_HOUR_PLATFORM, (selected,))
        st.subheader(f"{selected} — hour × platform congestion")
        st.altair_chart(
            alt.Chart(grid)
            .mark_rect(stroke="#ffffff", strokeWidth=1)
            .encode(
                x=alt.X(
                    "hour_band:N",
                    title="Hour of the day (local clock time)",
                    sort="ascending",
                ),
                # sort=None keeps the numeric-aware platform order the query
                # already imposed; an Altair sort here would go back to lexical.
                y=alt.Y("platform_code:N", title="Platform", sort=None),
                color=alt.Color(
                    "calls:Q",
                    title="Timetable rows",
                    scale=alt.Scale(range=SEQUENTIAL_BLUE),
                    legend=alt.Legend(orient="right"),
                ),
                tooltip=["platform_code", "hour_band", "calls", "pct_of_platform_day"],
            )
            # One row per numbered platform, and station_ranking already has
            # exactly that many rows — no need to count distinct values again.
            .properties(height=max(180, 30 * len(station_ranking))),
            use_container_width=True,
        )
        st.caption(
            "One sequential blue ramp, light to dark — a magnitude, so a single "
            "hue rather than a rainbow. Empty cells are hours in which the "
            "platform has no scheduled boarding call at all."
        )
        with st.expander("Show the SQL — dashboard/app.py · station explorer"):
            st.code(SQL_STATION_COVERAGE, language="sql")
            st.code(SQL_STATION_PLATFORM_RANKING, language="sql")
            st.code(SQL_STATION_HOUR_PLATFORM, language="sql")

    st.divider()
    show_block("q2_platform_bottlenecks.sql", "q2_hub_comparison")


def page_q3() -> None:
    st.title("Q3 — Busiest morning destinations")
    st.markdown(
        "*Find the top 3 most frequent terminal destinations (trip_headsign) for "
        "all morning trips that depart before 12:00:00.*"
    )

    top3 = run_block("q3_morning_destinations.sql", "q3_top3_destinations")
    columns = st.columns(3)
    for position, column in enumerate(columns):
        if position < len(top3):
            column.metric(
                f"#{int(top3['position'].iloc[position])}",
                str(top3["terminal_destination"].iloc[position]),
                delta=f"{fmt(top3['annual_morning_trips'].iloc[position])} trips/yr",
                delta_color="off",
            )

    span = run_sql(SQL_Q3_MORNING_TRIP_SPAN)
    st.info(
        "**\"Departs before 12:00\" is a statement about the trip, not about each "
        "of its calls.** A long cross-country service that leaves its origin in "
        "the morning is still setting down and picking up well into the "
        f"afternoon: {fmt(scalar(span, 'still_calling_after_noon'))} of the "
        f"{fmt(scalar(span, 'morning_trips'))} morning trips counted here "
        f"({scalar(span, 'pct_still_calling_after_noon')}%) are still making "
        "boardable calls after noon, the latest at "
        f"{scalar(span, 'latest_call_by_a_morning_trip')}. Filtering on every "
        "call instead of on the origin would count each of those trips once per "
        "station it happens to leave in the morning, and would quietly "
        "reclassify long afternoon services as morning ones. The filter is "
        "therefore applied to the trip's origin — the first boardable call, "
        "picked out by v_trip_origin.\n\n"
        "Annualised and raw counts agree on Anvers-Central and disagree below "
        "it: Bruxelles-Midi is second on raw trip count because many distinct "
        "morning services terminate there, but they run on few days each."
    )
    with st.expander("Show the SQL — dashboard/app.py · SQL_Q3_MORNING_TRIP_SPAN"):
        st.code(SQL_Q3_MORNING_TRIP_SPAN, language="sql")

    st.subheader("Ranked destinations")
    ranked = show_block(
        "q3_morning_destinations.sql", "q3_morning_destinations_ranked", table=False
    )
    st.dataframe(ranked, use_container_width=True, hide_index=True, height=420)
    left, right = st.columns(2)
    with left:
        st.altair_chart(
            category_bar(
                ranked,
                x="annual_morning_trips",
                x_title="Annual morning trips",
                y="terminal_destination",
                y_title="",
                colour=BLUE,
                title="By annualised trips",
                height=560,
            ),
            use_container_width=True,
        )
    with right:
        st.altair_chart(
            category_bar(
                ranked,
                x="morning_trips",
                x_title="Distinct morning services (timetable rows)",
                y="terminal_destination",
                y_title="",
                colour=ORANGE,
                title="By raw trip count",
                height=560,
            ),
            use_container_width=True,
        )
    st.caption(
        "Both charts keep the annualised ordering on the y-axis, so a bar that "
        "sticks out in the orange chart is a destination the raw count "
        "over-states."
    )

    st.subheader("When the morning peak actually is")
    profile = show_block(
        "q3_morning_destinations.sql", "q3_morning_departure_profile", table=False
    )
    st.altair_chart(
        hour_bar(
            profile,
            x="origin_hour",
            y="annual_trips",
            y_title="Annual trips starting in this hour",
            x_title="Origin hour (local clock time)",
            colour=BLUE,
            title="Morning trips by the hour their origin departs",
        ),
        use_container_width=True,
    )
    st.dataframe(profile, use_container_width=True, hide_index=True)

    st.subheader("Which destinations are genuinely morning-skewed")
    show_block("q3_morning_destinations.sql", "q3_morning_vs_afternoon")

    st.subheader("The same destinations in every published language")
    show_block("q3_morning_destinations.sql", "q3_morning_destinations_multilingual")


def page_q4() -> None:
    st.title("Q4 — Service frequency")
    st.markdown(
        "*Classify each active service ID into a weekly frequency category. "
        "5 or more days a week → High Frequency; 2–4 days → Medium Frequency; "
        "1 day or completely irregular → Low Frequency/Special.*"
    )

    classification = run_block("q4_service_frequency.sql", "q4_frequency_classification")
    columns = st.columns(len(classification) if len(classification) else 1)
    for position, column in enumerate(columns):
        if position < len(classification):
            column.metric(
                str(classification["frequency_class"].iloc[position]),
                f"{classification['pct_of_services'].iloc[position]}%",
                delta=f"{fmt(classification['services'].iloc[position])} services",
                delta_color="off",
            )

    st.warning(
        "**The obvious implementation returns the wrong answer on this feed.** "
        "`CASE WHEN monday + tuesday + … + sunday >= 5` against calendar.txt "
        "classifies every one of the 51 593 services as Low Frequency/Special, "
        "because SNCB publishes all seven weekday flags as 0 and expresses the "
        "entire operating pattern through calendar_dates.txt instead — 4 697 139 "
        "explicit dates, every one of them exception_type = 1. That is rule "
        "DQ-01. The weekly rhythm below is therefore *derived*, using the modal "
        "number of operating days across the weeks in which each service runs at "
        "all."
    )

    st.subheader("Evidence for DQ-01")
    show_block("q4_service_frequency.sql", "q4_calendar_txt_is_empty")

    st.subheader("The classification")
    st.dataframe(classification, use_container_width=True, hide_index=True)
    st.altair_chart(
        alt.Chart(classification, title="Share of services in each frequency class")
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            x=alt.X("pct_of_services:Q", title="Share of the 51 593 services (%)"),
            y=alt.Y("frequency_class:N", title="", sort=None),
            color=alt.Color(
                "frequency_class:N",
                title="Frequency class",
                scale=alt.Scale(
                    domain=FREQUENCY_CLASS_ORDER, range=FREQUENCY_CLASS_COLOURS
                ),
                legend=None,
            ),
            tooltip=list(classification.columns),
        )
        .properties(height=180),
        use_container_width=True,
    )
    st.caption(
        "The class name is on the axis, so colour is decoration here rather than "
        "the only carrier of identity — the chart reads with the colour removed."
    )
    with st.expander(
        "Show the SQL — sql/analysis/q4_service_frequency.sql · q4_frequency_classification"
    ):
        st.code(
            sql_blocks("q4_service_frequency.sql")["q4_frequency_classification"]["sql"],
            language="sql",
        )

    st.subheader("The shape underneath the three classes")
    distribution = show_block(
        "q4_service_frequency.sql", "q4_days_per_week_distribution", table=False
    )
    st.altair_chart(
        alt.Chart(distribution, title="Services by typical operating days per week")
        .mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
        .encode(
            x=alt.X(
                "typical_days_per_week:O",
                title="Typical operating days per active week",
            ),
            y=alt.Y("services:Q", title="Services"),
            color=alt.Color(
                "frequency_class:N",
                title="Frequency class",
                scale=alt.Scale(
                    domain=FREQUENCY_CLASS_ORDER, range=FREQUENCY_CLASS_COLOURS
                ),
                legend=alt.Legend(orient="top"),
            ),
            tooltip=list(distribution.columns),
        )
        .properties(height=320),
        use_container_width=True,
    )
    st.dataframe(distribution, use_container_width=True, hide_index=True)
    st.caption(
        "Two bars carry the network, and they are not the two a reader expects: "
        "5 days (16 541 services, the Monday-to-Friday commuter tier) and "
        "2 days (14 215). Only 6 420 services run all seven days. The 2-day "
        "tier is the weekend one but not purely so — 407 617 of its 480 828 "
        "operating dates fall on a Saturday or a Sunday (84.8%), the remaining "
        "15.2% on weekdays."
    )

    st.subheader("The same classes weighted by the trips that use them")
    show_block("q4_service_frequency.sql", "q4_frequency_by_trips")
    st.caption(
        "A service is a calendar, not a train. 20% of *services* being Low "
        "Frequency/Special does not mean 20% of the timetable is."
    )

    st.subheader("How much the answer depends on the definition")
    sensitivity = show_block(
        "q4_service_frequency.sql", "q4_definition_sensitivity", table=False
    )
    st.altair_chart(
        alt.Chart(sensitivity, title="Same brief, three defensible readings")
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            x=alt.X("pct_of_services:Q", title="Share of services (%)"),
            y=alt.Y("frequency_class:N", title="", sort=FREQUENCY_CLASS_ORDER),
            color=alt.Color(
                "frequency_class:N",
                title="Frequency class",
                scale=alt.Scale(
                    domain=FREQUENCY_CLASS_ORDER, range=FREQUENCY_CLASS_COLOURS
                ),
                legend=alt.Legend(orient="top"),
            ),
            row=alt.Row("definition:N", title=None, header=alt.Header(labelAnchor="start")),
            tooltip=list(sensitivity.columns),
        )
        .properties(height=110),
        use_container_width=True,
    )
    st.dataframe(sensitivity, use_container_width=True, hide_index=True, height=340)
    st.caption(
        "Publishing this is the difference between a number and a defensible "
        "number: the headline split is a modelling choice, and this is how big a "
        "choice it is."
    )

    st.subheader("Sanity check — which weekdays the network runs on")
    show_block("q4_service_frequency.sql", "q4_weekday_coverage")


def page_q5() -> None:
    st.title("Q5 — The accessibility audit")
    st.markdown(
        "*Calculate the exact ratio and percentage of scheduled trips per route "
        "that explicitly guarantee wheelchair accessibility or bicycle storage. "
        "Which specific routes score the lowest?*"
    )

    coverage = run_block("q5_accessibility_audit.sql", "q5_network_amenity_coverage")
    by_mode = run_block("q5_accessibility_audit.sql", "q5_amenity_by_mode")

    st.error(
        "**wheelchair_accessible is unpopulated for all 134 809 trips.**\n\n"
        "Every trip in this feed carries GTFS code 0 on `wheelchair_accessible`, "
        "and code 0 means **\"no information\"** — it is a silence, not a refusal. "
        "The station-level companion field, `stops.wheelchair_boarding`, is "
        "equally empty across all 652 stations.\n\n"
        "**No conclusion about wheelchair accessibility can be drawn from this "
        "data, in either direction.** Reporting 0% as \"not accessible\" would "
        "publish a statement about SNCB's fleet that the feed does not support. "
        "The gap itself is the finding, and it is a fixable publishing problem "
        "rather than a fleet problem. Every ratio on this page is computed "
        "strictly against code 1."
    )

    columns = st.columns(3)
    if not coverage.empty:
        columns[0].metric(
            "Bicycle storage guaranteed",
            f"{coverage['pct_guaranteed'].iloc[0]}%",
            delta=str(coverage["ratio"].iloc[0]),
            delta_color="off",
        )
        columns[1].metric(
            "Wheelchair access guaranteed",
            f"{coverage['pct_guaranteed'].iloc[1]}%",
            delta=f"{str(coverage['ratio'].iloc[1])} — field unpopulated",
            delta_color="off",
        )
        columns[2].metric(
            "No information on bikes",
            fmt(coverage["trips_no_information"].iloc[0]),
            delta="all of them replacement buses",
            delta_color="off",
        )

    st.subheader("Network-wide coverage")
    show_block("q5_accessibility_audit.sql", "q5_network_amenity_coverage")

    st.subheader("The finding — a mode split, not a route split")
    st.info(
        "Bicycle provision divides perfectly by mode: **100.0% of the 123 051 "
        "Rail trips** guarantee bike storage and **0.0% of the 11 758 "
        "rail-replacement Bus trips** do. Nothing in between. The lowest-scoring "
        "routes are therefore not a scattered set of underperformers to chase "
        "individually — they are one coherent operational category, and the "
        "recommendation that follows is completely different from the one a "
        "route-by-route table on its own would suggest."
    )
    st.dataframe(by_mode, use_container_width=True, hide_index=True)
    st.altair_chart(
        alt.Chart(by_mode, title="Bicycle storage guaranteed, by mode")
        .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
        .encode(
            x=alt.X(
                "pct_bikes:Q",
                title="Trips guaranteeing bicycle storage (%)",
                scale=alt.Scale(domain=[0, 100]),
            ),
            y=alt.Y("mode:N", title="", sort=None),
            color=alt.Color(
                "mode:N",
                title="Mode",
                # Domain stated explicitly: without it Altair sorts the nominal
                # values alphabetically and hands Bus the blue, which inverts the
                # blue-is-the-good-case convention the rest of the report uses.
                # The mode is also on the y axis, so colour is redundant here
                # rather than load-bearing, and the legend is off.
                scale=alt.Scale(domain=["Rail", "Bus"], range=[BLUE, ORANGE]),
                legend=None,
            ),
            tooltip=list(by_mode.columns),
        )
        .properties(height=140),
        use_container_width=True,
    )
    with st.expander(
        "Show the SQL — sql/analysis/q5_accessibility_audit.sql · q5_amenity_by_mode"
    ):
        st.code(
            sql_blocks("q5_accessibility_audit.sql")["q5_amenity_by_mode"]["sql"],
            language="sql",
        )

    st.subheader("Every route scoring zero, in one line")
    show_block("q5_accessibility_audit.sql", "q5_worst_routes_all_sizes")

    st.subheader("The lowest-scoring routes, ranked by passenger exposure")
    st.caption(
        "All 270 zero-scoring routes score exactly 0%, so ranking them by "
        "percentage is meaningless. The actionable ordering is by exposure: "
        "trips multiplied by the days they actually run."
    )
    exposure = show_block(
        "q5_accessibility_audit.sql", "q5_worst_routes_by_passenger_exposure", table=False
    )
    st.dataframe(exposure, use_container_width=True, hide_index=True, height=420)
    st.altair_chart(
        category_bar(
            exposure,
            x="annual_trips",
            x_title="Annual trips on a route with no amenity guarantee",
            y="route_long_name",
            y_title="",
            colour=ORANGE,
            title="Replacement-bus corridors to fix first",
            height=440,
        ),
        use_container_width=True,
    )

    st.subheader("Amenity ratio per route, worst first")
    show_block("q5_accessibility_audit.sql", "q5_route_amenity_ratios", height=420)

    st.subheader("By commercial route category")
    show_block("q5_accessibility_audit.sql", "q5_amenity_by_route_category", height=380)

    st.subheader("The second unpopulated accessibility field")
    show_block("q5_accessibility_audit.sql", "q5_station_accessibility_gap")


def page_leaderboard() -> None:
    st.title("Network leaderboard — the five main hubs")
    st.markdown(
        "*Create a visual leaderboard comparing these 5 main hubs. Which city has "
        "the most efficient, on-time station?*"
    )
    st.info(
        "**The static timetable cannot answer \"on time\".** A plan is by "
        "definition never late. This page is therefore in two halves: a "
        "structural leaderboard built from the static feed, which is always "
        "available and is what a scheduler actually optimises, and a punctuality "
        "leaderboard built from accumulated GTFS-Realtime snapshots, which only "
        "becomes meaningful once the poller has been running. The hubs compared "
        f"are {', '.join(config.MAIN_HUBS)}. That shortlist is written down "
        "twice: as `MAIN_HUBS` in `src/railpulse/config.py`, which is what this "
        "sentence prints, and as five string literals in "
        "`sql/analysis/q6_network_leaderboard.sql`, which is what the tables "
        "below actually query. SQLite cannot read a Python constant, so the two "
        "have to be edited together — they agree today, and nothing enforces it."
    )

    st.subheader("Part A — structural leaderboard")
    structural = show_block(
        "q6_network_leaderboard.sql", "q6_hub_structural_leaderboard", table=False
    )
    st.dataframe(structural, use_container_width=True, hide_index=True)

    columns = st.columns(3)
    with columns[0]:
        st.altair_chart(
            category_bar(
                structural,
                x="annual_departures",
                x_title="Annual departures (trains)",
                y="station_name",
                y_title="",
                colour=BLUE,
                title="Load",
                height=220,
            ),
            use_container_width=True,
        )
    with columns[1]:
        st.altair_chart(
            category_bar(
                structural,
                x="calls_per_platform",
                x_title="Timetable rows per numbered platform",
                y="station_name",
                y_title="",
                colour=ORANGE,
                title="Platform pressure",
                height=220,
            ),
            use_container_width=True,
        )
    with columns[2]:
        st.altair_chart(
            category_bar(
                structural,
                x="peak_concentration_pct",
                x_title="Share of the day in the busiest hour (%)",
                y="station_name",
                y_title="",
                colour=AQUA,
                title="Peak concentration",
                height=220,
            ),
            use_container_width=True,
        )
    st.caption(
        "Three separate charts, three separate units — load, crowding and "
        "spikiness are not comparable quantities and are not forced onto one "
        "axis. All three keep the load ordering on the y-axis so the rows line "
        "up across the charts. Lower is better in the middle and right charts."
    )

    st.subheader("Composite structural score")
    composite = show_block(
        "q6_network_leaderboard.sql", "q6_hub_composite_score", table=False
    )
    st.altair_chart(
        category_bar(
            composite,
            x="composite_score",
            x_title="Composite structural score (0–100, mean of three components)",
            y="station_name",
            y_title="",
            colour=BLUE,
            title="Leaderboard position",
            height=220,
        ),
        use_container_width=True,
    )
    st.dataframe(composite, use_container_width=True, hide_index=True)
    st.caption(
        "This score is a presentation device, not a physical measurement. The "
        "three normalised components are published beside it precisely so a "
        "reader can disagree with the weighting and recompute."
    )

    st.subheader("Hub load by hour")
    shape = run_block("q6_network_leaderboard.sql", "q6_hub_hourly_shape")
    st.altair_chart(
        alt.Chart(shape, title="Share of each hub's day, by hour")
        .mark_rect(stroke="#ffffff", strokeWidth=1)
        .encode(
            x=alt.X(
                "hour_band:N",
                title="Hour of the day (local clock time)",
                sort="ascending",
            ),
            y=alt.Y("station_name:N", title="", sort=None),
            color=alt.Color(
                "pct_of_hub_day:Q",
                title="% of hub day",
                scale=alt.Scale(range=SEQUENTIAL_BLUE),
                legend=alt.Legend(orient="right"),
            ),
            tooltip=["station_name", "hour_band", "calls", "pct_of_hub_day"],
        )
        .properties(height=200),
        use_container_width=True,
    )
    st.caption(
        "Shares rather than absolute calls, so a small hub's shape is legible "
        "next to a large one. Two hubs with identical daily totals behave "
        "completely differently if one is flat and the other spikes."
    )

    st.divider()
    st.subheader("Part B — punctuality (real-time)")
    coverage = run_block("q6_network_leaderboard.sql", "q6_realtime_coverage")
    st.dataframe(coverage, use_container_width=True, hide_index=True)

    readings = scalar(coverage, "calls_with_a_delay_reading")
    snapshots = scalar(coverage, "trip_update_snapshots")
    if not readings:
        st.warning(
            "No real-time delay readings have been collected yet. Start the "
            "poller (`scripts/poll_realtime.sh`, or `make poll`, or "
            "`python -m railpulse poll`) and this section fills in. Until then the "
            "punctuality question stays unanswered rather than being answered "
            "from the timetable, which would just report 100% on time."
        )
        return

    st.warning(
        f"This verdict rests on {fmt(snapshots)} trip-update snapshots and "
        f"{fmt(readings)} departures with a delay reading. That is a sample, not "
        "a season. A punctuality leaderboard built on a handful of snapshots is "
        "an anecdote; the numbers below are shown so the pipeline can be seen "
        "working end to end, and they should not be quoted as SNCB's punctuality."
    )

    punctuality = run_block("q6_network_leaderboard.sql", "q6_hub_punctuality")
    if punctuality.empty:
        st.info(
            "None of the five hubs has an observed departure in the snapshots "
            "collected so far."
        )
    else:
        st.dataframe(punctuality, use_container_width=True, hide_index=True)
        st.altair_chart(
            category_bar(
                punctuality,
                x="on_time_pct",
                x_title="Departures under 120 s late (%)",
                y="station_name",
                y_title="",
                colour=BLUE,
                title="On-time rate by hub",
                height=220,
            ),
            use_container_width=True,
        )

    st.subheader("Delay distribution")
    bands = show_block("q6_network_leaderboard.sql", "q6_delay_distribution", table=False)
    st.altair_chart(
        category_bar(
            bands,
            x="calls",
            x_title="Observed departures",
            y="delay_band",
            y_title="",
            colour=ORANGE,
            title="Where the delay actually sits",
            height=260,
        ),
        use_container_width=True,
    )
    st.dataframe(bands, use_container_width=True, hide_index=True)
    st.caption(
        "An average delay hides everything that matters. A network where 95% of "
        "trains are punctual and 5% are an hour late has the same mean as one "
        "where every train is three minutes late, and they are completely "
        "different railways to travel on."
    )

    st.subheader("Punctuality across every observed station")
    show_block("q6_network_leaderboard.sql", "q6_punctuality_all_stations", height=420)

    st.subheader("Service alerts in the latest snapshot")
    show_block("q6_network_leaderboard.sql", "q6_active_service_alerts", height=320)


def page_data_quality() -> None:
    st.title("Data quality")
    st.markdown(
        "Nine cleaning rules run between the raw staging tables and the "
        "normalised core model, all of them in `sql/03_transform.sql`. Their "
        "counts below are queried live rather than transcribed, so they cannot go "
        "stale when the feed changes."
    )

    runs = run_sql(SQL_INGESTION_RUNS)
    if not runs.empty:
        columns = st.columns(4)
        columns[0].metric("Rows staged", fmt(scalar(runs, "rows_staged")))
        columns[1].metric("Rows loaded", fmt(scalar(runs, "rows_loaded")))
        columns[2].metric("Rows rejected", fmt(scalar(runs, "rows_rejected")))
        columns[3].metric("Last run status", str(scalar(runs, "status")))

    st.subheader("The DQ rules and their evidence")
    evidence = run_sql(SQL_DQ_EVIDENCE)
    st.dataframe(
        evidence,
        use_container_width=True,
        hide_index=True,
        column_config={
            "rows_affected": st.column_config.NumberColumn(
                "rows_affected", help="Rows this rule actually matched in this feed."
            ),
            "rows_examined": st.column_config.NumberColumn(
                "rows_examined", help="Rows the rule was evaluated against."
            ),
        },
    )
    st.caption(
        "DQ-04, DQ-05 and DQ-09 report zero. That is not a rule that was never "
        "written — it is a quarantine that is in place and that this particular "
        "feed did not trigger. A referential-integrity guard is worth having "
        "before it fires, not after."
    )
    with st.expander("Show the SQL — dashboard/app.py · SQL_DQ_EVIDENCE"):
        st.code(SQL_DQ_EVIDENCE, language="sql")

    st.subheader("Quarantined rows")
    rejected = run_sql(SQL_REJECTED_ROWS)
    if rejected.empty:
        st.success("No rows were quarantined in this build.")
    else:
        st.dataframe(rejected, use_container_width=True, hide_index=True)
        st.caption(
            "Rejected rows are kept, not dropped: `rejected_row` stores the "
            "source line, the rule that caught it and a JSON snapshot of the "
            "offending record, so a rule can be argued with after the fact."
        )
    with st.expander("Show the SQL — dashboard/app.py · SQL_REJECTED_ROWS"):
        st.code(SQL_REJECTED_ROWS, language="sql")

    st.subheader("Feed characteristics that change every count")
    characteristics = run_sql(SQL_FEED_CHARACTERISTICS)
    st.dataframe(characteristics, use_container_width=True, hide_index=True)
    st.caption(
        "None of these is a defect, and all three would silently distort the "
        "report if they were missed. The pass-throughs are 26.7% of every call "
        "in the timetable, but they are not spread evenly, which is what makes "
        "them dangerous: they pile up where trains run through without a "
        "commercial stop (Bruxelles-Chapelle 23 386, Bruxelles-Congrès 23 095, "
        "Schaerbeek 12 836) and are almost absent from the five leaderboard "
        "hubs (Bruxelles-Central 44, Bruxelles-Midi 9, Bruxelles-Nord 3, "
        "Anvers-Central and Gand-Saint-Pierre none). Counting them would not "
        "inflate the network uniformly — it would invent traffic at precisely "
        "the stations that have none."
    )
    with st.expander("Show the SQL — dashboard/app.py · SQL_FEED_CHARACTERISTICS"):
        st.code(SQL_FEED_CHARACTERISTICS, language="sql")

    st.subheader("Referential integrity, checked now")
    violations = run_sql(SQL_FK_CHECK)
    if violations.empty:
        st.success(
            "`PRAGMA foreign_key_check` returns no rows: every foreign key in the "
            "core model resolves."
        )
    else:
        st.error("`PRAGMA foreign_key_check` found violations:")
        st.dataframe(violations, use_container_width=True, hide_index=True)

    st.subheader("Ingestion history")
    st.dataframe(runs, use_container_width=True, hide_index=True)
    st.caption(
        "One row per attempted load. A failed or partial run stays visible "
        "instead of being overwritten by the next success."
    )


# ===========================================================================
# Navigation
# ===========================================================================

PAGES = {
    "Overview": page_overview,
    "Q1 · Peak hour": page_q1,
    "Q2 · Platform bottlenecks": page_q2,
    "Q3 · Morning destinations": page_q3,
    "Q4 · Service frequency": page_q4,
    "Q5 · Accessibility audit": page_q5,
    "Leaderboard · Five main hubs": page_leaderboard,
    "Data quality": page_data_quality,
    "SQL Chat · Ask the timetable": page_sql_chat,
}

st.sidebar.title("RailPulse")
st.sidebar.caption("Belgian transit SQL analysis · Sprint 1")
choice = st.sidebar.radio("Page", list(PAGES), label_visibility="collapsed")
st.sidebar.divider()
st.sidebar.markdown(
    "**Every number here comes from SQL.** The dashboard loads the labelled "
    "query blocks out of `sql/analysis/*.sql` and runs them unchanged; pandas "
    "only carries the already-aggregated rows to the chart layer."
)
st.sidebar.markdown(f"Database: `{config.DB_PATH.name}` · opened read-only")
st.sidebar.divider()
st.sidebar.caption(
    "Source: SNCB/NMBS GTFS Static via the Belgian Mobility Open Data portal. "
    f"Licence: {config.DATA_LICENCE}."
)

PAGES[choice]()
