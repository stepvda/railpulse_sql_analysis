"""RailPulse SQL Chat — natural-language query interface powered by Distil-Text2SQL.

A Streamlit page that provides a conversational interface for querying the
RailPulse SQLite database. The user types a question in natural language; a
locally-running distilled transformer model translates it into SQL; the SQL is
displayed transparently and executed against the read-only database.

Usage: this module is imported by ``dashboard/app.py`` and wired into the
sidebar radio navigation as the ``SQL Chat`` page.
"""

from __future__ import annotations

import textwrap
import time
import traceback
from datetime import datetime

import altair as alt
import pandas as pd
import streamlit as st

from dashboard.text2sql_engine import (
    MAX_RESULT_ROWS,
    PROSE_SCHEMA,
    QueryTimeout,
    execute_readonly_capped,
    generate_sql,
    sanitize_for_execution,
)

MESSAGE_ROLE_USER = "user"
MESSAGE_ROLE_ASSISTANT = "assistant"

CHART_BLUE = "#2a78d6"

EXAMPLE_QUESTIONS = [
    "How many stations are in the database?",
    "List the top 10 busiest stations by annual departures.",
    "Which routes have the most trips? Show route_short_name and count.",
    "How many distinct services operate on Saturdays?",
    "Which station has the most platforms with wheelchair boarding?",
    "List all rail routes that have 'Bruxelles' in their name.",
    "Show the hourly departure count across the whole network (use v_departure).",
    "What percentage of trips guarantee bicycle storage?",
    "Find the average number of stops per trip.",
    "Which platforms at Bruxelles-Central have the most departures?",
]

CHAT_INTRO = textwrap.dedent("""\
    Ask a question about the SNCB/NMBS timetable in natural language. A
    locally-running **Distil-Text2SQL** model translates your question into SQL,
    executes it against the read-only `railpulse.db`, and shows the results
    below.

    The model knows the complete schema — stations, platforms, routes, trips,
    stop times, calendar dates, and the semantic views (`v_departure`,
    `v_trip_service_days`, `v_trip_origin`, etc.). Every generated SQL is shown
    in an expander so you can verify, copy and tune it.
""")


def _init_session() -> None:
    """Initialise session-state keys used by the chat page."""
    defaults = {
        "sql_chat_history": [],
        "sql_chat_model_name": "",
        "sql_chat_show_advanced": False,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _render_message(msg: dict) -> None:
    """Render a single chat bubble from session state.

    For assistant messages that carry SQL and results metadata, also renders
    the SQL expander, result table, and auto-chart so the full response
    survives Streamlit reruns.
    """
    role = msg["role"]
    content = msg["content"]
    timestamp = msg["timestamp"]
    avatar = "🧑" if role == MESSAGE_ROLE_USER else "🤖"
    with st.chat_message(role, avatar=avatar):
        st.markdown(content)
        st.caption(timestamp)

        if role == MESSAGE_ROLE_ASSISTANT and msg.get("sql"):
            _render_sql_block(msg["sql"], msg.get("generation_ms", 0))
            rows_data = msg.get("rows")
            columns_data = msg.get("columns")
            if rows_data is not None and columns_data is not None:
                frame = pd.DataFrame(rows_data, columns=columns_data)
                if frame.empty:
                    st.info("The query returned no rows.")
                else:
                    _render_result_table(frame, msg.get("execution_ms", 0))
                    _auto_chart(frame)


def _render_sql_block(sql: str, elapsed_ms: float) -> None:
    """Render the generated SQL inside an expander with copy button."""
    label = f"Show the SQL ({elapsed_ms:.0f} ms)"
    with st.expander(label, expanded=False):
        st.code(sql, language="sql")


def _render_result_table(frame: pd.DataFrame, elapsed_ms: float) -> None:
    """Render a query result as a table with row count."""
    rows = len(frame)
    cols = len(frame.columns)
    st.caption(f"{rows:,} row{'s' if rows != 1 else ''} × {cols} column{'s' if cols != 1 else ''} · {elapsed_ms:.0f} ms")
    if not frame.empty:
        st.dataframe(frame, use_container_width=True, hide_index=True)


def _auto_chart(frame: pd.DataFrame) -> None:
    """Attempt to render an automatic chart for the result set.

    Tries to detect chart-worthy shapes:
    - One numeric column + one categorical → horizontal bar chart
    - Two columns, one looks like a time series → line or bar chart
    - Numeric + categorical + numeric (3+ cols) → no auto-chart
    Returns silently if the shape is not chartable.
    """
    if frame.empty or len(frame.columns) < 2:
        return

    numeric_cols = frame.select_dtypes(include=["number"]).columns.tolist()
    if not numeric_cols:
        return

    categorical_cols = [
        c for c in frame.columns
        if c not in numeric_cols or (
            frame[c].dtype == "object"
            and frame[c].nunique() < min(30, max(2, len(frame) * 0.8))
        )
    ]

    if not categorical_cols:
        return

    cat_col = categorical_cols[0]
    num_col = numeric_cols[0]

    unique_cats = frame[cat_col].nunique()
    if unique_cats < 2 or unique_cats > 50:
        return

    chart_height = max(140, min(600, unique_cats * 26))

    try:
        chart = (
            alt.Chart(frame.head(50))
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color=CHART_BLUE)
            .encode(
                x=alt.X(f"{num_col}:Q", title=num_col),
                y=alt.Y(f"{cat_col}:N", title=cat_col, sort=None),
                tooltip=list(frame.columns),
            )
            .properties(height=chart_height)
        )
        st.altair_chart(chart, use_container_width=True)
    except Exception:
        pass


