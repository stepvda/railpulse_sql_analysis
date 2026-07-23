"""Execute ``sql/analysis/*.sql`` and publish the results.

    railpulse analyse                 # run every question, write output/
    railpulse analyse --question q2   # just one
    railpulse analyse --no-csv        # markdown report only

WHAT THIS MODULE IS AND IS NOT
------------------------------
It is a runner. It opens the database READ-ONLY, executes the SQL somebody else
wrote, and renders whatever comes back. It contains no analytical logic at all
— no thresholds, no rankings, no derived percentages. If a number appears in
``docs/analysis_report.md`` it came out of a ``.sql`` file, and you can find it
by its label.

THE ``-- @label:`` CONVENTION
----------------------------
Each analysis file is a sequence of queries annotated with structured comments::

    -- @label: q1_peak_hour_headline      (required; names the output file)
    -- @title: One-line answer            (optional; heading in the report)
    -- @description: ...                  (optional; may span several lines)
    SELECT ...;

The runner splits the file with :func:`railpulse.db.iter_statements`, pairs each
statement with the annotation block immediately above it, and writes one CSV per
label. A query with no ``@label`` still runs and is still reported, under a
generated name — so an unannotated query cannot silently vanish from the report.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Sequence

from . import config
from .db import connect, iter_statements

#: Files are run in this order; anything else matching q*.sql is appended.
PREFERRED_ORDER = (
    "q1_peak_hour.sql",
    "q2_platform_bottlenecks.sql",
    "q3_morning_destinations.sql",
    "q4_service_frequency.sql",
    "q5_accessibility_audit.sql",
    "q6_network_leaderboard.sql",
    "q7_index_optimisation.sql",
)

_ANNOTATION = re.compile(r"^\s*--\s*@(\w+)\s*:\s*(.*)$")
_COMMENT = re.compile(r"^\s*--\s?(.*)$")
#: Truncate very wide result sets in the markdown report; the CSV keeps them all.
MAX_REPORT_ROWS = 30


@dataclass
class Query:
    """One annotated statement from an analysis file."""

    label: str
    title: str
    description: str
    sql: str
    source_file: str
    #: True when the file gave no ``@label`` and one had to be generated.
    #: Tests assert this is never set, so a query cannot lose its citable name.
    label_generated: bool = False
    rows: list[sqlite3.Row] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    error: str | None = None


def parse_analysis_file(path: Path) -> Iterator[Query]:
    """Yield the annotated queries in *path*, in file order."""
    text = path.read_text(encoding="utf-8")
    counter = 0

    for statement in iter_statements(text):
        counter += 1
        label = ""
        title = ""
        description_parts: list[str] = []
        sql_lines: list[str] = []
        in_body = False

        for raw_line in statement.splitlines():
            annotation = _ANNOTATION.match(raw_line)
            if annotation and not in_body:
                key, value = annotation.group(1).lower(), annotation.group(2).strip()
                if key == "label":
                    label = value
                elif key == "title":
                    title = value
                elif key == "description":
                    description_parts.append(value)
                continue

            comment = _COMMENT.match(raw_line)
            if comment is not None and not in_body:
                # A continuation line of a multi-line @description, or one of
                # the long explanatory banners at the top of the file.
                if description_parts and comment.group(1).strip():
                    description_parts.append(comment.group(1).strip())
                continue

            if raw_line.strip():
                in_body = True
            if in_body:
                sql_lines.append(raw_line)

        sql = "\n".join(sql_lines).strip()
        if not sql:
            continue

        yield Query(
            label=label or f"{path.stem}_unlabelled_{counter:02d}",
            title=title or (label or f"Query {counter}"),
            description=" ".join(description_parts).strip(),
            sql=sql,
            source_file=path.name,
            label_generated=not label,
        )


def run_file(conn: sqlite3.Connection, path: Path) -> list[Query]:
    """Execute every query in *path*, capturing rows, timing and any error."""
    queries = list(parse_analysis_file(path))
    print(f"\n  {path.name} — {len(queries)} queries")
    for query in queries:
        started = time.perf_counter()
        try:
            cursor = conn.execute(query.sql)
            query.rows = cursor.fetchall()
            query.columns = ([d[0] for d in cursor.description]
                             if cursor.description else [])
        except sqlite3.Error as exc:
            query.error = str(exc)
        query.elapsed_seconds = time.perf_counter() - started

        if query.error:
            print(f"    ✗ {query.label:<44} {query.error}")
        else:
            print(f"    ✓ {query.label:<44} "
                  f"{len(query.rows):>6,} rows  {query.elapsed_seconds:6.2f}s")
    return queries


def write_csv(query: Query, output_dir: Path) -> Path | None:
    """Write one query's full result set to ``output/<label>.csv``."""
    if query.error or not query.columns:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{query.label}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(query.columns)
        writer.writerows([tuple(row) for row in query.rows])
    return path


