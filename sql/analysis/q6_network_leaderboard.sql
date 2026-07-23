-- ===========================================================================
-- Q6 — NETWORK LEADERBOARD  (nice-to-have)
-- "Create a visual leaderboard comparing these 5 main hubs. Which city has the
--  most efficient, on-time station?"
-- ===========================================================================
--
-- TWO KINDS OF "EFFICIENT", AND ONLY ONE OF THEM IS PUNCTUALITY
--
-- The static timetable cannot answer "on time" — it is a plan, and a plan is by
-- definition never late. So this file is in two halves:
--
--   Part A  STRUCTURAL leaderboard, from the static feed alone. Load, platform
--           pressure, connectivity, peak concentration and turnaround headroom.
--           Always available, and it is what a scheduler optimises.
--   Part B  PUNCTUALITY leaderboard, from the accumulated GTFS-Realtime
--           snapshots. Only meaningful once the poller has been running; the
--           first query in Part B reports exactly how much observation the
--           verdict rests on, so a leaderboard built on twenty minutes of data
--           cannot be mistaken for a leaderboard built on a month of it.
--
-- The five hubs compared are Bruxelles-Midi, Bruxelles-Central, Bruxelles-Nord,
-- Anvers-Central and Gand-Saint-Pierre — the country's highest-load stations
-- outside the Brussels junction triple, chosen in src/railpulse/config.py so
-- the dashboard and this file cannot disagree about the shortlist.
--
-- ON-TIME THRESHOLD: a departure is on time when its real-time delay is under
-- 120 seconds. That is the threshold the brief specifies and it matches SNCB's
-- own published punctuality definition. Cancellations (SKIPPED) are never
-- folded in as zero-delay departures; they are counted separately, because
-- deleting a late train is not the same as running it on time.
-- ===========================================================================


-- ===========================================================================
-- PART A — STRUCTURAL LEADERBOARD (static feed; always available)
-- ===========================================================================

-- @label: q6_hub_structural_leaderboard
-- @title: Hub leaderboard — structural load and pressure
-- @description: One row per hub. calls_per_platform is the crude congestion
--   index; peak_concentration_pct is the share of the day's departures landing
--   in the hub's single busiest hour — a station that spreads its load is
--   easier to run than one that spikes.
WITH hub_calls AS (
    SELECT
        d.station_id,
        d.station_name,
        d.platform_code,
        d.departure_hour,
        d.route_id,
        d.trip_headsign,
        d.trip_id
    FROM v_departure d
    WHERE d.station_name IN ('Bruxelles-Midi', 'Bruxelles-Central',
                             'Bruxelles-Nord', 'Anvers-Central',
                             'Gand-Saint-Pierre')
),
per_hour AS (
    SELECT station_name, departure_hour, COUNT(*) AS calls
    FROM hub_calls GROUP BY station_name, departure_hour
),
peak AS (
    SELECT
        station_name,
        MAX(calls) AS peak_hour_calls,
        SUM(calls) AS total_calls
    FROM per_hour GROUP BY station_name
),
totals AS (
    SELECT
        c.station_name,
        COUNT(*)                          AS timetabled_calls,
        SUM(tsd.operating_days)           AS annual_departures,
        COUNT(DISTINCT c.platform_code)   AS numbered_platforms,
        COUNT(DISTINCT c.route_id)        AS routes_served,
        COUNT(DISTINCT c.trip_headsign)   AS distinct_destinations
    FROM hub_calls c
    JOIN v_trip_service_days tsd ON tsd.trip_id = c.trip_id
    WHERE c.platform_code IS NOT NULL
    GROUP BY c.station_name
)
SELECT
    RANK() OVER (ORDER BY t.annual_departures DESC) AS rank_by_volume,
    t.station_name,
    t.annual_departures,
    t.timetabled_calls,
    t.numbered_platforms,
    ROUND(1.0 * t.timetabled_calls / t.numbered_platforms, 0) AS calls_per_platform,
    t.routes_served,
    t.distinct_destinations,
    ROUND(100.0 * p.peak_hour_calls / p.total_calls, 1) AS peak_concentration_pct,
    RANK() OVER (ORDER BY 1.0 * t.timetabled_calls
                          / t.numbered_platforms DESC) AS rank_by_pressure
