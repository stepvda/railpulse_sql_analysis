#!/usr/bin/env bash
# ===========================================================================
# RailPulse — real-time poller (the "Live Stream Integration" nice-to-have)
# ===========================================================================
# Appends one GTFS-Realtime snapshot (trip updates + service alerts) to the
# database. Designed to be driven by cron or launchd so that, left running, it
# accumulates the delay history the static timetable can be measured against.
#
# INSTALL AS A CRON JOB (every 30 minutes — see the quota arithmetic below)
#   crontab -e
#   */30 * * * * /full/path/to/railpulse_sql_analysis/scripts/poll_realtime.sh
#
# INSTALL ON MACOS WITH launchd (survives reboots, preferred over cron on Mac)
#   see docs/api_and_compliance.md for a ready-made .plist
#
# WHY 30 MINUTES AND NOT 30 SECONDS
# The feed itself refreshes every ~30 s, so a faster poll would capture more
# detail. The limit is the published anonymous quota of 100 requests/day, and
# the arithmetic has to count REQUESTS, not runs: each run costs 2 requests
# (trip-update + alert).
#
#   interval   runs/day   requests/day   vs the 100/day anonymous quota
#   --------   --------   ------------   ------------------------------
#   */15            96            192    192 %  — OVER. Do not use anonymously.
#   */30            48             96     96 %  — just inside. The default here.
#   hourly          24             48     48 %  — comfortable headroom.
#   every 3 h        8             16     16 %  — long-horizon trend only.
#
# */30 is the default because it is the fastest cadence that fits the anonymous
# quota. With a Standard subscription the ceiling is higher and */15 becomes
# reasonable — but that is a decision to take against your own quota, having
# done this arithmetic, rather than a default someone inherits by accident.
#
# Duplicate polls are harmless: rt_snapshot carries UNIQUE(feed,
# feed_timestamp_epoch), so a snapshot whose upstream timestamp has not moved
# is skipped rather than double-counted.
# ===========================================================================
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_DIR="${RAILPULSE_LOG_DIR:-$PROJECT_ROOT/data/logs}"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/realtime-$(date -u +%Y-%m-%d).log"

# Prefer the project's virtualenv when one exists, so cron (which has a minimal
# PATH and no shell profile) still finds the right interpreter.
if [ -x "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

export PYTHONPATH="$PROJECT_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

{
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) poll starting ---"
    "$PYTHON" -m railpulse.ingest_realtime --feed all
    echo "--- $(date -u +%Y-%m-%dT%H:%M:%SZ) poll finished ---"
} >> "$LOG_FILE" 2>&1
