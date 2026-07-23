-- ===========================================================================
-- RailPulse — 05_views.sql
-- The semantic layer: business vocabulary on top of the GTFS-shaped core.
-- ===========================================================================
-- A view here is a *named join*, not a materialised copy. Its job is to make
-- sure that "a departure", "a morning trip" and "a high-frequency service" mean
-- exactly one thing across every query, every chart and every conversation with
-- the client — instead of five analysts each re-deriving them slightly
-- differently in five WHERE clauses.
--
-- All five core answers are written against these views. If the definition of
-- "departure" ever changes, it changes in one place.
-- ===========================================================================


-- ===========================================================================
-- v_departure — THE canonical departure event.
-- ---------------------------------------------------------------------------
-- One row per scheduled call at which a passenger may actually board, enriched
-- with its platform, station, trip and route.
--
-- Two filters carry the whole definition:
--   is_boardable = 1        excludes the 577 k technical pass-throughs where
--                           the train serves the platform but nobody may get on
--                           (pickup_type = 1). Counting them would inflate every
--                           hub in this report by roughly a quarter.
--   departure_secs NOT NULL excludes calls with no published departure.
--
-- Deliberately NOT filtered here: route_type. The network includes 270
-- rail-replacement bus routes which are genuinely part of SNCB's scheduled
-- service. Individual queries opt in or out and say so.
-- ===========================================================================
DROP VIEW IF EXISTS v_departure;
CREATE VIEW v_departure AS
SELECT
    st.trip_id,
    st.stop_sequence,
    st.stop_id,
    p.station_id,
    s.station_name,
    p.platform_code,
    p.has_platform_code,
    st.departure_time,                 -- raw GTFS text, may exceed 24:00:00
    st.departure_secs,                 -- seconds since the service day began
    st.departure_hour,                 -- 0-23 clock hour, the passenger's view
    st.day_offset,                     -- 0 = same day, 1 = after midnight
    st.pickup_type,
    st.drop_off_type,
    t.route_id,
    t.service_id,
    t.trip_headsign,
    t.trip_short_name,
    t.bikes_allowed,
    t.wheelchair_accessible,
    r.route_short_name,
    r.route_long_name,
    r.route_type,
    rt.label AS route_type_label
FROM stop_time st
JOIN platform       p  ON p.stop_id     = st.stop_id
JOIN station        s  ON s.station_id  = p.station_id
JOIN trip           t  ON t.trip_id     = st.trip_id
JOIN route          r  ON r.route_id    = t.route_id
JOIN ref_route_type rt ON rt.route_type = r.route_type
WHERE st.is_boardable = 1
  AND st.departure_secs IS NOT NULL;


-- ===========================================================================
-- v_trip_service_days — how many calendar days each trip actually runs.
-- ---------------------------------------------------------------------------
-- WHY THIS MATTERS MORE THAN IT LOOKS
-- This feed does not describe "a week"; it describes 358 individual dates
-- between 2025-12-20 and 2026-12-12, and each of its 134 809 trips is attached
-- to its own service calendar. Some trips run 250 times a year, some run once.
--
-- So a plain COUNT(*) over stop_time answers "how many rows are in the
-- timetable file", not "how many trains actually depart". Multiplying each call
-- by this number converts timetable rows into real annual departures. Q1
-- reports both and the difference is material.
-- ===========================================================================
DROP VIEW IF EXISTS v_trip_service_days;
CREATE VIEW v_trip_service_days AS
SELECT
    t.trip_id,
    COUNT(sd.service_date) AS operating_days,
    MIN(sd.service_date)   AS first_operating_day,
    MAX(sd.service_date)   AS last_operating_day
FROM trip t
JOIN service_date sd
      ON sd.service_id     = t.service_id
     AND sd.exception_type = 1          -- 1 = ADDED; the only type in this feed
GROUP BY t.trip_id;


