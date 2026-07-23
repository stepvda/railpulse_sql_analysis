-- ===========================================================================
-- Q2 — PLATFORM BOTTLENECKS
-- "Identify the top 3 busiest platforms in Brussels-Central."
-- ===========================================================================
--
-- WHAT COUNTS AS A "PLATFORM" HERE
--
-- The feed calls the station "Bruxelles-Central" (feed_lang = 'fr'); the Dutch
-- "Brussel-Centraal" is available through text_translation. It is station
-- gs:nmbssncb:S8813003 and it owns seven child stops: six numbered platforms
-- (1-6) plus one platform_code IS NULL child that the feed uses for calls where
-- no track has been allocated. Every station in the feed has exactly one of
-- those null children, and the 1 348 calls sitting on Bruxelles-Central's are
-- reported separately below rather than being silently folded into a platform.
--
-- THE SAME WEIGHTING ISSUE AS Q1, AND IT CHANGES THE ANSWER
--
-- Ranked by raw timetable rows the order is 3, 4, 2. Ranked by departures that
-- actually happen across the feed year it is 4, 3, 2 — platform 4 carries fewer
-- distinct scheduled calls but they run on more days. The top three are the
-- same set either way, so the headline answer is robust; the ordering inside it
-- is not, and this file says so instead of picking whichever looks tidier.
--
-- Only boardable calls are counted (v_departure), so a train that merely passes
-- through platform 6 without opening its doors does not make platform 6 look
-- busy.
-- ===========================================================================


-- @label: q2_brussels_central_platform_ranking
-- @title: Bruxelles-Central — platform ranking (HEADLINE ANSWER)
-- @description: Numbered platforms only, ranked by annualised departures with
--   the raw timetable count shown alongside. The top 3 are platforms 4, 3 and 2.
WITH platform_load AS (
    SELECT
        d.platform_code,
        COUNT(*)                        AS timetabled_calls,
        SUM(tsd.operating_days)         AS annual_departures,
        COUNT(DISTINCT d.route_id)      AS routes_served,
        COUNT(DISTINCT d.trip_headsign) AS distinct_destinations,
        MIN(d.departure_time)           AS first_departure,
        MAX(d.departure_time)           AS last_departure
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    WHERE d.station_name = 'Bruxelles-Central'
      AND d.has_platform_code = 1
    GROUP BY d.platform_code
)
SELECT
    RANK() OVER (ORDER BY annual_departures DESC) AS rank_annualised,
    RANK() OVER (ORDER BY timetabled_calls DESC)  AS rank_timetabled,
    'Platform ' || platform_code AS platform,
    annual_departures,
    ROUND(100.0 * annual_departures / SUM(annual_departures) OVER (), 1)
        AS pct_of_station,
    timetabled_calls,
    routes_served,
    distinct_destinations,
    first_departure,
    last_departure
FROM platform_load
ORDER BY annual_departures DESC;


-- @label: q2_brussels_central_top3
-- @title: The three busiest platforms, stated plainly
-- @description: The literal answer to the question asked.
WITH platform_load AS (
    SELECT d.platform_code,
           SUM(tsd.operating_days) AS annual_departures
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    WHERE d.station_name = 'Bruxelles-Central'
      AND d.has_platform_code = 1
    GROUP BY d.platform_code
)
SELECT
    ROW_NUMBER() OVER (ORDER BY annual_departures DESC) AS position,
    'Platform ' || platform_code AS busiest_platform,
    annual_departures
FROM platform_load
ORDER BY annual_departures DESC
LIMIT 3;


-- @label: q2_unallocated_platform_calls
-- @title: Calls at Bruxelles-Central with no platform allocated
-- @description: Completeness check. These 1 348 calls are real departures the
--   feed has not assigned to a track, and they are excluded from the ranking
--   above. Reported so the platform figures cannot be mistaken for the
--   station total.
SELECT
    d.station_name,
    COUNT(*) AS calls_without_platform,
    (SELECT COUNT(*) FROM v_departure
      WHERE station_name = 'Bruxelles-Central') AS all_station_calls,
    ROUND(100.0 * COUNT(*) /
          (SELECT COUNT(*) FROM v_departure
            WHERE station_name = 'Bruxelles-Central'), 2) AS pct_unallocated
FROM v_departure d
WHERE d.station_name = 'Bruxelles-Central'
  AND d.has_platform_code = 0
GROUP BY d.station_name;


