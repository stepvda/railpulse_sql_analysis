-- ===========================================================================
-- RailPulse — 02_schema.sql
-- The normalised core model (3NF) plus its reference/lookup tables.
-- ===========================================================================
--
-- ⓘ NEW TO THIS DOMAIN? Read docs/glossary.md first. It defines every term
--   used below — GTFS, trip, route, headsign, service calendar, stop_time,
--   platform vs station, 3NF, transitive dependency, fact vs dimension table,
--   WITHOUT ROWID, SARGable — each with an example from this data. Five
--   minutes there will save you twenty here.
--
-- THE ONE-PARAGRAPH VERSION, FOR SOMEONE WHO HAS NEVER SEEN GTFS
--
--   Belgian Railways publishes its timetable as a ZIP of CSV files. In it, a
--   ROUTE is a named service pattern ("IC", "S5"). A TRIP is one journey by one
--   vehicle on one route. A SERVICE is a pattern of dates telling you which
--   days a trip runs. A STOP_TIME is one trip stopping at one platform once —
--   there are 2.17 million of those, and it is the table everything is really
--   about. GTFS keeps stations and platforms in one file with a type flag; we
--   split them into STATION and PLATFORM, because they are different things.
--
-- READ THIS FIRST — THE SHAPE OF THE MODEL
--
--   Reference (static code lists, hand-seeded at the bottom of this file)
--     ref_location_type · ref_route_type · ref_pickup_drop · ref_accessibility
--     ref_exception_type · ref_transfer_type
--
--   Dimensions (the "things")
--     agency ─< route ─< trip >─ service ─< service_date
--     station ─< platform
--     text_translation   (nl / de / en names for stations and headsigns)
--
--   Fact (the "events")
--     stop_time   — one call of one trip at one platform  (~2.2 M rows)
--
--   Operational
--     feed_info · ingestion_run · rejected_row
--
-- WHY station AND platform ARE SEPARATE TABLES
-- GTFS ships stations and platforms interleaved in a single stops.txt with a
-- `location_type` discriminator — two different grains in one file, which is
-- exactly what normalisation forbids. Splitting them gives us:
--   * a real 1-to-many key (a station has many platforms),
--   * `station_name` stored exactly once (verified: in this feed a child stop's
--     stop_name is *always* identical to its parent's, so keeping it on the
--     child would be a pure transitive dependency — a 3NF violation),
--   * a natural home for the Q2 "busiest platform" analysis.
--
-- CONVENTIONS
--   * Dates are ISO-8601 TEXT 'YYYY-MM-DD'. GTFS ships 'YYYYMMDD'; the ISO form
--     is what SQLite's date functions understand.
--   * Clock times keep their GTFS TEXT form (which legitimately exceeds 24:00:00
--     for trips running past midnight) *and* gain integer companions —
--     see the long comment on `stop_time`.
--   * Every code column carries a FOREIGN KEY into a ref_ table, so an
--     unexpected code from a future feed fails loudly at load time instead of
--     silently skewing an average.
--   * SQLite only enforces foreign keys when `PRAGMA foreign_keys = ON`, which
--     src/railpulse/db.py sets on every single connection.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Drop in reverse dependency order so FK constraints never block the rebuild.
-- ---------------------------------------------------------------------------
DROP VIEW  IF EXISTS v_departure;
DROP VIEW  IF EXISTS v_trip_origin;
DROP VIEW  IF EXISTS v_service_frequency;
DROP VIEW  IF EXISTS v_station_daily_departures;
DROP VIEW  IF EXISTS v_trip_amenity;
DROP VIEW  IF EXISTS v_rt_departure_performance;

