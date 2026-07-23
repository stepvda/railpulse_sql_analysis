-- ===========================================================================
-- RailPulse — 03_transform.sql
-- staging (raw TEXT)  ->  normalised core model.  Pure SQL, no Python logic.
-- ===========================================================================
-- This file is the entire cleaning layer. Nine rules are applied; each one is
-- tagged DQ-nn and documented in docs/data_quality.md. Anything a rule refuses
-- is written to `rejected_row` — never dropped silently.
--
--   DQ-01  calendar.txt weekday flags are all zero -> flag it, derive the real
--          weekly rhythm from calendar_dates instead (see v_service_frequency).
--   DQ-02  Empty accessibility codes mean "no information" (0), not "no" (2).
--   DQ-03  Calls at 48:00:00 or later are physically implausible -> quarantine.
--   DQ-04  Calls referencing an unknown trip or platform -> quarantine.
--   DQ-05  Duplicate (trip_id, stop_sequence) -> keep the first, quarantine
--          the rest.
--   DQ-06  'YYYYMMDD' -> ISO 'YYYY-MM-DD'; feed_info ships a leading space.
--   DQ-07  Translations are keyed by value (record_id is empty throughout).
--   DQ-08  Empty strings are NOT the same as NULL; normalise them.
--   DQ-09  Trips referencing an unknown route or service -> quarantine.
--
-- The whole file runs inside one transaction (opened by src/railpulse/db.py):
-- either the core model is fully rebuilt or nothing changes. That is the 'A'
-- of ACID doing real work — see SQL&DB_theory.md §4.
-- ===========================================================================


-- ===========================================================================
-- 0. Clear the quarantine entries belonging to the static feed.
-- ---------------------------------------------------------------------------
-- `rejected_row` survives rebuilds (it is the audit log), but the rows this
-- run is about to re-derive must not pile up on top of the previous run's.
-- Real-time quarantine entries, which are not reproducible, are left alone.
-- ===========================================================================
DELETE FROM rejected_row WHERE source_table LIKE 'stg%';


-- ===========================================================================
-- 1. feed_info  — provenance first, so every later report can be dated.
-- ---------------------------------------------------------------------------
-- DQ-06: this feed publishes ' 20251220' (leading space) for the validity
-- window, so TRIM before slicing into ISO form.
-- ===========================================================================
INSERT INTO feed_info (
    feed_id, feed_publisher_name, feed_publisher_url, feed_lang,
    feed_start_date, feed_end_date, feed_version
)
SELECT
    TRIM(feed_id),
    TRIM(feed_publisher_name),
    NULLIF(TRIM(feed_publisher_url), ''),
    NULLIF(TRIM(feed_lang), ''),
    CASE WHEN LENGTH(TRIM(feed_start_date)) = 8
         THEN substr(TRIM(feed_start_date), 1, 4) || '-' ||
              substr(TRIM(feed_start_date), 5, 2) || '-' ||
              substr(TRIM(feed_start_date), 7, 2) END,
    CASE WHEN LENGTH(TRIM(feed_end_date)) = 8
         THEN substr(TRIM(feed_end_date), 1, 4) || '-' ||
              substr(TRIM(feed_end_date), 5, 2) || '-' ||
              substr(TRIM(feed_end_date), 7, 2) END,
    NULLIF(TRIM(feed_version), '')
FROM stg_feed_info;


-- ===========================================================================
-- 2. agency
-- ===========================================================================
INSERT INTO agency (agency_id, agency_name, agency_url, agency_timezone,
                    agency_lang, agency_phone)
SELECT
    TRIM(agency_id),
    TRIM(agency_name),
    NULLIF(TRIM(agency_url), ''),
    TRIM(agency_timezone),
    NULLIF(TRIM(agency_lang), ''),
    NULLIF(TRIM(agency_phone), '')       -- DQ-08
FROM stg_agency
WHERE TRIM(COALESCE(agency_id, '')) <> '';