-- @label: q2_platform_hourly_congestion
-- @title: Bruxelles-Central — congestion by platform and hour
-- @description: Where and when the bottleneck actually bites. A platform that
--   is busy spread evenly across 18 hours is not a bottleneck; one that takes
--   its whole load inside the commuter peak is. peak_share makes that visible.
WITH hourly AS (
    SELECT
        d.platform_code,
        d.departure_hour,
        COUNT(*) AS calls
    FROM v_departure d
    WHERE d.station_name = 'Bruxelles-Central'
      AND d.has_platform_code = 1
    GROUP BY d.platform_code, d.departure_hour
),
totals AS (
    SELECT platform_code, SUM(calls) AS platform_total
    FROM hourly GROUP BY platform_code
)
SELECT
    'Platform ' || h.platform_code AS platform,
    printf('%02d:00', h.departure_hour) AS hour_band,
    h.calls,
    ROUND(100.0 * h.calls / t.platform_total, 1) AS pct_of_platform_day,
    RANK() OVER (PARTITION BY h.platform_code
                 ORDER BY h.calls DESC) AS busiest_hour_rank
FROM hourly h
JOIN totals t ON t.platform_code = h.platform_code
ORDER BY h.platform_code, h.departure_hour;


-- @label: q2_platform_peak_pressure
-- @title: Peak-hour pressure per platform
-- @description: One row per platform: its single busiest hour, and — the number
--   an operations planner actually acts on — how many trains use it in that
--   hour on a typical operating day.
--
--   The busiest hour is chosen by ANNUALISED departures, not raw timetable
--   rows. This file's own header argues that raw rows mislead (they weight a
--   seasonal variant equally with a daily service); picking the peak hour by
--   raw rows here would contradict that and could name an hour that is busy in
--   the timetable file but not on the platform. The raw count is still shown as
--   a column so the two can be compared.
--
--   trains_per_day_in_peak_hour is the annualised count for that hour divided
--   by the number of dates in the feed — the honest per-day figure. (Dividing
--   the raw feed-wide count by 60 would be wrong by three orders of magnitude.)
WITH hourly AS (
    SELECT
        d.platform_code,
        d.departure_hour,
        COUNT(*)                AS timetabled_calls,
        SUM(tsd.operating_days) AS annual_departures
    FROM v_departure d
    JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
    WHERE d.station_name = 'Bruxelles-Central'
      AND d.has_platform_code = 1
    GROUP BY d.platform_code, d.departure_hour
),
feed_days AS (
    SELECT COUNT(DISTINCT service_date) AS dates_in_feed
    FROM service_date WHERE exception_type = 1
),
ranked AS (
    SELECT
        platform_code, departure_hour, timetabled_calls, annual_departures,
        SUM(annual_departures) OVER (PARTITION BY platform_code) AS platform_annual_total,
        ROW_NUMBER() OVER (PARTITION BY platform_code
                           ORDER BY annual_departures DESC) AS hour_rank
    FROM hourly
)
SELECT
    'Platform ' || r.platform_code AS platform,
    printf('%02d:00', r.departure_hour) AS busiest_hour,
    r.annual_departures AS annual_departures_in_busiest_hour,
    ROUND(100.0 * r.annual_departures / r.platform_annual_total, 1)
        AS pct_of_platform_day,
    r.timetabled_calls AS timetabled_calls_in_busiest_hour,
    ROUND(1.0 * r.annual_departures / f.dates_in_feed, 1)
        AS trains_per_day_in_peak_hour,
    ROUND(60.0 / NULLIF(1.0 * r.annual_departures / f.dates_in_feed, 0), 1)
        AS avg_minutes_between_trains_in_peak
FROM ranked r
CROSS JOIN feed_days f
WHERE r.hour_rank = 1
ORDER BY r.annual_departures DESC;


-- @label: q2_hub_comparison
-- @title: Platform pressure across the five main hubs
-- @description: Context for the Bruxelles-Central numbers. calls_per_platform
--   is the crude congestion index: a station with 22 platforms absorbing the
--   same load as one with 6 is not under the same pressure.
SELECT
    d.station_name,
    COUNT(DISTINCT d.platform_code) AS numbered_platforms,
    COUNT(*) AS timetabled_calls,
    SUM(tsd.operating_days) AS annual_departures,
    ROUND(1.0 * COUNT(*) / COUNT(DISTINCT d.platform_code), 0)
        AS calls_per_platform,
    RANK() OVER (ORDER BY 1.0 * COUNT(*)
                          / COUNT(DISTINCT d.platform_code) DESC)
        AS pressure_rank
FROM v_departure d
JOIN v_trip_service_days tsd ON tsd.trip_id = d.trip_id
WHERE d.has_platform_code = 1
  AND d.station_name IN ('Bruxelles-Midi', 'Bruxelles-Central', 'Bruxelles-Nord',
                         'Anvers-Central', 'Gand-Saint-Pierre')
GROUP BY d.station_name
ORDER BY calls_per_platform DESC;