DROP TABLE IF EXISTS text_translation;
DROP TABLE IF EXISTS transfer;
DROP TABLE IF EXISTS stop_time;
DROP TABLE IF EXISTS trip;
DROP TABLE IF EXISTS service_date;
DROP TABLE IF EXISTS service;
DROP TABLE IF EXISTS route;
DROP TABLE IF EXISTS platform;
DROP TABLE IF EXISTS station;
DROP TABLE IF EXISTS agency;
DROP TABLE IF EXISTS feed_info;
-- NOTE: `ingestion_run` and `rejected_row` are deliberately absent from this
-- list. They are the build's own audit log; wiping them on every rebuild would
-- destroy the record of what previous loads did. They are created with
-- IF NOT EXISTS below, and 03_transform.sql clears only the rows belonging to
-- the static feed it is about to reload.
DROP TABLE IF EXISTS ref_transfer_type;
DROP TABLE IF EXISTS ref_exception_type;
DROP TABLE IF EXISTS ref_accessibility;
DROP TABLE IF EXISTS ref_pickup_drop;
DROP TABLE IF EXISTS ref_route_type;
DROP TABLE IF EXISTS ref_location_type;


-- ===========================================================================
-- 1. REFERENCE TABLES
-- ---------------------------------------------------------------------------
-- GTFS encodes meaning as bare integers. Storing "1" in `bikes_allowed` and
-- hoping the reader remembers what it means is how analyses go wrong. Each of
-- these tables turns a magic number into a joinable, self-documenting label and
-- — because the fact columns REFERENCE them — into a validity constraint.
-- ===========================================================================

CREATE TABLE ref_location_type (
    location_type INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    description   TEXT NOT NULL
);

CREATE TABLE ref_route_type (
    route_type  INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE ref_pickup_drop (
    code        INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT NOT NULL
);

-- Shared by trip.bikes_allowed, trip.wheelchair_accessible and
-- station.wheelchair_boarding: GTFS uses the same 0/1/2 vocabulary for all
-- three. Note that 0 means "no information", NOT "no" — conflating the two is
-- the single most common error in accessibility reporting, and Q5 depends on
-- getting it right.
CREATE TABLE ref_accessibility (
    code        INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT NOT NULL,
    -- 1 only for codes that are an explicit positive guarantee. Q5 aggregates
    -- on this flag rather than hard-coding `= 1` in five different queries.
    is_guaranteed INTEGER NOT NULL CHECK (is_guaranteed IN (0, 1))
);

CREATE TABLE ref_exception_type (
    exception_type INTEGER PRIMARY KEY,
    label          TEXT NOT NULL,
    description    TEXT NOT NULL
);

CREATE TABLE ref_transfer_type (
    transfer_type INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    description   TEXT NOT NULL
);


-- ===========================================================================
-- 2. OPERATIONAL / PROVENANCE TABLES
-- ---------------------------------------------------------------------------
-- A database you cannot audit is a database you cannot trust. These three
-- tables answer "where did this row come from, when, and what was thrown away".
-- ===========================================================================

-- One row per GTFS feed loaded. `feed_start_date`/`feed_end_date` bound the
-- timetable and are quoted in every report so a number is never undated.
CREATE TABLE feed_info (
    feed_id             TEXT PRIMARY KEY,
    feed_publisher_name TEXT NOT NULL,
    feed_publisher_url  TEXT,
    feed_lang           TEXT,
    feed_start_date     TEXT,            -- ISO 'YYYY-MM-DD'
    feed_end_date       TEXT,
    feed_version        TEXT,
    CHECK (feed_start_date IS NULL OR feed_start_date LIKE '____-__-__'),
    CHECK (feed_end_date   IS NULL OR feed_end_date   LIKE '____-__-__')
);

-- One row per execution of the ingestion job: what was fetched, from where,
-- how big it was, and how long it took. Makes reloads reproducible and lets
-- the dashboard print "data as of ...".
CREATE TABLE IF NOT EXISTS ingestion_run (
    run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at_utc    TEXT NOT NULL,
    finished_at_utc   TEXT,
    source            TEXT NOT NULL,     -- 'gtfs-static' | 'gtfs-rt-trip-update' | ...
    source_url        TEXT,
    http_status       INTEGER,
    bytes_downloaded  INTEGER,
    source_last_modified TEXT,           -- upstream Last-Modified header
    rows_staged       INTEGER,
    rows_loaded       INTEGER,
    rows_rejected     INTEGER,
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'ok', 'failed')),
    notes             TEXT
);

