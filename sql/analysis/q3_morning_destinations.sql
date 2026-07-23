-- ===========================================================================
-- Q3 — BUSIEST MORNING DESTINATIONS
-- "Find the top 3 most frequent terminal destinations (trip_headsign) for all
--  morning trips that depart before 12:00:00 PM."
-- ===========================================================================
--
-- READING THE QUESTION CAREFULLY
--
-- "morning trips that depart before 12:00" is a statement about the trip, not
-- about each of its calls. An Ostende-Eupen service that leaves its origin at
-- 06:04 is a morning trip even though it is still leaving intermediate stations
-- at 13:30. Filtering `stop_time.departure_hour < 12` would count that one trip
-- once per morning station it happens to pass through, which both double-counts
-- it and quietly reclassifies long afternoon services as morning ones.
--
-- So the filter is applied to the trip's ORIGIN — v_trip_origin, the first
-- boardable call of each trip, found with a ROW_NUMBER window. One row per
-- trip, exactly as the question intends.
--
-- `day_offset = 0` additionally excludes trips whose origin is published as
-- 24:xx or later. Those are after-midnight continuations of the previous
-- service day; their clock hour is legitimately "before 12:00" but calling them
-- morning trips would be wrong.
--
-- BOTH RANKINGS ARE REPORTED, AND THEY DISAGREE BELOW RANK 1
--
--   by annualised trips : Anvers-Central, Louvain, Charleroi-Central
--   by raw trip count   : Anvers-Central, Bruxelles-Midi, Louvain
--
-- Bruxelles-Midi appears near the top of the raw count because a large number
-- of distinct morning services terminate there, but many of them run on few
-- days. Anvers-Central leads on both measures, so it is the one destination the
-- answer can be stated flatly.
-- ===========================================================================


-- @label: q3_morning_destinations_ranked
-- @title: Morning terminal destinations, ranked (HEADLINE ANSWER)
-- @description: Trips whose origin departs before 12:00:00, grouped by
--   trip_headsign. Ranked by annualised trips, with the raw count and its own
--   rank alongside so the divergence is visible rather than hidden.
WITH morning_trips AS (
    SELECT
        o.trip_headsign,
        o.trip_id,
        tsd.operating_days
    FROM v_trip_origin o
    JOIN v_trip_service_days tsd ON tsd.trip_id = o.trip_id
    WHERE o.departure_secs < 12 * 3600     -- before 12:00:00
      AND o.day_offset = 0                 -- same service day, not a 24:xx tail
      AND o.trip_headsign IS NOT NULL
),
by_destination AS (
    SELECT
        trip_headsign,
        COUNT(*)                 AS morning_trips,
        SUM(operating_days)      AS annual_morning_trips,
        ROUND(AVG(operating_days), 1) AS avg_days_per_trip
    FROM morning_trips
    GROUP BY trip_headsign
)
SELECT
    RANK() OVER (ORDER BY annual_morning_trips DESC) AS rank_annualised,
    RANK() OVER (ORDER BY morning_trips DESC)        AS rank_raw,
    trip_headsign AS terminal_destination,
    annual_morning_trips,
    ROUND(100.0 * annual_morning_trips
          / SUM(annual_morning_trips) OVER (), 2) AS pct_of_morning_network,
    morning_trips,
    avg_days_per_trip
FROM by_destination
ORDER BY annual_morning_trips DESC
LIMIT 25;


-- @label: q3_top3_destinations
-- @title: The three busiest morning destinations, stated plainly
SELECT
    ROW_NUMBER() OVER (ORDER BY SUM(tsd.operating_days) DESC) AS position,
    o.trip_headsign AS terminal_destination,
    SUM(tsd.operating_days) AS annual_morning_trips,
    COUNT(*) AS distinct_morning_services
FROM v_trip_origin o
JOIN v_trip_service_days tsd ON tsd.trip_id = o.trip_id
WHERE o.departure_secs < 12 * 3600
  AND o.day_offset = 0
  AND o.trip_headsign IS NOT NULL