FROM totals t
JOIN peak p ON p.station_name = t.station_name
ORDER BY t.annual_departures DESC;


-- @label: q6_hub_hourly_shape
-- @title: Hub load by hour — the shape behind the pressure index
-- @description: Feeds the dashboard heat-map. Two hubs with identical daily
--   totals behave completely differently if one is flat and the other spikes.
WITH hub_hours AS (
    SELECT d.station_name, d.departure_hour, COUNT(*) AS calls
    FROM v_departure d
    WHERE d.station_name IN ('Bruxelles-Midi', 'Bruxelles-Central',
                             'Bruxelles-Nord', 'Anvers-Central',
                             'Gand-Saint-Pierre')
    GROUP BY d.station_name, d.departure_hour
)
SELECT
    station_name,
    printf('%02d:00', departure_hour) AS hour_band,
    calls,
    ROUND(100.0 * calls
          / SUM(calls) OVER (PARTITION BY station_name), 2) AS pct_of_hub_day,
    RANK() OVER (PARTITION BY station_name ORDER BY calls DESC) AS hour_rank
FROM hub_hours
ORDER BY station_name, departure_hour;


-- @label: q6_hub_composite_score
-- @title: Composite structural efficiency score
-- @description: A single ranked number for the leaderboard visual. Each hub is
--   scored 0-100 on three normalised components and the score is the mean:
--     connectivity  destinations reachable (more is better)
--     headroom      inverse of calls-per-platform (less crowded is better)
--     smoothness    inverse of peak concentration (flatter is better)
--   This is a presentation device, not a physical measurement, and it is
--   labelled as such: the components are published alongside so a reader can
--   disagree with the weighting and recompute.
WITH base AS (
    SELECT
        d.station_name,
        COUNT(*) AS calls,
        COUNT(DISTINCT d.platform_code) AS platforms,
        COUNT(DISTINCT d.trip_headsign) AS destinations
    FROM v_departure d
    WHERE d.station_name IN ('Bruxelles-Midi', 'Bruxelles-Central',
                             'Bruxelles-Nord', 'Anvers-Central',
                             'Gand-Saint-Pierre')
      AND d.has_platform_code = 1
    GROUP BY d.station_name
),
peaks AS (
    SELECT station_name,
           MAX(calls) * 1.0 / SUM(calls) AS peak_share
    FROM (SELECT station_name, departure_hour, COUNT(*) AS calls
          FROM v_departure
          WHERE station_name IN ('Bruxelles-Midi', 'Bruxelles-Central',
                                 'Bruxelles-Nord', 'Anvers-Central',
                                 'Gand-Saint-Pierre')
            AND has_platform_code = 1
          GROUP BY station_name, departure_hour)
    GROUP BY station_name
),
metrics AS (
    SELECT
        b.station_name,
        b.destinations,
        1.0 * b.calls / b.platforms AS calls_per_platform,
        p.peak_share
    FROM base b JOIN peaks p ON p.station_name = b.station_name
),
scored AS (
    SELECT
        station_name,
        destinations,
        ROUND(calls_per_platform, 0) AS calls_per_platform,
        ROUND(100.0 * peak_share, 1) AS peak_concentration_pct,
        100.0 * (destinations - MIN(destinations) OVER ())
              / NULLIF(MAX(destinations) OVER () - MIN(destinations) OVER (), 0)
            AS connectivity_score,
        100.0 * (MAX(calls_per_platform) OVER () - calls_per_platform)
              / NULLIF(MAX(calls_per_platform) OVER ()
                       - MIN(calls_per_platform) OVER (), 0)
            AS headroom_score,
        100.0 * (MAX(peak_share) OVER () - peak_share)
              / NULLIF(MAX(peak_share) OVER () - MIN(peak_share) OVER (), 0)
            AS smoothness_score
    FROM metrics
)
SELECT
    RANK() OVER (ORDER BY (connectivity_score + headroom_score
                           + smoothness_score) / 3.0 DESC) AS leaderboard_position,
    station_name,
    ROUND((connectivity_score + headroom_score + smoothness_score) / 3.0, 1)
        AS composite_score,
    ROUND(connectivity_score, 1) AS connectivity_score,
    ROUND(headroom_score, 1)     AS headroom_score,
    ROUND(smoothness_score, 1)   AS smoothness_score,
    destinations,
    calls_per_platform,
    peak_concentration_pct