-- The quarantine. A row that cannot satisfy the core model's constraints is
-- never silently dropped: it lands here with the file, line number, rule that
-- rejected it, and the original payload as JSON.
CREATE TABLE IF NOT EXISTS rejected_row (
    rejected_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER REFERENCES ingestion_run(run_id),
    source_table TEXT NOT NULL,          -- e.g. 'stg_stop_times'
    src_line_no  INTEGER,
    rule_code    TEXT NOT NULL,          -- e.g. 'DQ-03-IMPLAUSIBLE-DEPARTURE'
    reason       TEXT NOT NULL,
    payload      TEXT                    -- JSON snapshot of the offending row
);


-- ===========================================================================
-- 3. DIMENSIONS
-- ===========================================================================

-- --------------------------------------------------------------------------
-- agency — the transport operator. One row (NMBS/SNCB) today; the column is
-- still a real FK so the model extends to De Lijn / TEC / STIB without change.
-- --------------------------------------------------------------------------
CREATE TABLE agency (
    agency_id       TEXT PRIMARY KEY,
    agency_name     TEXT NOT NULL,
    agency_url      TEXT,
    agency_timezone TEXT NOT NULL,
    agency_lang     TEXT,
    agency_phone    TEXT
);

-- --------------------------------------------------------------------------
-- station — a named rail hub (GTFS location_type = 1). 652 rows.
-- This is the level a passenger means by "Bruxelles-Central".
-- --------------------------------------------------------------------------
CREATE TABLE station (
    station_id          TEXT PRIMARY KEY,
    station_name        TEXT NOT NULL,
    latitude            REAL,
    longitude           REAL,
    wheelchair_boarding INTEGER NOT NULL DEFAULT 0
                        REFERENCES ref_accessibility(code),
    CHECK (latitude  IS NULL OR latitude  BETWEEN  -90 AND  90),
    CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180)
);

-- --------------------------------------------------------------------------
-- platform — a boarding point inside a station (GTFS location_type = 0).
--            2 243 rows: 1 591 carry a real platform number, and each station
--            additionally owns exactly one platform_code IS NULL row used by
--            the feed for calls where no track has been allocated.
--
-- `station_name` is deliberately ABSENT: it is functionally dependent on
-- station_id, not on stop_id. Queries join through to `station`.
-- --------------------------------------------------------------------------
CREATE TABLE platform (
    stop_id       TEXT PRIMARY KEY,
    station_id    TEXT NOT NULL REFERENCES station(station_id),
    platform_code TEXT,               -- NULL = feed allocated no track
    latitude      REAL,
    longitude     REAL,
    stop_desc     TEXT,               -- 'NMBSSNCB RAIL PLATFORM' / 'RAIL+BUS ...'
    -- A small-cardinality integer flag for "real, numbered platform" (Q2).
    -- Note: `WHERE platform_code IS NOT NULL` would ALSO be SARGable (a range
    -- seek), so this is not a SARGability fix. Its value is that a 0/1 integer
    -- composes cleanly into the FRONT of the composite covering index
    -- ix_platform_station (station_id, has_platform_code, platform_code), which
    -- a nullable TEXT column range-scanned does not do as tidily.
    has_platform_code INTEGER NOT NULL CHECK (has_platform_code IN (0, 1)),
    UNIQUE (station_id, platform_code)
);

-- --------------------------------------------------------------------------
-- route — a commercial line. 1 801 rows (1 531 rail, 270 replacement bus).
-- --------------------------------------------------------------------------
CREATE TABLE route (
    route_id         TEXT PRIMARY KEY,
    agency_id        TEXT NOT NULL REFERENCES agency(agency_id),
    route_short_name TEXT,            -- 'IC', 'S5', 'L', 'P', 'BUS' ...
    route_long_name  TEXT,            -- 'Louvain -- La Panne'
    route_type       INTEGER NOT NULL REFERENCES ref_route_type(route_type),
    route_color      TEXT,
    route_text_color TEXT
);

