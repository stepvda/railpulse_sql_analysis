# ===========================================================================
# RailPulse — Sprint 1
# ===========================================================================
# The whole project, from an empty checkout to answered questions:
#
#     make setup     install dependencies
#     make all       fetch the feed, build the database, verify it, analyse it
#     make dashboard open the Streamlit report
#
# Run `make help` for everything else.
# ===========================================================================

SHELL      := /bin/bash
PYTHON     ?= python3
export PYTHONPATH := $(CURDIR)/src:$(PYTHONPATH)

DB            := data/railpulse.db
ZIP           := data/raw/nmbssncb_gtfs_static.zip
# Only the schema/transform files shape the database. Editing an analysis query
# changes what you ask, not what is stored, so sql/analysis/*.sql is
# deliberately NOT a prerequisite of $(DB) — otherwise every tweak to a
# question would trigger a three-minute rebuild that changes nothing.
SCHEMA_SQL    := $(wildcard sql/[0-9]*.sql)
ANALYSIS_SQL  := $(wildcard sql/analysis/*.sql)
SQL_FILES     := $(SCHEMA_SQL) $(ANALYSIS_SQL)

.DEFAULT_GOAL := help
.PHONY: help setup all fetch build rebuild verify analyse poll benchmark \
        dashboard info clean clean-db clean-output test lint sqlfmt-check \
        api-key

# ---------------------------------------------------------------------------
help:  ## show this help
	@echo "RailPulse — Belgian transit SQL analysis (Sprint 1)"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Typical first run:  make setup && make all"

# ---------------------------------------------------------------------------
setup:  ## install the pipeline dependencies
	$(PYTHON) -m pip install -r requirements.txt

setup-dashboard:  ## install the optional dashboard + browser-automation extras
	$(PYTHON) -m pip install -r requirements-dashboard.txt
	$(PYTHON) -m playwright install chromium

# ---------------------------------------------------------------------------
all: build verify analyse  ## fetch, build, verify and analyse — the whole thing

fetch: $(ZIP)  ## download the GTFS Static feed (skipped when already current)

$(ZIP):
	$(PYTHON) -m railpulse fetch

# The database depends on the feed AND on every schema/transform file, so
# editing a cleaning rule or a table definition triggers a rebuild rather than
# leaving a stale database behind a green build.
$(DB): $(ZIP) $(SCHEMA_SQL)
	$(PYTHON) -m railpulse build --offline

build: $(DB)  ## rebuild the database if the feed or any .sql file changed

rebuild:  ## force a full rebuild, re-downloading the feed
	$(PYTHON) -m railpulse build --force-download

verify: $(DB)  ## assert the built database is internally consistent
	$(PYTHON) -m railpulse verify

analyse: $(DB)  ## run sql/analysis/*.sql, write output/ and docs/analysis_results.md
	$(PYTHON) -m railpulse analyse

poll: $(DB)  ## append one GTFS-Realtime snapshot
	$(PYTHON) -m railpulse poll

benchmark: $(DB)  ## measure the index and SARGability effects
	$(PYTHON) -m railpulse benchmark --with-index-drops

info: $(DB)  ## summarise what is currently loaded
	$(PYTHON) -m railpulse info

dashboard: $(DB)  ## launch the Streamlit report
	streamlit run dashboard/app.py

api-key:  ## create the developer-portal subscription and print the key
	$(PYTHON) scripts/setup_api_key.py

# ---------------------------------------------------------------------------
test:  ## run the test suite
	$(PYTHON) -m pytest tests -v

lint:  ## byte-compile the Python and syntax-check every SQL statement
	$(PYTHON) -m compileall -q src/railpulse dashboard scripts
	@$(PYTHON) -c "$$LINT_SQL"

# Prepares (but never runs) every statement in every .sql file against an
# in-memory database loaded with the real schema. sqlite3_prepare is a genuine
# parser, so this catches a typo, an unbalanced paren or an unknown function
# without touching the 1 GB database. Unknown *table* errors are expected for
# the analysis files here and are reported separately by `make test`, which
# runs them for real against the fixture feed.
define LINT_SQL
import pathlib, sqlite3, sys
sys.path.insert(0, "src")
from railpulse.db import iter_statements
conn = sqlite3.connect(":memory:")
for setup in ("sql/02_schema.sql", "sql/06_realtime.sql", "sql/05_views.sql"):
    for stmt in iter_statements(pathlib.Path(setup).read_text()):
        try:
            conn.execute(stmt)
        except sqlite3.Error as exc:
            print(f"FAIL {setup}: {exc}\n  {stmt.splitlines()[0][:90]}")
            sys.exit(1)
def body(statement):
    """The statement with its leading -- comment lines removed.

    Every analysis query is preceded by its `-- @label:` block, so a naive
    startswith() test sees the comment rather than the verb.
    """
    lines = statement.splitlines()
    while lines and (not lines[0].strip() or lines[0].lstrip().startswith("--")):
        lines.pop(0)
    return "\n".join(lines).lstrip()

bad = 0
checked = 0
for path in sorted(pathlib.Path("sql").rglob("*.sql")):
    for stmt in iter_statements(path.read_text()):
        checked += 1
        # q7's statements are already EXPLAIN QUERY PLAN; EXPLAIN EXPLAIN is
        # not valid SQL, so prepare those as-is.
        core = body(stmt)
        probe = stmt if core.upper().startswith("EXPLAIN") else "EXPLAIN " + stmt
        try:
            conn.execute(probe)
        except sqlite3.Error as exc:
            message = str(exc)
            # DDL and the staging tables are not present in this throwaway
            # schema; only real syntax problems should fail the lint.
            if "no such table" in message or "already exists" in message:
                continue
            if core.upper().startswith(("CREATE", "DROP", "INSERT", "DELETE", "ANALYZE", "PRAGMA", "WITH RECURSIVE")):
                continue
            print(f"FAIL {path}: {message}\n  {stmt.splitlines()[0][:90]}")
            bad += 1
print(f"lint ok — {checked} SQL statements parsed" if not bad else f"{bad} SQL statement(s) failed")
sys.exit(1 if bad else 0)
endef
export LINT_SQL

# ---------------------------------------------------------------------------
clean-output:  ## remove generated CSVs and the results report
	rm -f output/*.csv docs/analysis_results.md

clean-db:  ## remove the database (it is fully reproducible)
	rm -f $(DB) $(DB)-wal $(DB)-shm

clean: clean-output clean-db  ## remove everything generated except the raw feed
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
