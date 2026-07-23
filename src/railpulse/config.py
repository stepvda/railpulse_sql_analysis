"""Paths, endpoints and tunables for the RailPulse pipeline.

Everything configurable lives here. Nothing else in the package reads
``os.environ`` directly, so there is exactly one place to look when a value
seems wrong.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------
# python-dotenv is a convenience, not a requirement: if it is missing the
# pipeline still works from real environment variables. Ingestion should not
# fall over because an optional helper is not installed.
try:  # pragma: no cover - trivial import guard
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs) -> bool:
        return False


# --------------------------------------------------------------------------
# Filesystem layout
# --------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[1]

SQL_DIR = PROJECT_ROOT / "sql"
ANALYSIS_SQL_DIR = SQL_DIR / "analysis"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
OUTPUT_DIR = PROJECT_ROOT / "output"
DOCS_DIR = PROJECT_ROOT / "docs"

load_dotenv(PROJECT_ROOT / ".env")


def _env(name: str, default: str) -> str:
    """Read an environment variable, falling back to *default* when unset/blank."""
    value = os.environ.get(name, "").strip()
    return value or default


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
DB_PATH = Path(_env("RAILPULSE_DB", str(DATA_DIR / "railpulse.db")))
if not DB_PATH.is_absolute():
    DB_PATH = PROJECT_ROOT / DB_PATH

# --------------------------------------------------------------------------
# Belgian Mobility Open Data API
# --------------------------------------------------------------------------
# Discovered from https://data.belgianmobility.io/en/data.html. The endpoints
# answer without a key, but anonymous callers are throttled hard (see below),
# so the pipeline always sends one when it has it.
API_BASE = _env(
    "RAILPULSE_API_BASE",
    "https://api-management-discovery-production.azure-api.net",
)
DEVELOPER_PORTAL = (
    "https://api-management-opendata-production.developer.azure-api.net"
)

#: Which public transport operator to ingest. The schema is operator-agnostic;
#: 'delijn', 'tec' and 'stibmivb' feeds have the same shape.
OPERATOR = _env("RAILPULSE_OPERATOR", "nmbssncb")

GTFS_STATIC_URL = f"{API_BASE}/api/gtfs/feed/{OPERATOR}/static"
GTFS_RT_TRIP_UPDATE_URL = f"{API_BASE}/api/gtfs/feed/{OPERATOR}/rt/trip-update"
GTFS_RT_ALERT_URL = f"{API_BASE}/api/gtfs/feed/{OPERATOR}/rt/alert"

#: Azure API Management expects the subscription key in this header.
API_KEY_HEADER = "Ocp-Apim-Subscription-Key"
API_KEY = os.environ.get("BMC_API_KEY", "").strip()

# --------------------------------------------------------------------------
# Rate limiting — the brief says "be mindful of the request limits so you don't
# get blocked". The portal publishes 10 requests/minute and 100 requests/day for
# anonymous callers; a Standard subscription raises that, but this client stays
# at the anonymous ceiling so it is safe either way.
# --------------------------------------------------------------------------
MAX_REQUESTS_PER_MINUTE = int(_env("RAILPULSE_MAX_RPM", "10"))
MIN_SECONDS_BETWEEN_REQUESTS = 60.0 / max(MAX_REQUESTS_PER_MINUTE, 1)
REQUEST_TIMEOUT_SECONDS = int(_env("RAILPULSE_TIMEOUT", "180"))
MAX_RETRIES = int(_env("RAILPULSE_MAX_RETRIES", "4"))
USER_AGENT = _env(
    "RAILPULSE_USER_AGENT",
    "RailPulse/1.0 (BeCode data-engineering exercise; contact via GitHub issues)",
)

# --------------------------------------------------------------------------
# Attribution — required by the CC BY 4.0 licence the portal publishes under.
# --------------------------------------------------------------------------
DATA_LICENCE = "Creative Commons Attribution 4.0 International (CC BY 4.0)"
ATTRIBUTION_TEMPLATE = "NMBS/SNCB – Open Data – {feed_date}"

# --------------------------------------------------------------------------
# Ingestion tunables
# --------------------------------------------------------------------------
#: Rows per executemany() batch. 50 000 keeps peak memory around 40 MB while
#: still amortising the per-statement overhead across the 2.2 M-row file.
INSERT_BATCH_SIZE = int(_env("RAILPULSE_BATCH", "50000"))

#: GTFS file -> staging table. Order matters: parents load before children so a
#: partial run still leaves referential sense in the staging layer.
GTFS_FILE_TO_STAGING_TABLE: dict[str, str] = {
    "agency.txt": "stg_agency",
    "feed_info.txt": "stg_feed_info",
    "stops.txt": "stg_stops",
    "routes.txt": "stg_routes",
    "calendar.txt": "stg_calendar",
    "calendar_dates.txt": "stg_calendar_dates",
    "trips.txt": "stg_trips",
    "stop_times.txt": "stg_stop_times",
    "transfers.txt": "stg_transfers",
    "translations.txt": "stg_translations",
}

#: Files the pipeline needs; anything else in the zip is reported and skipped.
REQUIRED_GTFS_FILES = (
    "agency.txt", "stops.txt", "routes.txt",
    "trips.txt", "stop_times.txt", "calendar.txt",
)

# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------
#: The five hubs compared in the "Network Leaderboard" nice-to-have. Names are
#: the French forms used by the feed (feed_lang = 'fr'); the nl/de/en variants
#: are available through the text_translation table.
MAIN_HUBS = (
    "Bruxelles-Midi",
    "Bruxelles-Central",
    "Bruxelles-Nord",
    "Anvers-Central",
    "Gand-Saint-Pierre",
)

#: Station used by the platform-bottleneck question (Q2).
FOCUS_STATION = "Bruxelles-Central"