-- --------------------------------------------------------------------------
-- service — a calendar pattern referenced by trips (GTFS calendar.txt).
--
-- ⚠ In this feed all seven weekday flags are 0 for all 51 593 services and the
--   real pattern lives in service_date. The columns are retained because they
--   are part of the GTFS contract and a future feed may populate them, but
--   `has_weekday_pattern` records whether they can be trusted, and Q4 derives
--   the weekly rhythm from service_date instead. See docs/data_quality.md DQ-01.
-- --------------------------------------------------------------------------
CREATE TABLE service (
    service_id  TEXT PRIMARY KEY,
    start_date  TEXT NOT NULL,        -- ISO
    end_date    TEXT NOT NULL,        -- ISO
    monday      INTEGER NOT NULL CHECK (monday    IN (0, 1)),
    tuesday     INTEGER NOT NULL CHECK (tuesday   IN (0, 1)),
    wednesday   INTEGER NOT NULL CHECK (wednesday IN (0, 1)),
    thursday    INTEGER NOT NULL CHECK (thursday  IN (0, 1)),
    friday      INTEGER NOT NULL CHECK (friday    IN (0, 1)),
    saturday    INTEGER NOT NULL CHECK (saturday  IN (0, 1)),
    sunday      INTEGER NOT NULL CHECK (sunday    IN (0, 1)),
    -- 1 when at least one weekday flag is set, i.e. the GTFS weekly pattern is
    -- usable. 0 for every row in the current SNCB feed.
    has_weekday_pattern INTEGER NOT NULL CHECK (has_weekday_pattern IN (0, 1)),
    CHECK (start_date LIKE '____-__-__'),
    CHECK (end_date   LIKE '____-__-__'),
    CHECK (end_date >= start_date)
);

