"""Splitting SQL scripts, and reading the `-- @label:` annotations.

This machinery sits underneath everything: `run_sql_file` uses it to execute a
schema file inside one transaction, and `analyse` uses it to name every result
set. If it mis-splits a file, the failure is silent and horrible — a statement
truncated at a semicolon inside a string literal, or two queries merged into
one and reported under a single label.

The obvious implementation, `sql_text.split(";")`, breaks on the first
semicolon inside a string or a comment. This project's SQL is heavily
commented, so that is not a hypothetical. `sqlite3.complete_statement()` is the
engine's own tokenizer-aware check for "is this a whole statement yet?", and
these tests pin the behaviour that buys us.
"""

from __future__ import annotations

import pathlib
import textwrap

import pytest

from railpulse.analyse import parse_analysis_file
from railpulse.db import iter_statements


def write(tmp_path: pathlib.Path, sql: str) -> pathlib.Path:
    path = tmp_path / "case.sql"
    path.write_text(textwrap.dedent(sql), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# iter_statements — the splitter
# ---------------------------------------------------------------------------
def test_semicolon_inside_a_string_literal_does_not_split():
    statements = list(iter_statements("SELECT 'a;b' AS x;\nSELECT 2;\n"))
    assert len(statements) == 2
    assert "'a;b'" in statements[0]


def test_semicolon_inside_a_comment_does_not_split():
    statements = list(iter_statements(
        "-- this comment; contains a semicolon\nSELECT 1;\n"
    ))
    assert len(statements) == 1


def test_comment_only_blocks_are_dropped():
    """The schema files open with long banner comments. They are not statements
    and must not be handed to the engine."""
    assert list(iter_statements("-- just a banner\n-- more banner\n")) == []
    assert list(iter_statements("")) == []


def test_trailing_statement_without_a_semicolon_is_kept():
    statements = list(iter_statements("SELECT 1;\nSELECT 2\n"))
    assert len(statements) == 2


def test_nested_parentheses_and_ctes_survive():
    sql = "WITH x AS (SELECT ';' AS s) SELECT * FROM x;"
    assert list(iter_statements(sql)) == [sql]


def test_the_real_schema_files_split_into_runnable_statements():
    """A regression guard on the actual project files, not a synthetic case."""
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in ("01_staging.sql", "02_schema.sql", "03_transform.sql",
                 "04_indexes.sql", "05_views.sql", "06_realtime.sql"):
        text = (root / "sql" / name).read_text(encoding="utf-8")
        statements = list(iter_statements(text))
        assert statements, f"{name} produced no statements"
        for statement in statements:
            # Nothing that reaches the engine may be pure commentary.
            assert any(not line.strip().startswith("--") and line.strip()
                       for line in statement.splitlines()), \
                f"{name} yielded a comment-only statement"


# ---------------------------------------------------------------------------
# parse_analysis_file — the annotation reader
# ---------------------------------------------------------------------------
def test_label_title_and_description_are_read(tmp_path):
    path = write(tmp_path, """
        -- @label: my_label
        -- @title: My Title
        -- @description: first line
        --   second line
        SELECT 1;
    """)
    query = next(iter(parse_analysis_file(path)))
    assert query.label == "my_label"
    assert query.title == "My Title"
    assert "first line" in query.description
    assert "second line" in query.description
    assert query.label_generated is False


def test_missing_label_is_generated_and_flagged(tmp_path):
    """An unlabelled query still runs and is still reported — it must never
    vanish from the results — but it is flagged so a test can catch it."""
    query = next(iter(parse_analysis_file(write(tmp_path, "SELECT 1;"))))
    assert query.label_generated is True
    assert "unlabelled" in query.label


def test_annotations_do_not_leak_into_the_sql(tmp_path):
    """The `@label` comment must not be executed as part of the statement."""
    path = write(tmp_path, """
        -- @label: a
        -- @description: some prose
        SELECT 1;
    """)
    query = next(iter(parse_analysis_file(path)))
    assert "@label" not in query.sql
    assert "some prose" not in query.sql
    assert query.sql.strip().startswith("SELECT")


def test_each_query_gets_its_own_annotations(tmp_path):
    path = write(tmp_path, """
        -- @label: first
        SELECT 1;
        -- @label: second
        SELECT 2;
    """)
    labels = [q.label for q in parse_analysis_file(path)]
    assert labels == ["first", "second"]


def test_banner_comment_between_queries_is_not_treated_as_a_query(tmp_path):
    path = write(tmp_path, """
        -- @label: a
        SELECT 1;
        -- ==========================
        -- a section banner
        -- ==========================
        -- @label: b
        SELECT 2;
    """)
    queries = list(parse_analysis_file(path))
    assert [q.label for q in queries] == ["a", "b"]


@pytest.mark.parametrize("content", ["", "-- only a comment\n", "\n\n\n"])
def test_degenerate_files_yield_nothing_and_do_not_raise(tmp_path, content):
    assert list(parse_analysis_file(write(tmp_path, content))) == []