-- ===========================================================================
-- v_trip_origin — the first boardable call of every trip.
-- ---------------------------------------------------------------------------
-- "A trip that departs before 12:00" (Q3) is a statement about where the trip
-- *starts*, not about every intermediate station it happens to leave in the
-- morning. This view pins that down with a window function: rank the calls of
-- each trip by stop_sequence and keep rank 1.
--
-- A correlated `MIN(stop_sequence)` subquery would give the same answer; the
-- window form reads the table once instead of once per trip.
-- ===========================================================================
DROP VIEW IF EXISTS v_trip_origin;
CREATE VIEW v_trip_origin AS
SELECT
    trip_id, stop_sequence, stop_id, station_id, station_name,
    platform_code, departure_time, departure_secs, departure_hour, day_offset,
    route_id, service_id, trip_headsign, trip_short_name,
    route_short_name, route_long_name, route_type
FROM (
    SELECT
        d.*,
        ROW_NUMBER() OVER (PARTITION BY d.trip_id
                           ORDER BY d.stop_sequence) AS call_rank
    FROM v_departure d
)
WHERE call_rank = 1;


-- ===========================================================================
-- v_service_frequency — the weekly rhythm of every service, derived.
-- ---------------------------------------------------------------------------
-- calendar.txt *should* answer "which weekdays does this service run?" via its
-- monday..sunday flags. In this feed all seven are 0 for all 51 593 services
-- (DQ-01), so the question has to be answered from the 4.7 M exploded dates.
--
-- Three different measures are exposed, because they disagree and the
-- disagreement is informative:
--
--   distinct_weekdays      how many of the 7 weekdays the service ever touches.
--                          A Mon-Fri commuter service scores 5 whether it runs
--                          for one week or fifty.
--   typical_days_per_week  the MODAL number of operating days across the weeks
--                          in which the service runs at all. This is the honest
--                          reading of "operates N days a week" and is what the
--                          classification uses.
--   operating_days         the raw annual total, for weighting.
--
-- Weeks are bucketed by counting whole weeks from a fixed Monday epoch
-- (1970-01-05 was a Monday) rather than with strftime('%W'). strftime resets
-- its counter at each new year, which would split the week straddling
-- 2025-12-29 -> 2026-01-04 into two half-weeks and drag those services'
-- typical count down.
-- ===========================================================================
DROP VIEW IF EXISTS v_service_frequency;
CREATE VIEW v_service_frequency AS
WITH active AS (
    SELECT service_id, service_date, day_of_week
    FROM service_date
    WHERE exception_type = 1
),
totals AS (
    SELECT
        service_id,
        COUNT(*)                    AS operating_days,
        COUNT(DISTINCT day_of_week) AS distinct_weekdays,
        MIN(service_date)           AS first_operating_day,
        MAX(service_date)           AS last_operating_day
    FROM active
    GROUP BY service_id
),
per_week AS (
    SELECT
        service_id,
        CAST((julianday(service_date) - julianday('1970-01-05')) / 7 AS INTEGER)
            AS week_index,
        COUNT(*) AS days_in_week
    FROM active
    GROUP BY service_id, week_index
),
week_shape AS (
    SELECT
        service_id,
        days_in_week,
        COUNT(*) AS weeks_with_this_shape
    FROM per_week
    GROUP BY service_id, days_in_week
),
modal AS (
    SELECT
        service_id,
        days_in_week AS typical_days_per_week,
        ROW_NUMBER() OVER (
            PARTITION BY service_id
            -- most common shape wins; ties broken towards the busier week so a
            -- service that is 50/50 between 4 and 5 days is not under-reported
            ORDER BY weeks_with_this_shape DESC, days_in_week DESC
        ) AS shape_rank
    FROM week_shape
),
week_span AS (
    SELECT service_id, COUNT(*) AS active_weeks, MAX(days_in_week) AS max_days_per_week
    FROM per_week
    GROUP BY service_id
)
SELECT
    t.service_id,
    t.operating_days,
    t.distinct_weekdays,
    m.typical_days_per_week,
    w.max_days_per_week,
    w.active_weeks,
    t.first_operating_day,
    t.last_operating_day,
    -- The classification required by the brief, applied to the modal weekly
    -- shape. Kept in the view so Q4, the dashboard and any ad-hoc query cannot
    -- drift apart on where the boundaries sit.
    CASE
        WHEN m.typical_days_per_week >= 5 THEN 'High Frequency'
        WHEN m.typical_days_per_week >= 2 THEN 'Medium Frequency'
        ELSE 'Low Frequency/Special'
    END AS frequency_class