-- --------------------------------------------------------------------------
-- service_date — the exploded operating calendar (GTFS calendar_dates.txt).
--                ~4.7 M rows, one per (service, calendar day).
--
-- `day_of_week` is materialised at load time rather than computed per query.
-- Q4 groups ~4.7 M rows by weekday; calling strftime('%w', …) there would cost
-- a function call per row *and* make any index on the column unusable. This is
-- the SARGability lesson from the study guide, applied.
--   0 = Sunday … 6 = Saturday  (SQLite's strftime('%w') convention)
-- --------------------------------------------------------------------------
CREATE TABLE service_date (
    service_id     TEXT    NOT NULL REFERENCES service(service_id),
    service_date   TEXT    NOT NULL,   -- ISO 'YYYY-MM-DD'
    exception_type INTEGER NOT NULL REFERENCES ref_exception_type(exception_type),
    day_of_week    INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    PRIMARY KEY (service_id, service_date),
    CHECK (service_date LIKE '____-__-__')
) WITHOUT ROWID;
-- WITHOUT ROWID: rows are stored inside the primary-key B-tree instead of in a
-- separate table addressed by a hidden rowid. Worth it here because the two key
-- columns are most of the row — a normal table would additionally carry a
-- separate PK index holding service_id + service_date + rowid for all 4.7 M
-- rows, on the order of 150 MB by arithmetic on the column widths. It also
-- removes one B-tree hop from every lookup.

-- --------------------------------------------------------------------------
-- trip — one vehicle journey: a route, running on a service calendar,
--        towards a headsign destination. 134 809 rows.
-- --------------------------------------------------------------------------
CREATE TABLE trip (
    trip_id               TEXT PRIMARY KEY,
    route_id              TEXT NOT NULL REFERENCES route(route_id),
    service_id            TEXT NOT NULL REFERENCES service(service_id),
    trip_headsign         TEXT,        -- terminal destination shown to passengers
    trip_short_name       TEXT,        -- public train number, e.g. 'IC 1832'
    -- GTFS block: trips sharing a block_id are worked by the same physical
    -- vehicle in sequence, so a passenger can stay aboard from one to the next
    -- without changing trains. Populated on every trip in this feed; carried for
    -- completeness, not used by any Sprint-1 analysis.
    block_id              TEXT,
    direction_id          INTEGER CHECK (direction_id IN (0, 1)),
    -- GTFS 0/1/2 vocabulary; 0 = "no information" (NOT "no"). See DQ-02.
    bikes_allowed         INTEGER NOT NULL DEFAULT 0
                          REFERENCES ref_accessibility(code),
    wheelchair_accessible INTEGER NOT NULL DEFAULT 0
                          REFERENCES ref_accessibility(code)
);

-- --------------------------------------------------------------------------
-- transfer — minimum connection times. 733 rows, all transfer_type = 2.
--            659 are self-transfers (platform-to-platform inside one station).
-- --------------------------------------------------------------------------
CREATE TABLE transfer (
    transfer_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    from_stop_id      TEXT NOT NULL REFERENCES platform(stop_id),
    to_stop_id        TEXT NOT NULL REFERENCES platform(stop_id),
    transfer_type     INTEGER NOT NULL REFERENCES ref_transfer_type(transfer_type),
    min_transfer_time INTEGER CHECK (min_transfer_time IS NULL OR min_transfer_time >= 0),
    from_trip_id      TEXT REFERENCES trip(trip_id),
    to_trip_id        TEXT REFERENCES trip(trip_id),
    -- A surrogate PK is used because the GTFS natural key
    -- (from_stop_id, to_stop_id) is not unique once trip-scoped transfers exist.
    UNIQUE (from_stop_id, to_stop_id, from_trip_id, to_trip_id)
);

-- --------------------------------------------------------------------------
-- text_translation — nl / de / en labels for station names and headsigns.
--
-- The feed leaves `record_id` empty on every row, so translations are keyed by
-- the French *value* rather than by a row id (a GTFS-permitted but lossy
-- pattern — see DQ-07). The PK therefore has to be the value tuple.
-- --------------------------------------------------------------------------
CREATE TABLE text_translation (
    table_name  TEXT NOT NULL CHECK (table_name IN ('stops', 'trips')),
    field_name  TEXT NOT NULL,       -- 'stop_name' | 'trip_headsign'
    field_value TEXT NOT NULL,       -- the French source string
    language    TEXT NOT NULL CHECK (language IN ('nl', 'de', 'en', 'fr')),
    translation TEXT NOT NULL,
    PRIMARY KEY (table_name, field_name, field_value, language)
) WITHOUT ROWID;


-- ===========================================================================
-- 4. THE FACT TABLE
-- ===========================================================================
-- stop_time — one scheduled call of one trip at one platform. ~2.2 M rows.
--
-- THE THREE DERIVED TIME COLUMNS, AND WHY THEY EXIST
--
-- GTFS clock times are *service-relative*: a train leaving at 00:20 on the
-- night of a Saturday service is published as "24:20:00", and this feed really
-- does contain 31 154 such calls. Three different questions need three
-- different shapes of that value, so the transform materialises all three:
--
--   departure_time  TEXT     '24:20:00'  — the untouched GTFS value, kept so
--                                          the row can always be traced back.
--   departure_secs  INTEGER  87600       — seconds since the service day began.
--                                          Correct for ordering and durations.
--   departure_hour  INTEGER  0           — the clock hour a passenger would
--                                          read on the platform: (secs/3600)%24.
--
-- Q1 ("which hour is busiest?") must use `departure_hour`. Using the raw text
-- would scatter after-midnight departures into fictional hours 24-25, and
-- computing it per query — WHERE/GROUP BY strftime(...) — would be a SARGable
-- violation over 2.2 M rows.
--
-- `is_boardable` is the other analytical shortcut. 577 k calls in this feed are
-- technical pass-throughs (pickup_type = 1 AND drop_off_type = 1): the train
-- physically passes the platform but no passenger may use it. Counting those
-- as "departures" would overstate the network by 49 %, and unevenly: it adds
-- 74.2 % at Anvers-Central and 0.1 % at Bruxelles-Central, because a
-- pass-through is a train with no commercial business at that station. The
-- distortion reorders hubs rather than just scaling them.
-- ---------------------------------------------------------------------------
CREATE TABLE stop_time (
    trip_id        TEXT    NOT NULL REFERENCES trip(trip_id),
    stop_sequence  INTEGER NOT NULL,
    stop_id        TEXT    NOT NULL REFERENCES platform(stop_id),

    arrival_time   TEXT,                       -- raw GTFS, may exceed 24:00:00
    departure_time TEXT,                       -- raw GTFS, may exceed 24:00:00
    arrival_secs   INTEGER CHECK (arrival_secs   IS NULL OR arrival_secs   >= 0),
    departure_secs INTEGER CHECK (departure_secs IS NULL OR departure_secs >= 0),
    departure_hour INTEGER CHECK (departure_hour IS NULL OR departure_hour BETWEEN 0 AND 23),
    arrival_hour   INTEGER CHECK (arrival_hour   IS NULL OR arrival_hour   BETWEEN 0 AND 23),
    -- 0 for a same-day call, 1 for 24:00-47:59, 2 for 48:00+ — how many
    -- calendar days after the service date the call actually happens.
    day_offset     INTEGER NOT NULL DEFAULT 0 CHECK (day_offset >= 0),

    pickup_type    INTEGER NOT NULL REFERENCES ref_pickup_drop(code),
    drop_off_type  INTEGER NOT NULL REFERENCES ref_pickup_drop(code),
    stop_headsign  TEXT,

    -- 1 when a passenger may actually board here (pickup_type <> 1).
    is_boardable   INTEGER NOT NULL CHECK (is_boardable IN (0, 1)),
    -- 1 when a passenger may actually alight here (drop_off_type <> 1).
    is_alightable  INTEGER NOT NULL CHECK (is_alightable IN (0, 1)),

    PRIMARY KEY (trip_id, stop_sequence)
);


-- ===========================================================================
-- 5. REFERENCE DATA
-- ---------------------------------------------------------------------------
-- Seeded from the GTFS Schedule Reference. Loaded before the transform runs,
-- because every FK in the core model points here.
-- ===========================================================================

INSERT INTO ref_location_type (location_type, label, description) VALUES
    (0, 'Stop/Platform', 'A boarding point where passengers board or alight'),
    (1, 'Station',       'A physical structure containing one or more platforms'),
    (2, 'Entrance/Exit', 'A location where passengers enter or leave a station'),
    (3, 'Generic Node',  'A location within a station used to link pathways'),
    (4, 'Boarding Area', 'A specific location on a platform');

INSERT INTO ref_route_type (route_type, label, description) VALUES
    (0,  'Tram',       'Tram, streetcar or light rail within a city'),
    (1,  'Metro',      'Underground rail system within a city'),
    (2,  'Rail',       'Intercity or long-distance rail — the SNCB core network'),
    (3,  'Bus',        'Short- and long-distance bus, incl. rail replacement'),
    (4,  'Ferry',      'Boat service'),
    (5,  'Cable Tram', 'Street-level rail with a cable running beneath'),
    (6,  'Aerial Lift','Cable car, gondola or aerial tramway'),
    (7,  'Funicular',  'Rail system designed for steep inclines'),
    (11, 'Trolleybus', 'Electric bus drawing power from overhead wires'),
    (12, 'Monorail',   'Railway in which the track consists of a single rail');

INSERT INTO ref_pickup_drop (code, label, description) VALUES
    (0, 'Regularly scheduled', 'Normal commercial pick-up / drop-off'),
    (1, 'Not available',       'No boarding/alighting — a technical pass-through'),
    (2, 'Phone agency',        'Must phone the agency to arrange'),
    (3, 'Coordinate with driver', 'Must coordinate with the driver');

INSERT INTO ref_accessibility (code, label, description, is_guaranteed) VALUES
    (0, 'No information', 'The feed makes no statement — this is NOT a "no"', 0),
    (1, 'Yes',            'Explicitly accommodated / guaranteed',              1),
    (2, 'No',             'Explicitly not accommodated',                       0);

INSERT INTO ref_exception_type (exception_type, label, description) VALUES
    (1, 'Added',   'Service has been ADDED for the specified date'),
    (2, 'Removed', 'Service has been REMOVED for the specified date');

INSERT INTO ref_transfer_type (transfer_type, label, description) VALUES
    (0, 'Recommended',           'Recommended transfer point'),
    (1, 'Timed',                 'Departing vehicle waits for the arriving one'),
    (2, 'Minimum time required', 'A minimum transfer time is needed'),
    (3, 'Not possible',          'Transfer is not possible here');