-- ===========================================================================
-- 3. station  <- stops.txt WHERE location_type = 1
-- ===========================================================================
INSERT INTO station (station_id, station_name, latitude, longitude,
                     wheelchair_boarding)
SELECT
    TRIM(stop_id),
    TRIM(stop_name),
    CAST(NULLIF(TRIM(stop_lat), '') AS REAL),
    CAST(NULLIF(TRIM(stop_lon), '') AS REAL),
    -- DQ-02: an empty accessibility code is "no information" (0), not "no".
    COALESCE(CAST(NULLIF(TRIM(wheelchair_boarding), '') AS INTEGER), 0)
FROM stg_stops
WHERE CAST(NULLIF(TRIM(location_type), '') AS INTEGER) = 1;


-- ===========================================================================
-- 4. platform  <- stops.txt WHERE location_type = 0
-- ---------------------------------------------------------------------------
-- Note the deliberate omission of stop_name: it is always identical to the
-- parent station's name in this feed (verified: 0 of 2 243 children differ),
-- so storing it here would be a transitive dependency and a 3NF violation.
-- ===========================================================================
INSERT INTO platform (stop_id, station_id, platform_code, latitude, longitude,
                      stop_desc, has_platform_code)
SELECT
    TRIM(s.stop_id),
    TRIM(s.parent_station),
    NULLIF(TRIM(s.platform_code), ''),                 -- DQ-08
    CAST(NULLIF(TRIM(s.stop_lat), '') AS REAL),
    CAST(NULLIF(TRIM(s.stop_lon), '') AS REAL),
    NULLIF(TRIM(s.stop_desc), ''),
    CASE WHEN TRIM(COALESCE(s.platform_code, '')) <> '' THEN 1 ELSE 0 END
FROM stg_stops s
JOIN station st ON st.station_id = TRIM(s.parent_station)   -- DQ-04
WHERE CAST(NULLIF(TRIM(s.location_type), '') AS INTEGER) = 0;

-- DQ-04: quarantine any boarding point whose parent station is missing.
INSERT INTO rejected_row (source_table, src_line_no, rule_code, reason, payload)
SELECT
    'stg_stops', s.src_line_no, 'DQ-04-ORPHAN-PLATFORM',
    'location_type=0 row references a parent_station that is not in the feed',
    json_object('stop_id', s.stop_id, 'parent_station', s.parent_station,
                'stop_name', s.stop_name)
FROM stg_stops s
LEFT JOIN station st ON st.station_id = TRIM(s.parent_station)
WHERE CAST(NULLIF(TRIM(s.location_type), '') AS INTEGER) = 0
  AND st.station_id IS NULL;


-- ===========================================================================
-- 5. route
-- ---------------------------------------------------------------------------
-- route_desc, route_url and route_sort_order are empty for all 1 801 rows and
-- are therefore not carried into the core model (documented in DQ-08).
-- ===========================================================================
INSERT INTO route (route_id, agency_id, route_short_name, route_long_name,
                   route_type, route_color, route_text_color)
SELECT
    TRIM(r.route_id),
    TRIM(r.agency_id),
    NULLIF(TRIM(r.route_short_name), ''),
    NULLIF(TRIM(r.route_long_name), ''),
    CAST(TRIM(r.route_type) AS INTEGER),
    NULLIF(TRIM(r.route_color), ''),
    NULLIF(TRIM(r.route_text_color), '')
FROM stg_routes r
JOIN agency a ON a.agency_id = TRIM(r.agency_id);


-- ===========================================================================
-- 6. service  <- calendar.txt
-- ---------------------------------------------------------------------------
-- DQ-01: every weekday flag is 0 in this feed. `has_weekday_pattern` records
-- that fact per row so downstream code can never silently trust a column that
-- carries no signal. Q4 therefore derives frequency from service_date.
-- ===========================================================================
INSERT INTO service (service_id, start_date, end_date,
                     monday, tuesday, wednesday, thursday, friday, saturday, sunday,
                     has_weekday_pattern)
