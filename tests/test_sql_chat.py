"""The SQL Chat text-to-SQL guardrail and execution caps.

These test the *safety* half of the feature — the part that has to hold even
when the model emits something dangerous or ruinously expensive. The model
itself is not exercised (it needs torch + transformers, the optional `chat`
extra); everything here is pure functions plus a read-only query against the
built database.

Each test corresponds to a defect found in review:
  * the guardrail used to be a first-word check that a stacked statement slipped
    through;
  * execution used an unbounded `fetchall()` with no timeout, so a cartesian
    join could hang the dashboard.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# The SQL Chat modules import `streamlit` at module load. It is not a test
# dependency, so stub the two decorators we touch before importing them.
if "streamlit" not in sys.modules:
    _st = types.ModuleType("streamlit")
    _st.cache_resource = lambda **_k: (lambda f: f)
    _st.cache_data = lambda **_k: (lambda f: f)
    sys.modules["streamlit"] = _st

# The engine lives under dashboard/, which is not a package on sys.path by
# default; add the repo root so `import dashboard.text2sql_engine` resolves.
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dashboard.text2sql_engine import (  # noqa: E402
    MAX_RESULT_ROWS,
    QueryTimeout,
    _clean_sql,
    _is_safe_sql,
    build_prompt,
    execute_readonly_capped,
)

DB = _REPO / "data" / "railpulse.db"
needs_db = pytest.mark.skipif(not DB.exists(), reason="built database not present")


# ---------------------------------------------------------------------------
# The guardrail: only single read-only SELECT/WITH statements survive
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT * FROM station",
    "select station_name from station where station_name = 'Bruxelles-Midi'",
    "WITH x AS (SELECT 1 AS n) SELECT n FROM x",
    "SELECT COUNT(*) FROM v_departure WHERE departure_hour = 17",
    "SELECT * FROM station WHERE station_name = 'L''Isle'",   # doubled-quote escape
    "SELECT * FROM route WHERE route_short_name = 'DROP'",     # keyword in a literal
])
def test_read_only_selects_are_allowed(sql):
    assert _is_safe_sql(_clean_sql(sql)) is True


@pytest.mark.parametrize("sql", [
    "DROP TABLE station",
    "DELETE FROM trip",
    "UPDATE trip SET route_id = 'x'",
    "INSERT INTO station VALUES ('x','y',0,0,0)",
    "ALTER TABLE station ADD COLUMN hacked TEXT",
    "PRAGMA foreign_keys = OFF",
    "ATTACH DATABASE '/etc/passwd' AS pw",
    "VACUUM",
    "",
    "   ",
])
def test_non_select_statements_are_rejected(sql):
    assert _is_safe_sql(_clean_sql(sql)) is False


@pytest.mark.parametrize("sql", [
    "SELECT 1; DROP TABLE station",                 # stacked
    "SELECT 1 -- harmless?\nDROP TABLE station",    # comment-hidden verb + stack
    "SELECT 1 /* c */ ; DELETE FROM trip",          # block comment + stack
    "SELECT (SELECT 1); INSERT INTO x VALUES (1)",  # stacked after a subquery
    "'DROP' SELECT 1",                              # leading literal can't disguise it
])
def test_stacked_and_hidden_destructive_statements_are_rejected(sql):
    """The old first-word check passed several of these; the whole-statement
    scan must not."""
    assert _is_safe_sql(_clean_sql(sql)) is False


# ---------------------------------------------------------------------------
# Prompt building: PROSE_SCHEMA is genuinely reachable (was dead code)
# ---------------------------------------------------------------------------
def test_compact_prompt_uses_the_terse_schema():
    prompt = build_prompt("how many stations",
                          {"station": ["station_id", "station_name"]},
                          schema_mode="compact")
    assert "station(station_id,station_name)" in prompt
    assert "CRITICAL RULES" not in prompt


def test_rich_prompt_injects_the_full_schema_and_rules():
    prompt = build_prompt("how many stations", None, schema_mode="rich")
    assert "CRITICAL RULES" in prompt
    assert "v_departure" in prompt      # the semantic guidance is actually sent


# ---------------------------------------------------------------------------
# Execution caps: the DoS fix
# ---------------------------------------------------------------------------
@needs_db
def test_a_normal_query_runs_and_is_not_truncated():
    columns, rows, truncated = execute_readonly_capped("SELECT COUNT(*) FROM station")
    assert columns == ["COUNT(*)"]
    assert rows == [(652,)]
    assert truncated is False


@needs_db
def test_unbounded_select_is_row_capped():
    columns, rows, truncated = execute_readonly_capped(
        "SELECT * FROM stop_time", max_rows=50
    )
    assert len(rows) == 50
    assert truncated is True


@needs_db
def test_cartesian_join_is_cancelled_by_the_timeout():
    """The exact query that hung the dashboard in review — 2.17 M × 4.7 M row
    pairs — must now be cancelled, not run to completion."""
    with pytest.raises(QueryTimeout):
        execute_readonly_capped(
            "SELECT COUNT(*) FROM stop_time a, service_date b",
            timeout_seconds=2,
        )


@needs_db
def test_writes_are_impossible_even_if_a_write_reaches_execution():
    """Defence in depth: the connection is read-only, so a write raises rather
    than mutating the database — independent of the guardrail."""
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        execute_readonly_capped("CREATE TABLE hack (x)")
