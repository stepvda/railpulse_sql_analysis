-- ===========================================================================
-- Q1 — THE PEAK HOUR PROBLEM
-- "What hour of the day experiences the highest volume of scheduled train
--  departures across the entire network?"
-- ===========================================================================
--
-- THE TRAP IN THIS QUESTION
--
-- The obvious query is `SELECT hour, COUNT(*) FROM stop_times GROUP BY hour`.
-- On this feed it answers **10:00**, and it is wrong — or rather, it answers a
-- different question than the client asked.
--
-- The SNCB feed is a *year-long* timetable: 358 dates, 51 593 service calendars
-- and 134 809 trips, where each trip carries its own calendar. A summer-Sunday
-- excursion and a Monday-to-Friday commuter train are one row each in
-- stop_times, but the second one actually departs ~250 times more often. So
-- COUNT(*) measures "rows in the timetable file", not "trains that depart".
--
-- Multiplying every call by the number of days its service runs
-- (v_trip_service_days) converts timetable rows into real departures, and the
-- answer moves to **17:00**, with 07:00 a close second — the evening and
-- morning commuter peaks any rail planner would expect to see.
--
-- Both numbers are reported below, because the gap between them *is* the
-- finding: off-peak and seasonal services are over-represented in the raw
-- timetable, and any capacity decision taken from the naive count would invest
-- in the wrong hour of the day.
--
-- Definitions used throughout (all encoded in v_departure):
--   * a "departure" is a call where a passenger may actually board
--     (pickup_type <> 1); the 577 k technical pass-throughs are excluded.
--   * the hour is the clock hour a passenger reads on the platform, so a
--     GTFS "24:20:00" counts towards hour 00, not a fictional hour 24.
--
-- ---------------------------------------------------------------------------
-- ⓘ SQL FEATURES USED BELOW, IF ANY ARE NEW TO YOU
--
--   WITH name AS (...)      A "common table expression" (CTE): a named
--                           intermediate result, so one unreadable nested
--                           query becomes a readable sequence of steps.
--
--   RANK() OVER (ORDER BY x DESC)
--                           A *window function*. Unlike a normal aggregate,
--                           it does NOT collapse rows: every row survives and
--                           gets its ranking attached. Here it numbers the
--                           hours 1..24 by volume without a second query.
--
--   SUM(x) OVER ()          The same idea with an empty window: puts the GRAND
--                           TOTAL on every row. That is what lets the next
--                           column compute "percentage of the whole day"
--                           in one pass instead of querying the total
--                           separately and joining it back.
--
--   SUM(SUM(x)) OVER ()     Looks odd, is not a typo. The inner SUM is the
--                           GROUP BY aggregate; the outer one is a window over
--                           the already-grouped rows. Read it as "the total of
--                           all the group totals".
--
--   printf('%02d:00', h)    Formatting only — turns the integer 7 into "07:00"
--                           so the output sorts and reads correctly.
--
-- docs/glossary.md explains all of these at more length.
-- ===========================================================================


-- @label: q1_annualised_departures_by_hour
-- @title: Peak hour — annualised departures (HEADLINE ANSWER)
-- @description: Every boardable call weighted by the number of days its service
--   actually operates over the 2025-12-20 -> 2026-12-12 feed window. This is
--   the number of trains that really leave a platform in each hour.
WITH hourly AS (
    SELECT
        d.departure_hour                    AS departure_hour,
        SUM(tsd.operating_days)             AS annual_departures,
        COUNT(*)                            AS timetabled_calls,
        COUNT(DISTINCT d.station_id)        AS stations_active,
        COUNT(DISTINCT d.route_id)          AS routes_active
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    GROUP BY d.departure_hour
)
SELECT
    printf('%02d:00-%02d:59', departure_hour, departure_hour) AS hour_band,
    departure_hour,
    annual_departures,
    ROUND(100.0 * annual_departures / SUM(annual_departures) OVER (), 2)
        AS pct_of_network_day,
    timetabled_calls,
    stations_active,
    routes_active,
    RANK() OVER (ORDER BY annual_departures DESC) AS rank_by_volume
FROM hourly
ORDER BY annual_departures DESC;