def _markdown_table(columns: Sequence[str], rows: Sequence[sqlite3.Row]) -> str:
    """Render rows as a GitHub-flavoured markdown table."""
    if not columns:
        return "_(no result set)_"
    if not rows:
        return "_(no rows)_"

    def cell(value: object) -> str:
        if value is None:
            return ""
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |",
             "|" + "|".join("---" for _ in columns) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(cell(v) for v in tuple(row)) + " |")
    return "\n".join(lines)


def write_report(
    results: dict[str, list[Query]],
    output_path: Path,
    *,
    metadata: dict[str, object],
) -> Path:
    """Render every result set into one markdown report."""
    parts: list[str] = []
    parts.append("# RailPulse — analysis results\n")
    parts.append(
        "Generated by `railpulse analyse`. Every table below is the verbatim "
        "output of a query in `sql/analysis/`, identified by its label. Nothing "
        "here is computed in Python.\n"
    )
    parts.append("| | |")
    parts.append("|---|---|")
    for key, value in metadata.items():
        parts.append(f"| {key} | {value} |")
    parts.append("")

    parts.append("## Contents\n")
    for filename, queries in results.items():
        parts.append(f"- **{filename}**")
        for query in queries:
            anchor = query.label.lower().replace("_", "-")
            parts.append(f"  - [{query.title}](#{anchor})")
    parts.append("")

    for filename, queries in results.items():
        parts.append(f"\n---\n\n## `{filename}`\n")
        for query in queries:
            parts.append(f"### <a id=\"{query.label.lower().replace('_', '-')}\">"
                         f"</a>{query.title}\n")
            parts.append(f"`{query.label}` · {query.elapsed_seconds:.2f}s · "
                         f"{len(query.rows):,} rows\n")
            if query.description:
                parts.append(f"> {query.description}\n")
            if query.error:
                parts.append(f"**Query failed:** `{query.error}`\n")
                continue
            shown = query.rows[:MAX_REPORT_ROWS]
            parts.append(_markdown_table(query.columns, shown))
            if len(query.rows) > MAX_REPORT_ROWS:
                parts.append(f"\n_… {len(query.rows) - MAX_REPORT_ROWS:,} further "
                             f"rows in `output/{query.label}.csv`_")
            parts.append("")
            parts.append("<details><summary>SQL</summary>\n")
            parts.append("```sql")
            parts.append(query.sql)
            parts.append("```")
            parts.append("</details>\n")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(parts), encoding="utf-8")
    return output_path