SELECT
    TRIM(c.service_id),
    substr(TRIM(c.start_date), 1, 4) || '-' || substr(TRIM(c.start_date), 5, 2)
        || '-' || substr(TRIM(c.start_date), 7, 2),          -- DQ-06
    substr(TRIM(c.end_date), 1, 4) || '-' || substr(TRIM(c.end_date), 5, 2)
        || '-' || substr(TRIM(c.end_date), 7, 2),
    COALESCE(CAST(NULLIF(TRIM(c.monday),    '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(c.tuesday),   '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(c.wednesday), '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(c.thursday),  '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(c.friday),    '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(c.saturday),  '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(c.sunday),    '') AS INTEGER), 0),
    CASE WHEN COALESCE(CAST(NULLIF(TRIM(c.monday),    '') AS INTEGER), 0)
            + COALESCE(CAST(NULLIF(TRIM(c.tuesday),   '') AS INTEGER), 0)
            + COALESCE(CAST(NULLIF(TRIM(c.wednesday), '') AS INTEGER), 0)
            + COALESCE(CAST(NULLIF(TRIM(c.thursday),  '') AS INTEGER), 0)
            + COALESCE(CAST(NULLIF(TRIM(c.friday),    '') AS INTEGER), 0)
            + COALESCE(CAST(NULLIF(TRIM(c.saturday),  '') AS INTEGER), 0)
            + COALESCE(CAST(NULLIF(TRIM(c.sunday),    '') AS INTEGER), 0) > 0
         THEN 1 ELSE 0 END
FROM stg_calendar c
WHERE LENGTH(TRIM(c.start_date)) = 8
  AND LENGTH(TRIM(c.end_date))   = 8;


-- ===========================================================================
-- 7. service_date  <- calendar_dates.txt   (~4.7 M rows)
-- ---------------------------------------------------------------------------
-- `day_of_week` is materialised here, once, rather than being recomputed by
-- every analytical query. See the note on the table definition in 02_schema.sql.
-- ===========================================================================
-- A calendar_dates row that repeats a (service_id, date) pair would violate the
-- primary key. OR IGNORE keeps the first occurrence; the pass underneath then
-- records exactly which pairs were duplicated, so the quarantine stays honest.
INSERT OR IGNORE INTO service_date (service_id, service_date, exception_type, day_of_week)
SELECT
    TRIM(cd.service_id),
    substr(TRIM(cd.date), 1, 4) || '-' || substr(TRIM(cd.date), 5, 2)
        || '-' || substr(TRIM(cd.date), 7, 2),                -- DQ-06
    CAST(TRIM(cd.exception_type) AS INTEGER),
    CAST(strftime('%w',
        substr(TRIM(cd.date), 1, 4) || '-' || substr(TRIM(cd.date), 5, 2)
            || '-' || substr(TRIM(cd.date), 7, 2)) AS INTEGER)
FROM stg_calendar_dates cd
JOIN service s ON s.service_id = TRIM(cd.service_id)          -- DQ-04
WHERE LENGTH(TRIM(cd.date)) = 8;

-- DQ-04: service days pointing at a service_id that calendar.txt never declared.
INSERT INTO rejected_row (source_table, src_line_no, rule_code, reason, payload)
SELECT
    'stg_calendar_dates', cd.src_line_no, 'DQ-04-ORPHAN-SERVICE-DATE',
    'calendar_dates row references a service_id absent from calendar.txt',
    json_object('service_id', cd.service_id, 'date', cd.date)
FROM stg_calendar_dates cd
LEFT JOIN service s ON s.service_id = TRIM(cd.service_id)
WHERE s.service_id IS NULL
   OR LENGTH(TRIM(cd.date)) <> 8;