-- @label: q1_naive_timetable_rows_by_hour
-- @title: Peak hour — naive timetable-row count (for contrast)
-- @description: The same grouping WITHOUT weighting by operating days. Answers
--   10:00. Shown so the reader can see exactly how much the seasonal/off-peak
--   long tail distorts an unweighted count.
SELECT
    printf('%02d:00-%02d:59', departure_hour, departure_hour) AS hour_band,
    departure_hour,
    COUNT(*) AS timetabled_calls,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_rows,
    RANK() OVER (ORDER BY COUNT(*) DESC) AS rank_by_rows
FROM v_departure
GROUP BY departure_hour
ORDER BY timetabled_calls DESC;


-- @label: q1_rank_divergence
-- @title: How far each hour moves between the two methods
-- @description: The audit trail for the headline claim. A positive
--   rank_improvement means the hour is busier in reality than the raw
--   timetable suggests — the commuter peaks — and a negative one means the
--   opposite.
WITH weighted AS (
    SELECT d.departure_hour AS h,
           SUM(tsd.operating_days) AS annual_departures,
           COUNT(*) AS timetabled_calls
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    GROUP BY d.departure_hour
),
ranked AS (
    SELECT
        h,
        annual_departures,
        timetabled_calls,
        RANK() OVER (ORDER BY annual_departures DESC) AS rank_annualised,
        RANK() OVER (ORDER BY timetabled_calls  DESC) AS rank_naive,
        -- average number of days a call in this hour actually runs
        ROUND(1.0 * annual_departures / timetabled_calls, 1) AS avg_days_per_call
    FROM weighted
)
SELECT
    printf('%02d:00', h) AS hour_band,
    rank_annualised,
    rank_naive,
    rank_naive - rank_annualised AS rank_improvement,
    annual_departures,
    timetabled_calls,
    avg_days_per_call
FROM ranked
ORDER BY rank_annualised;


-- @label: q1_peak_by_daytype
-- @title: Peak hour split by weekday vs weekend
-- @description: The network runs two different days. Aggregating them together
--   hides that the weekend has no commuter peak at all, which matters for the
--   winter-scheduling decision this analysis feeds.
--
--   Note the shape of this query: the weekday/weekend split is resolved ONCE
--   per trip (134 809 rows) and only then joined to the 2.2 M calls. Joining
--   v_departure straight onto service_date would multiply 2.2 M calls by ~90
--   service dates each and materialise ~200 M intermediate rows to answer a
--   48-row question.
WITH trip_daytype AS (
    SELECT
        t.trip_id,
        SUM(CASE WHEN sd.day_of_week IN (0, 6) THEN 1 ELSE 0 END) AS weekend_days,
        SUM(CASE WHEN sd.day_of_week BETWEEN 1 AND 5 THEN 1 ELSE 0 END) AS weekday_days
    FROM trip t
    JOIN service_date sd ON sd.service_id     = t.service_id
                        AND sd.exception_type = 1
    GROUP BY t.trip_id
),
hourly AS (
    SELECT
        d.departure_hour,
        SUM(td.weekday_days) AS weekday_departures,
        SUM(td.weekend_days) AS weekend_departures
    FROM v_departure d
    JOIN trip_daytype td ON td.trip_id = d.trip_id
    GROUP BY d.departure_hour
),
unpivoted AS (
    SELECT departure_hour, 'Weekday' AS day_type, weekday_departures AS departures
    FROM hourly
    UNION ALL
    SELECT departure_hour, 'Weekend', weekend_departures
    FROM hourly
)
SELECT
    day_type,
    printf('%02d:00', departure_hour) AS hour_band,
    departures,
    ROUND(100.0 * departures
          / SUM(departures) OVER (PARTITION BY day_type), 2) AS pct_of_day_type,
    RANK() OVER (PARTITION BY day_type ORDER BY departures DESC) AS rank_in_day_type
FROM unpivoted
ORDER BY day_type, departures DESC;


-- @label: q1_peak_hour_headline
-- @title: One-line answer
-- @description: The single row a stakeholder needs.
WITH hourly AS (
    SELECT d.departure_hour AS h, SUM(tsd.operating_days) AS annual_departures
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    GROUP BY d.departure_hour
)
SELECT
    printf('%02d:00-%02d:59', h, h)  AS peak_hour,
    annual_departures,
    ROUND(100.0 * annual_departures
          / (SELECT SUM(annual_departures) FROM hourly), 2) AS pct_of_all_departures
FROM hourly
ORDER BY annual_departures DESC
LIMIT 1;