def analyse(
    *,
    questions: Sequence[str] | None = None,
    write_csvs: bool = True,
    output_dir: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, list[Query]]:
    """Run the analysis suite and write ``output/`` plus the markdown report."""
    if not config.DB_PATH.exists():
        raise SystemExit(
            f"{config.DB_PATH} does not exist. Run `make build` first."
        )

    output_dir = output_dir or config.OUTPUT_DIR

    # A filtered run must not overwrite the full report with a partial one:
    # docs/analysis_results.md is a published artefact that other documents link
    # into, and silently truncating it to one question is a nasty way to lose
    # work. Filtered runs write to their own file instead.
    if report_path is None:
        if questions:
            suffix = "-".join(sorted(q.lower() for q in questions))
            report_path = config.DOCS_DIR / f"analysis_results.partial-{suffix}.md"
        else:
            report_path = config.DOCS_DIR / "analysis_results.md"

    available = sorted(config.ANALYSIS_SQL_DIR.glob("q*.sql"))
    ordered = [config.ANALYSIS_SQL_DIR / name for name in PREFERRED_ORDER
               if (config.ANALYSIS_SQL_DIR / name).exists()]
    ordered += [p for p in available if p not in ordered]

    if questions:
        wanted = {q.lower() for q in questions}
        ordered = [p for p in ordered
                   if any(p.name.lower().startswith(w) for w in wanted)]
        if not ordered:
            raise SystemExit(
                f"no analysis file matches {sorted(wanted)}; available: "
                f"{[p.name for p in available]}"
            )

    conn = connect(read_only=True)
    conn.execute("PRAGMA cache_size = -262144")
    started = time.perf_counter()
    results: dict[str, list[Query]] = {}
    try:
        print(f"=== RailPulse analysis ({len(ordered)} files) ===")
        for path in ordered:
            results[path.name] = run_file(conn, path)

        feed = conn.execute(
            "SELECT feed_id, feed_start_date, feed_end_date, feed_version "
            "  FROM feed_info LIMIT 1"
        ).fetchone()
        run = conn.execute(
            "SELECT started_at_utc, source_last_modified, rows_loaded, "
            "       rows_rejected "
            "  FROM ingestion_run WHERE status = 'ok' "
            " ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    written = 0
    if write_csvs:
        for queries in results.values():
            for query in queries:
                if write_csv(query, output_dir):
                    written += 1

    metadata: dict[str, object] = {
        "Source": "NMBS/SNCB GTFS Static — Belgian Mobility Open Data portal",
        "Licence": f"{config.DATA_LICENCE}",
        "Attribution": config.ATTRIBUTION_TEMPLATE.format(
            feed_date=feed["feed_version"] if feed else "unknown"),
        "Feed window": (f"{feed['feed_start_date']} to {feed['feed_end_date']}"
                        if feed else "unknown"),
        "Feed version": feed["feed_version"] if feed else "unknown",
        "Ingested at (UTC)": run["started_at_utc"] if run else "unknown",
        "Upstream Last-Modified": (run["source_last_modified"]
                                   if run else "unknown"),
        "Rows quarantined": f"{run['rows_rejected']:,}" if run else "unknown",
    }
    report = write_report(results, report_path, metadata=metadata)

    total_queries = sum(len(q) for q in results.values())
    failures = [q for qs in results.values() for q in qs if q.error]
    elapsed = time.perf_counter() - started
    print(f"\n=== {total_queries} queries in {elapsed:0.1f}s · "
          f"{written} CSVs -> {output_dir} · report -> {report} ===")
    if failures:
        print(f"!!! {len(failures)} query/queries failed:")
        for query in failures:
            print(f"    {query.source_file} :: {query.label}: {query.error}")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="railpulse analyse",
        description="Run sql/analysis/*.sql and publish CSVs plus a report.",
    )
    parser.add_argument("--question", "-q", action="append", default=None,
                        help="run only files starting with this prefix "
                             "(e.g. q2); repeatable")
    parser.add_argument("--no-csv", action="store_true",
                        help="skip writing output/*.csv")
    args = parser.parse_args(argv)

    results = analyse(questions=args.question, write_csvs=not args.no_csv)
    failed = any(q.error for qs in results.values() for q in qs)
    return 1 if failed else 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