-- DQ-05: repeated (service_id, date) pairs. One summary row per duplicated key.
INSERT INTO rejected_row (source_table, src_line_no, rule_code, reason, payload)
SELECT
    'stg_calendar_dates', MIN(cd.src_line_no), 'DQ-05-DUPLICATE-SERVICE-DATE',
    'duplicate (service_id, date); first occurrence kept',
    json_object('service_id', TRIM(cd.service_id), 'date', TRIM(cd.date),
                'occurrences', COUNT(*))
FROM stg_calendar_dates cd
GROUP BY TRIM(cd.service_id), TRIM(cd.date)
HAVING COUNT(*) > 1;


-- ===========================================================================
-- 8. trip
-- ---------------------------------------------------------------------------
-- DQ-02 again: bikes_allowed / wheelchair_accessible default to 0 = "no
-- information". In this feed wheelchair_accessible is empty for all 134 809
-- trips, which is itself the headline finding of Q5.
-- ===========================================================================
INSERT INTO trip (trip_id, route_id, service_id, trip_headsign, trip_short_name,
                  block_id, direction_id, bikes_allowed, wheelchair_accessible)
SELECT
    TRIM(t.trip_id),
    TRIM(t.route_id),
    TRIM(t.service_id),
    NULLIF(TRIM(t.trip_headsign), ''),
    NULLIF(TRIM(t.trip_short_name), ''),
    NULLIF(TRIM(t.block_id), ''),
    CAST(NULLIF(TRIM(t.direction_id), '') AS INTEGER),
    COALESCE(CAST(NULLIF(TRIM(t.bikes_allowed), '') AS INTEGER), 0),
    COALESCE(CAST(NULLIF(TRIM(t.wheelchair_accessible), '') AS INTEGER), 0)
FROM stg_trips t
JOIN route   r ON r.route_id   = TRIM(t.route_id)
JOIN service s ON s.service_id = TRIM(t.service_id);

-- DQ-09: trips whose route or service is missing from the feed.
INSERT INTO rejected_row (source_table, src_line_no, rule_code, reason, payload)
SELECT
    'stg_trips', t.src_line_no, 'DQ-09-ORPHAN-TRIP',
    'trip references a route_id or service_id absent from the feed',
    json_object('trip_id', t.trip_id, 'route_id', t.route_id,
                'service_id', t.service_id)
FROM stg_trips t
LEFT JOIN route   r ON r.route_id   = TRIM(t.route_id)
LEFT JOIN service s ON s.service_id = TRIM(t.service_id)
WHERE r.route_id IS NULL OR s.service_id IS NULL;


