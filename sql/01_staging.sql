-- ===========================================================================
-- RailPulse — 01_staging.sql
-- Landing zone for the raw SNCB/NMBS GTFS Static feed.
-- ===========================================================================
-- WHY A STAGING LAYER EXISTS
--
-- The challenge forbids using pandas (or any data-frame engine) to filter or
-- aggregate. So the pipeline is deliberately ELT, not ETL:
--
--     CSV  --(python: csv + sqlite3, verbatim copy)-->  stg_*   (this file)
--     stg_*  --(03_transform.sql, pure SQL)---------->  core model
--
-- Python never inspects, filters, casts or reshapes a value. It streams each
-- row of each .txt file straight into the matching stg_ table. *Every* cleaning
-- rule, type cast, de-duplication and integrity check lives in SQL, where a
-- reviewer can read it.
--
-- DESIGN RULES FOR THIS LAYER
--   * Every column is TEXT. A GTFS file is text; casting is a transform
--     concern, and a failed cast here would abort the load and lose the row.
--   * No PRIMARY KEY, no FOREIGN KEY, no NOT NULL, no CHECK. Staging must be
--     able to hold *bad* data — that is the whole point. Constraints are what
--     the core model adds, and rows that cannot satisfy them are quarantined
--     into `rejected_row` with a reason.
--   * Column names mirror the GTFS spec exactly, so the loader can map by
--     header name. The SNCB feed ships its columns in alphabetical order
--     rather than spec order, so positional loading would silently corrupt
--     the data.
--   * `src_line_no` records the physical line number in the source file, so a
--     quarantined row can be traced back to the exact line of the exact file.
-- ===========================================================================

DROP TABLE IF EXISTS stg_agency;
DROP TABLE IF EXISTS stg_feed_info;
DROP TABLE IF EXISTS stg_stops;
DROP TABLE IF EXISTS stg_routes;
DROP TABLE IF EXISTS stg_trips;
DROP TABLE IF EXISTS stg_stop_times;
DROP TABLE IF EXISTS stg_calendar;
DROP TABLE IF EXISTS stg_calendar_dates;
DROP TABLE IF EXISTS stg_transfers;
DROP TABLE IF EXISTS stg_translations;

-- --------------------------------------------------------------------------
-- agency.txt — one row: NMBS/SNCB
-- --------------------------------------------------------------------------
CREATE TABLE stg_agency (
    src_line_no     INTEGER,
    agency_id       TEXT,
    agency_name     TEXT,
    agency_url      TEXT,
    agency_timezone TEXT,
    agency_lang     TEXT,
    agency_phone    TEXT,
    agency_fare_url TEXT,
    agency_email    TEXT
);

-- --------------------------------------------------------------------------
-- feed_info.txt — provenance: publisher, language, validity window, version
-- --------------------------------------------------------------------------
CREATE TABLE stg_feed_info (
    src_line_no          INTEGER,
    feed_id              TEXT,
    feed_publisher_name  TEXT,
    feed_publisher_url   TEXT,
    feed_lang            TEXT,
    default_lang         TEXT,
    feed_start_date      TEXT,
    feed_end_date        TEXT,
    feed_version         TEXT,
    feed_contact_email   TEXT,
    feed_contact_url     TEXT
);

-- --------------------------------------------------------------------------
-- stops.txt — mixed grain: location_type=1 rows are stations,
--             location_type=0 rows are the boarding points (platforms).
--             03_transform.sql splits this single file into two tables.
-- --------------------------------------------------------------------------
CREATE TABLE stg_stops (
    src_line_no         INTEGER,
    stop_id             TEXT,
    stop_code           TEXT,
    stop_name           TEXT,
    stop_desc           TEXT,
    stop_lat            TEXT,
    stop_lon            TEXT,
    zone_id             TEXT,
    stop_url            TEXT,
    location_type       TEXT,
    parent_station      TEXT,
    stop_timezone       TEXT,
    wheelchair_boarding TEXT,
    level_id            TEXT,
    platform_code       TEXT
);