FROM scored
ORDER BY composite_score DESC;


-- ===========================================================================
-- PART B — PUNCTUALITY LEADERBOARD (real-time; needs the poller)
-- ===========================================================================

-- @label: q6_realtime_coverage
-- @title: How much real-time observation this leaderboard rests on
-- @description: RUN THIS FIRST. Every punctuality number below is only as good
--   as this table. A verdict drawn from a single snapshot is an anecdote; state
--   the sample size before stating the winner.
SELECT
    (SELECT COUNT(*) FROM rt_snapshot WHERE feed = 'trip-update')
        AS trip_update_snapshots,
    (SELECT COUNT(*) FROM rt_snapshot WHERE feed = 'alert')
        AS alert_snapshots,
    (SELECT MIN(fetched_at_utc) FROM rt_snapshot) AS first_observation_utc,
    (SELECT MAX(fetched_at_utc) FROM rt_snapshot) AS last_observation_utc,
    (SELECT COUNT(*) FROM rt_stop_time_update)    AS raw_stop_time_updates,
    (SELECT COUNT(*) FROM v_rt_departure_performance)
        AS distinct_observed_calls,
    (SELECT COUNT(*) FROM v_rt_departure_performance WHERE is_on_time IS NOT NULL)
        AS calls_with_a_delay_reading,
    (SELECT COUNT(DISTINCT trip_id) FROM rt_trip_update) AS distinct_trips_seen,
    (SELECT ROUND(100.0 * COUNT(*) /
                  NULLIF((SELECT COUNT(*) FROM rt_trip_update), 0), 1)
       FROM rt_trip_update r JOIN trip t ON t.trip_id = r.trip_id)
        AS pct_realtime_trips_matching_static;


-- @label: q6_hub_punctuality
-- @title: Hub punctuality leaderboard (real-time)
-- @description: On-time rate per hub, on-time defined as a departure delay
--   under 120 s.
--
--   THREE STATES ARE KEPT APART, AND THE MIDDLE ONE IS THE TRAP.
--   GTFS-RT's stop-level schedule_relationship uses a different vocabulary from
--   its trip-level one, on the same integers:
--     1 = SKIPPED  the call will not happen. A cancellation. No punctuality
--                  verdict is possible, and it must NOT be scored as on time —
--                  deleting a late train is not running it punctually.
--     2 = NO_DATA  the operator has no prediction for this call. The train is
--                  still expected. This is NOT a cancellation, and reading it
--                  as one (the easy mistake, since 2 means UNSCHEDULED at trip
--                  level) overstates cancellations by orders of magnitude and
--                  silently shrinks the denominator.
--   Both are reported in their own columns so neither can hide inside the
--   on-time rate. Hubs with no scored departures are excluded rather than
--   shown as 100 %.
SELECT
    RANK() OVER (
        ORDER BY 1.0 * SUM(COALESCE(is_on_time, 0))
                 / NULLIF(SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END), 0)
        DESC
    ) AS punctuality_rank,
    station_name,
    SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END) AS scored_departures,
    SUM(COALESCE(is_on_time, 0)) AS on_time_departures,
    ROUND(100.0 * SUM(COALESCE(is_on_time, 0))
          / NULLIF(SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END), 0), 1)
        AS on_time_pct,
    SUM(is_skipped)   AS cancelled_calls,
    SUM(has_no_data)  AS calls_without_prediction,
    COUNT(*)          AS calls_observed_total,
    ROUND(AVG(CASE WHEN is_skipped = 0 THEN departure_delay_s END), 0)
        AS avg_delay_seconds,
    MAX(departure_delay_s) AS worst_delay_seconds