-- ===========================================================================
-- 9. stop_time  — the fact table (~2.2 M rows). The only genuinely interesting
--                 transform in the file.
-- ---------------------------------------------------------------------------
-- Every clock value in this feed is exactly 8 characters, 'HH:MM:SS', where HH
-- may legitimately run past 23 (31 154 calls do). So parsing is a fixed-offset
-- substr — no regex, no date function, no per-row branch:
--
--     secs = HH*3600 + MM*60 + SS
--     day_offset     = secs / 86400            (integer division)
--     departure_hour = (secs / 3600) % 24      (the hour on the platform clock)
--
-- The `keep` CTE evaluates every rule once; the two statements below it then
-- split staging into "loaded" and "quarantined" on the same predicate, so a row
-- can never land in both or in neither.
-- ===========================================================================
CREATE TEMP TABLE tmp_stop_time_screened AS
WITH parsed AS (
    SELECT
        st.rowid                                  AS stg_rowid,
        st.src_line_no,
        TRIM(st.trip_id)                          AS trip_id,
        TRIM(st.stop_id)                          AS stop_id,
        CAST(TRIM(st.stop_sequence) AS INTEGER)   AS stop_sequence,
        -- Whether that CAST was actually meaningful. SQLite's CAST does not
        -- fail on garbage: CAST('abc' AS INTEGER) is 0, not NULL. So testing
        -- the cast result for NULL detects nothing, and a corrupt
        -- stop_sequence would load as 0 — silently becoming the trip's FIRST
        -- call and changing the origin that Q3 is built on. The raw text has
        -- to be inspected instead, which is what this GLOB does.
        CASE WHEN TRIM(COALESCE(st.stop_sequence, '')) <> ''
              AND TRIM(st.stop_sequence) NOT GLOB '*[^0-9]*'
             THEN 1 ELSE 0 END                    AS stop_sequence_is_valid,
        NULLIF(TRIM(st.arrival_time), '')         AS arrival_time,
        NULLIF(TRIM(st.departure_time), '')       AS departure_time,
        CASE WHEN LENGTH(TRIM(st.arrival_time)) = 8
             THEN CAST(substr(TRIM(st.arrival_time), 1, 2) AS INTEGER) * 3600
                + CAST(substr(TRIM(st.arrival_time), 4, 2) AS INTEGER) * 60
                + CAST(substr(TRIM(st.arrival_time), 7, 2) AS INTEGER) END
                                                  AS arrival_secs,
        CASE WHEN LENGTH(TRIM(st.departure_time)) = 8
             THEN CAST(substr(TRIM(st.departure_time), 1, 2) AS INTEGER) * 3600
                + CAST(substr(TRIM(st.departure_time), 4, 2) AS INTEGER) * 60
                + CAST(substr(TRIM(st.departure_time), 7, 2) AS INTEGER) END
                                                  AS departure_secs,
        COALESCE(CAST(NULLIF(TRIM(st.pickup_type),   '') AS INTEGER), 0) AS pickup_type,
        COALESCE(CAST(NULLIF(TRIM(st.drop_off_type), '') AS INTEGER), 0) AS drop_off_type,
        NULLIF(TRIM(st.stop_headsign), '')        AS stop_headsign
    FROM stg_stop_times st
),
screened AS (
    SELECT
        p.*,
        t.trip_id  AS fk_trip,
        pl.stop_id AS fk_platform,
        -- DQ-05: within a duplicated (trip_id, stop_sequence) group the first
        -- physical line in the file wins; the others are quarantined.
        ROW_NUMBER() OVER (PARTITION BY p.trip_id, p.stop_sequence
                           ORDER BY p.src_line_no) AS dup_rank
    FROM parsed p
    LEFT JOIN trip     t  ON t.trip_id  = p.trip_id
    LEFT JOIN platform pl ON pl.stop_id = p.stop_id
)
SELECT
    s.*,
    CASE
        WHEN s.fk_trip     IS NULL THEN 'DQ-04-ORPHAN-STOP-TIME-TRIP'
        WHEN s.fk_platform IS NULL THEN 'DQ-04-ORPHAN-STOP-TIME-PLATFORM'
        WHEN s.stop_sequence_is_valid = 0 THEN 'DQ-08-BAD-STOP-SEQUENCE'
        WHEN s.departure_secs IS NULL AND s.arrival_secs IS NULL
             THEN 'DQ-08-NO-TIME'
        -- DQ-03: GTFS permits times past 24:00:00 for trips crossing midnight,
        -- but a *rail* call 48 h into its own service day is not a timetable,
        -- it is a data error. 12 rows in this feed (up to 87:39:00).
        WHEN COALESCE(s.departure_secs, s.arrival_secs) >= 172800
             THEN 'DQ-03-IMPLAUSIBLE-DEPARTURE'
        WHEN s.dup_rank > 1 THEN 'DQ-05-DUPLICATE-CALL'
        ELSE NULL
    END AS reject_rule
FROM screened s;

INSERT INTO stop_time (
    trip_id, stop_sequence, stop_id,
    arrival_time, departure_time, arrival_secs, departure_secs,
    departure_hour, arrival_hour, day_offset,
    pickup_type, drop_off_type, stop_headsign,
    is_boardable, is_alightable
)
SELECT
    trip_id, stop_sequence, stop_id,
    arrival_time, departure_time, arrival_secs, departure_secs,
    CASE WHEN departure_secs IS NOT NULL
         THEN (departure_secs / 3600) % 24 END,
    CASE WHEN arrival_secs IS NOT NULL
         THEN (arrival_secs / 3600) % 24 END,
    COALESCE(departure_secs, arrival_secs) / 86400,
    pickup_type, drop_off_type, stop_headsign,
    CASE WHEN pickup_type   = 1 THEN 0 ELSE 1 END,   -- is_boardable
    CASE WHEN drop_off_type = 1 THEN 0 ELSE 1 END    -- is_alightable