-- --------------------------------------------------------------------------
-- routes.txt — commercial lines (IC / L / S / P / BUS ...)
-- --------------------------------------------------------------------------
CREATE TABLE stg_routes (
    src_line_no      INTEGER,
    route_id         TEXT,
    agency_id        TEXT,
    route_short_name TEXT,
    route_long_name  TEXT,
    route_desc       TEXT,
    route_type       TEXT,
    route_url        TEXT,
    route_color      TEXT,
    route_text_color TEXT,
    route_sort_order TEXT
);

-- --------------------------------------------------------------------------
-- trips.txt — one physical vehicle journey on one route for one service
-- --------------------------------------------------------------------------
CREATE TABLE stg_trips (
    src_line_no           INTEGER,
    route_id              TEXT,
    service_id            TEXT,
    trip_id               TEXT,
    trip_headsign         TEXT,
    trip_short_name       TEXT,
    direction_id          TEXT,
    block_id              TEXT,
    shape_id              TEXT,
    wheelchair_accessible TEXT,
    bikes_allowed         TEXT
);

-- --------------------------------------------------------------------------
-- stop_times.txt — the fact grain (~2.2 M rows): a call of one trip at one
--                  boarding point. Loaded in batches by the ingestion job.
-- --------------------------------------------------------------------------
CREATE TABLE stg_stop_times (
    src_line_no         INTEGER,
    trip_id             TEXT,
    arrival_time        TEXT,
    departure_time      TEXT,
    stop_id             TEXT,
    stop_sequence       TEXT,
    stop_headsign       TEXT,
    pickup_type         TEXT,
    drop_off_type       TEXT,
    shape_dist_traveled TEXT,
    timepoint           TEXT
);

-- --------------------------------------------------------------------------
-- calendar.txt — NOTE: in this feed every weekday flag is 0 for all 51 593
--                services. The operating pattern is expressed *entirely*
--                through calendar_dates.txt. See docs/data_quality.md (DQ-01).
-- --------------------------------------------------------------------------
CREATE TABLE stg_calendar (
    src_line_no INTEGER,
    service_id  TEXT,
    monday      TEXT,
    tuesday     TEXT,
    wednesday   TEXT,
    thursday    TEXT,
    friday      TEXT,
    saturday    TEXT,
    sunday      TEXT,
    start_date  TEXT,
    end_date    TEXT
);

-- --------------------------------------------------------------------------
-- calendar_dates.txt — ~4.7 M rows, exception_type=1 (ADDED) throughout.
--                      This is the real calendar for this feed.
-- --------------------------------------------------------------------------
CREATE TABLE stg_calendar_dates (
    src_line_no    INTEGER,
    service_id     TEXT,
    date           TEXT,
    exception_type TEXT
);

-- --------------------------------------------------------------------------
-- transfers.txt — minimum connection times between boarding points
-- --------------------------------------------------------------------------
CREATE TABLE stg_transfers (
    src_line_no      INTEGER,
    from_stop_id     TEXT,
    to_stop_id       TEXT,
    transfer_type    TEXT,
    min_transfer_time TEXT,
    from_route_id    TEXT,
    to_route_id      TEXT,
    from_trip_id     TEXT,
    to_trip_id       TEXT
);

-- --------------------------------------------------------------------------
-- translations.txt — Belgium is trilingual. The feed is published in French
--                    (feed_lang='fr') and carries nl/de/en translations.
--                    record_id is empty throughout, so translations are keyed
--                    by *value* rather than by row id. See DQ-07.
-- --------------------------------------------------------------------------
CREATE TABLE stg_translations (
    src_line_no   INTEGER,
    table_name    TEXT,
    field_name    TEXT,
    language      TEXT,
    translation   TEXT,
    record_id     TEXT,
    record_sub_id TEXT,
    field_value   TEXT
);