GROUP BY o.trip_headsign
ORDER BY annual_morning_trips DESC
LIMIT 3;


-- @label: q3_morning_vs_afternoon
-- @title: Which destinations are genuinely morning-skewed
-- @description: A destination can top the morning table simply by being busy
--   all day. morning_share isolates the destinations whose demand is
--   specifically a morning phenomenon — the commuter flows worth protecting
--   when the winter timetable is thinned.
WITH origins AS (
    SELECT
        o.trip_headsign,
        CASE WHEN o.departure_secs < 12 * 3600 THEN 1 ELSE 0 END AS is_morning,
        tsd.operating_days
    FROM v_trip_origin o
    JOIN v_trip_service_days tsd ON tsd.trip_id = o.trip_id
    WHERE o.day_offset = 0
      AND o.trip_headsign IS NOT NULL
)
SELECT
    trip_headsign AS terminal_destination,
    SUM(operating_days * is_morning)       AS annual_morning_trips,
    SUM(operating_days * (1 - is_morning)) AS annual_afternoon_evening_trips,
    SUM(operating_days)                    AS annual_trips_all_day,
    ROUND(100.0 * SUM(operating_days * is_morning)
          / SUM(operating_days), 1)        AS morning_share_pct
FROM origins
GROUP BY trip_headsign
HAVING SUM(operating_days) >= 5000        -- ignore the long tail of specials
ORDER BY morning_share_pct DESC, annual_morning_trips DESC
LIMIT 20;


-- @label: q3_morning_departure_profile
-- @title: When the morning peak actually is
-- @description: Morning trips bucketed by their origin hour. Confirms that
--   "before 12:00" is not one flat block: it contains a sharp 07:00-08:00
--   commuter spike.
SELECT
    printf('%02d:00', o.departure_hour) AS origin_hour,
    COUNT(*) AS distinct_services,
    SUM(tsd.operating_days) AS annual_trips,
    COUNT(DISTINCT o.trip_headsign) AS distinct_destinations,
    ROUND(100.0 * SUM(tsd.operating_days)
          / SUM(SUM(tsd.operating_days)) OVER (), 1) AS pct_of_morning
FROM v_trip_origin o
JOIN v_trip_service_days tsd ON tsd.trip_id = o.trip_id
WHERE o.departure_secs < 12 * 3600
  AND o.day_offset = 0
GROUP BY o.departure_hour
ORDER BY o.departure_hour;


-- @label: q3_morning_destinations_multilingual
-- @title: The top destinations in all four published languages
-- @description: The feed publishes French names. SNCB operates in a trilingual
--   country, so any report going to the client needs the Dutch and German
--   forms too. This is what the text_translation table is for.
WITH top_destinations AS (
    SELECT
        o.trip_headsign,
        SUM(tsd.operating_days) AS annual_morning_trips
    FROM v_trip_origin o
    JOIN v_trip_service_days tsd ON tsd.trip_id = o.trip_id
    WHERE o.departure_secs < 12 * 3600
      AND o.day_offset = 0
      AND o.trip_headsign IS NOT NULL
    GROUP BY o.trip_headsign
    ORDER BY annual_morning_trips DESC
    LIMIT 10
)
SELECT
    t.trip_headsign AS destination_fr,
    MAX(CASE WHEN x.language = 'nl' THEN x.translation END) AS destination_nl,
    MAX(CASE WHEN x.language = 'de' THEN x.translation END) AS destination_de,
    MAX(CASE WHEN x.language = 'en' THEN x.translation END) AS destination_en,
    t.annual_morning_trips
FROM top_destinations t
LEFT JOIN text_translation x
       ON x.field_value = t.trip_headsign
      AND x.field_name IN ('trip_headsign', 'stop_name')
GROUP BY t.trip_headsign, t.annual_morning_trips
ORDER BY t.annual_morning_trips DESC;