FROM tmp_stop_time_screened
WHERE reject_rule IS NULL;

INSERT INTO rejected_row (source_table, src_line_no, rule_code, reason, payload)
SELECT
    'stg_stop_times', src_line_no, reject_rule,
    CASE reject_rule
        WHEN 'DQ-04-ORPHAN-STOP-TIME-TRIP'     THEN 'call references an unknown trip_id'
        WHEN 'DQ-04-ORPHAN-STOP-TIME-PLATFORM' THEN 'call references an unknown stop_id'
        WHEN 'DQ-08-BAD-STOP-SEQUENCE'         THEN 'stop_sequence is not an integer'
        WHEN 'DQ-08-NO-TIME'                   THEN 'call carries neither an arrival nor a departure time'
        WHEN 'DQ-03-IMPLAUSIBLE-DEPARTURE'     THEN 'call occurs 48 h or more after the start of its service day'
        WHEN 'DQ-05-DUPLICATE-CALL'            THEN 'duplicate (trip_id, stop_sequence); first occurrence kept'
    END,
    json_object('trip_id', trip_id, 'stop_sequence', stop_sequence,
                'stop_id', stop_id, 'arrival_time', arrival_time,
                'departure_time', departure_time)
FROM tmp_stop_time_screened
WHERE reject_rule IS NOT NULL;

DROP TABLE tmp_stop_time_screened;


-- ===========================================================================
-- 10. transfer
-- ===========================================================================
INSERT INTO transfer (from_stop_id, to_stop_id, transfer_type,
                      min_transfer_time, from_trip_id, to_trip_id)
SELECT
    TRIM(tr.from_stop_id),
    TRIM(tr.to_stop_id),
    COALESCE(CAST(NULLIF(TRIM(tr.transfer_type), '') AS INTEGER), 0),
    CAST(NULLIF(TRIM(tr.min_transfer_time), '') AS INTEGER),
    ft.trip_id,
    tt.trip_id
FROM stg_transfers tr
JOIN platform pf ON pf.stop_id = TRIM(tr.from_stop_id)
JOIN platform pt ON pt.stop_id = TRIM(tr.to_stop_id)
LEFT JOIN trip ft ON ft.trip_id = NULLIF(TRIM(tr.from_trip_id), '')
LEFT JOIN trip tt ON tt.trip_id = NULLIF(TRIM(tr.to_trip_id),   '');


-- ===========================================================================
-- 11. text_translation
-- ---------------------------------------------------------------------------
-- DQ-07: record_id is empty on all 2 599 rows, so the feed relies on
-- value-keyed translation. 3 of 2 599 source values match no current
-- stop_name/trip_headsign (stale entries); they are kept — they are harmless
-- and dropping them would lose information the publisher intended to ship.
-- INSERT OR IGNORE guards the composite PK against the handful of rows that
-- repeat the same (value, language) pair.
-- ===========================================================================
INSERT OR IGNORE INTO text_translation (table_name, field_name, field_value,
                                        language, translation)
SELECT
    TRIM(tl.table_name),
    TRIM(tl.field_name),
    TRIM(tl.field_value),
    TRIM(tl.language),
    TRIM(tl.translation)
FROM stg_translations tl
WHERE TRIM(COALESCE(tl.field_value, '')) <> ''
  AND TRIM(COALESCE(tl.translation, '')) <> ''
  AND TRIM(tl.language)   IN ('nl', 'de', 'en', 'fr')
  AND TRIM(tl.table_name) IN ('stops', 'trips');