FROM totals t
JOIN modal     m ON m.service_id = t.service_id AND m.shape_rank = 1
JOIN week_span w ON w.service_id = t.service_id;


-- ===========================================================================
-- v_trip_amenity — passenger-amenity flags resolved to labels.
-- ---------------------------------------------------------------------------
-- Q5 hangs on one distinction that GTFS makes and casual readings miss:
--   code 0 = "no information"   -- the operator said nothing
--   code 1 = "yes"              -- an explicit guarantee
--   code 2 = "no"               -- an explicit refusal
-- Reporting 0 as "not accessible" would invent a fact. This view therefore
-- exposes `guarantees_*` (strictly code 1, via ref_accessibility.is_guaranteed)
-- alongside `*_is_unknown`, so a query can always separate "no" from "unstated".
-- ===========================================================================
DROP VIEW IF EXISTS v_trip_amenity;
CREATE VIEW v_trip_amenity AS
SELECT
    t.trip_id,
    t.route_id,
    t.service_id,
    t.trip_headsign,
    r.route_short_name,
    r.route_long_name,
    r.route_type,
    t.bikes_allowed,
    ab.label                AS bikes_allowed_label,
    ab.is_guaranteed        AS guarantees_bikes,
    CASE WHEN t.bikes_allowed = 0 THEN 1 ELSE 0 END AS bikes_is_unknown,
    t.wheelchair_accessible,
    aw.label                AS wheelchair_label,
    aw.is_guaranteed        AS guarantees_wheelchair,
    CASE WHEN t.wheelchair_accessible = 0 THEN 1 ELSE 0 END AS wheelchair_is_unknown,
    -- "any amenity explicitly guaranteed" — the union the brief asks for
    CASE WHEN ab.is_guaranteed = 1 OR aw.is_guaranteed = 1 THEN 1 ELSE 0 END
                            AS guarantees_any_amenity,
    CASE WHEN ab.is_guaranteed = 1 AND aw.is_guaranteed = 1 THEN 1 ELSE 0 END
                            AS guarantees_both_amenities
FROM trip t
JOIN route r              ON r.route_id = t.route_id
JOIN ref_accessibility ab ON ab.code    = t.bikes_allowed
JOIN ref_accessibility aw ON aw.code    = t.wheelchair_accessible;


-- ===========================================================================
-- v_station_daily_departures — network-shape summary, one row per station.
-- Feeds the hub leaderboard and the dashboard's station picker.
-- ===========================================================================
DROP VIEW IF EXISTS v_station_daily_departures;
CREATE VIEW v_station_daily_departures AS
SELECT
    s.station_id,
    s.station_name,
    s.latitude,
    s.longitude,
    COUNT(DISTINCT p.stop_id)  FILTER (WHERE p.has_platform_code = 1)
                                            AS numbered_platforms,
    COUNT(d.trip_id)                        AS timetabled_departures,
    COUNT(DISTINCT d.route_id)              AS routes_served,
    COUNT(DISTINCT d.trip_headsign)         AS distinct_destinations,
    COALESCE(SUM(tsd.operating_days), 0)    AS annual_departures
FROM station s
LEFT JOIN platform p            ON p.station_id = s.station_id
LEFT JOIN v_departure d         ON d.stop_id    = p.stop_id
LEFT JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
GROUP BY s.station_id, s.station_name, s.latitude, s.longitude;