FROM v_rt_departure_performance
WHERE station_name IN ('Bruxelles-Midi', 'Bruxelles-Central', 'Bruxelles-Nord',
                       'Anvers-Central', 'Gand-Saint-Pierre')
GROUP BY station_name
HAVING SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END) > 0
ORDER BY on_time_pct DESC;


-- @label: q6_punctuality_all_stations
-- @title: Punctuality across every observed station
-- @description: The five-hub shortlist is the brief's, not the network's. This
--   widens it so a genuinely poor performer outside the shortlist is not
--   invisible. Minimum 10 observations so a single late train cannot take the
--   bottom of the table.
SELECT
    station_name,
    SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END) AS observed_departures,
    ROUND(100.0 * SUM(COALESCE(is_on_time, 0))
          / NULLIF(SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END), 0), 1)
        AS on_time_pct,
    ROUND(AVG(CASE WHEN is_skipped = 0 THEN departure_delay_s END), 0)
        AS avg_delay_seconds,
    SUM(is_skipped)  AS cancelled_calls,
    SUM(has_no_data) AS calls_without_prediction
FROM v_rt_departure_performance
WHERE station_name IS NOT NULL
GROUP BY station_name
HAVING SUM(CASE WHEN is_on_time IS NOT NULL THEN 1 ELSE 0 END) >= 10
ORDER BY on_time_pct ASC, observed_departures DESC
LIMIT 25;


-- @label: q6_delay_distribution
-- @title: Delay distribution across all observed departures
-- @description: An average delay hides everything that matters. A network where
--   95 % of trains are punctual and 5 % are an hour late has the same mean as
--   one where every train is three minutes late, and they are completely
--   different railways to travel on.
--
--   Cancelled (SKIPPED) calls are excluded — they have no delay to band. Calls
--   the operator has issued no prediction for (NO_DATA) are NOT excluded; they
--   land in '(no reading)', which is exactly what they are. Dropping them
--   silently would make the coverage of this table look better than it is.
SELECT
    CASE
        WHEN departure_delay_s IS NULL      THEN '(no reading)'
        WHEN departure_delay_s <    0       THEN 'Early'
        WHEN departure_delay_s <  120       THEN 'On time (< 2 min)'
        WHEN departure_delay_s <  300       THEN 'Slight delay (2-5 min)'
        WHEN departure_delay_s <  900       THEN 'Delayed (5-15 min)'
        WHEN departure_delay_s < 1800       THEN 'Badly delayed (15-30 min)'
        ELSE                                     'Severely delayed (30 min+)'
    END AS delay_band,
    COUNT(*) AS calls,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 2) AS pct_of_observations
FROM v_rt_departure_performance
WHERE is_skipped = 0
GROUP BY delay_band
ORDER BY MIN(COALESCE(departure_delay_s, -999999));


-- @label: q6_active_service_alerts
-- @title: Service alerts observed in the latest snapshot
-- @description: The disruption context behind the punctuality figures, in
--   French with the effect and cause resolved to labels.
WITH latest AS (
    SELECT MAX(snapshot_id) AS snapshot_id
    FROM rt_snapshot WHERE feed = 'alert'
)
SELECT
    c.label AS cause,
    e.label AS effect,
    txt.text AS alert_fr
FROM rt_alert a
JOIN latest l              ON l.snapshot_id = a.snapshot_id
LEFT JOIN ref_alert_cause  c ON c.code = a.cause
LEFT JOIN ref_alert_effect e ON e.code = a.effect
LEFT JOIN rt_alert_text  txt ON txt.snapshot_id  = a.snapshot_id
                            AND txt.rt_entity_id = a.rt_entity_id
                            AND txt.field_name   = 'header'
                            AND txt.language     = 'fr'
ORDER BY e.label, txt.text;