def _add_message(role: str, content: str, **extra) -> None:
    """Append a message to the chat history.

    Extra kwargs are stored as metadata (sql, columns, rows, generation_ms,
    execution_ms) and used to re-render assistant results from session state.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    msg = {"role": role, "content": content, "timestamp": ts}
    msg.update(extra)
    st.session_state.sql_chat_history.append(msg)


def _handle_question(question: str) -> None:
    """Process a user question: generate SQL, execute it, render results inline.

    User message is rendered immediately. The assistant response (SQL, results,
    chart) is rendered inline AND stored in session state so it survives reruns.
    """
    with st.chat_message(MESSAGE_ROLE_USER, avatar="🧑"):
        st.markdown(question)

    _add_message(MESSAGE_ROLE_USER, question)

    model_name = st.session_state.sql_chat_show_advanced and st.session_state.sql_chat_model_name or None

    generation_start = time.perf_counter()
    try:
        sql = generate_sql(question, model_name=model_name or None)
    except RuntimeError as exc:
        elapsed = (time.perf_counter() - generation_start) * 1000
        st.error(str(exc))
        return
    except ValueError as exc:
        elapsed = (time.perf_counter() - generation_start) * 1000
        content = f"The model could not produce a valid query. {exc}"
        with st.chat_message(MESSAGE_ROLE_ASSISTANT, avatar="🤖"):
            st.warning(str(exc))
        _add_message(MESSAGE_ROLE_ASSISTANT, content)
        return

    generation_elapsed = (time.perf_counter() - generation_start) * 1000
    sql = sanitize_for_execution(sql)

    execution_start = time.perf_counter()
    truncated = False
    try:
        # Capped execution: a row ceiling and a wall-clock timeout, so a valid
        # but pathological query (cartesian join, unbounded SELECT *) cannot hang
        # the dashboard or exhaust memory. See execute_readonly_capped.
        columns, rows, truncated = execute_readonly_capped(sql)
        frame = pd.DataFrame(rows, columns=columns)
    except QueryTimeout as exc:
        execution_elapsed = (time.perf_counter() - execution_start) * 1000
        content = (
            f"**Generated SQL:**\n\n```sql\n{sql}\n```\n\n"
            f"**Query cancelled:** {exc}"
        )
        with st.chat_message(MESSAGE_ROLE_ASSISTANT, avatar="🤖"):
            st.markdown("**Generated SQL:**")
            st.code(sql, language="sql")
            st.warning(str(exc))
        _add_message(MESSAGE_ROLE_ASSISTANT, content, sql=sql)
        return
    except Exception:
        execution_elapsed = (time.perf_counter() - execution_start) * 1000
        error_detail = traceback.format_exc()
        error_short = error_detail.strip().split("\n")[-1] if error_detail else "Unknown error"

        content = (
            f"**Generated SQL:**\n\n```sql\n{sql}\n```\n\n"
            f"**Execution failed:** `{error_short}`"
        )
        with st.chat_message(MESSAGE_ROLE_ASSISTANT, avatar="🤖"):
            st.markdown("**Generated SQL:**")
            st.code(sql, language="sql")
            st.error(f"Execution failed: `{error_short}`")
            with st.expander("Full traceback"):
                st.code(error_detail)
        _add_message(MESSAGE_ROLE_ASSISTANT, content, sql=sql)
        return

    execution_elapsed = (time.perf_counter() - execution_start) * 1000

    rows_count = len(frame)
    truncation_note = (
        f" (showing the first {MAX_RESULT_ROWS:,}; the query matched more)"
        if truncated else ""
    )
    content = (
        f"**Generated SQL:**\n\n```sql\n{sql}\n```\n\n"
        f"**Result:** {rows_count:,} row{'s' if rows_count != 1 else ''} returned in "
        f"{execution_elapsed:.0f} ms{truncation_note}."
    )

    with st.chat_message(MESSAGE_ROLE_ASSISTANT, avatar="🤖"):
        st.markdown(content)
        _render_sql_block(sql, generation_elapsed)
        if frame.empty:
            st.info("The query returned no rows. The SQL may be valid but no data matched.")
        else:
            if truncated:
                st.info(f"Result truncated to the first {MAX_RESULT_ROWS:,} rows.")
            _render_result_table(frame, execution_elapsed)
            _auto_chart(frame)

    _add_message(
        MESSAGE_ROLE_ASSISTANT,
        content,
        sql=sql,
        columns=list(frame.columns),
        rows=[list(row) for row in frame.itertuples(index=False)],
        generation_ms=generation_elapsed,
        execution_ms=execution_elapsed,
    )


def page_sql_chat() -> None:
    """The SQL Chat page — natural language → SQL → results."""
    st.title("SQL Chat — Ask the timetable")
    st.markdown(CHAT_INTRO)

    _init_session()

    with st.sidebar:
        st.subheader("SQL Chat")
        st.caption("Distil-Text2SQL · local inference")

        if st.button("Clear chat history", use_container_width=True):
            st.session_state.sql_chat_history = []
            st.rerun()

        st.divider()

        st.session_state.sql_chat_show_advanced = st.checkbox(
            "Advanced settings",
            value=st.session_state.sql_chat_show_advanced,
        )

        if st.session_state.sql_chat_show_advanced:
            st.session_state.sql_chat_model_name = st.text_input(
                "Model name",
                value=st.session_state.sql_chat_model_name,
                placeholder="Default (juierror/flan-t5-text2sql-with-schema-v2)",
                help=(
                    "HuggingFace model ID. Leave blank for the default. "
                    "Lighter alternative: mrm8488/t5-small-finetuned-wikiSQL"
                ),
            )
            with st.expander("Full schema reference"):
                st.code(PROSE_SCHEMA, language="text", line_numbers=False)
        else:
            st.caption(
                "Enable Advanced settings to override the model or inspect the "
                "full schema reference."
            )

    history = st.session_state.sql_chat_history
    for msg in history:
        _render_message(msg)

    if not history:
        st.info("Try one of these questions to get started:")
        cols = st.columns(2)
        for i, question in enumerate(EXAMPLE_QUESTIONS):
            with cols[i % 2]:
                if st.button(question, key=f"example_{i}", use_container_width=True):
                    st.session_state.sql_chat_example = question

    if "sql_chat_example" in st.session_state:
        q = st.session_state.pop("sql_chat_example")
        _handle_question(q)

    if question := st.chat_input("Ask a question about the SNCB/NMBS timetable…"):
        _handle_question(question)
