-- ===========================================================================
-- Q5 — THE ACCESSIBILITY AUDIT (VEHICLE FEATURES)
-- "Calculate the exact ratio and percentage of scheduled trips per route that
--  explicitly guarantee wheelchair accessibility or bicycle storage
--  (bikes_allowed). Which specific routes score the lowest in passenger
--  amenity availability?"
-- ===========================================================================
--
-- THE ONE DISTINCTION THAT DECIDES THIS ANSWER
--
-- GTFS encodes both amenities with the same three-value vocabulary:
--     0 = no information      1 = yes, guaranteed      2 = no
-- Code 0 is a *silence*, not a denial. Counting `<> 1` as "not accessible"
-- would publish a statement about SNCB's fleet that the data does not support,
-- so every ratio below is computed strictly against code 1 (the
-- `is_guaranteed` flag on ref_accessibility) and "no information" is reported
-- as its own column rather than being folded into a negative.
--
-- That distinction turns out to carry the whole finding.
--
-- WHAT THE DATA ACTUALLY SAYS
--
--  1. wheelchair_accessible is 'No information' for ALL 134 809 trips. The
--     field is completely unpopulated in this feed. No wheelchair conclusion
--     can be drawn from it — and reporting that honestly is the most useful
--     thing this audit can do, because it is a fixable publishing gap rather
--     than a fleet problem.
--
--  2. bikes_allowed splits perfectly by mode:
--       route_type 2 (Rail) : 123 051 trips, 100.0 % guarantee bike storage
--       route_type 3 (Bus)  :  11 758 trips,   0.0 % do
--     Every one of the 270 zero-scoring routes is a rail-replacement bus. So
--     the "worst routes" are not a scattered set of underperformers to chase
--     individually; they are a single, coherent operational category. The
--     recommendation that follows from that is completely different from the
--     one a route-by-route table would suggest, which is why the mode split is
--     the second query rather than a footnote.
-- ===========================================================================


-- @label: q5_network_amenity_coverage
-- @title: Network-wide amenity coverage (HEADLINE ANSWER)
-- @description: The exact ratio and percentage across all 134 809 scheduled
--   trips, keeping "explicitly yes", "explicitly no" and "no information"
--   strictly apart.
SELECT
    'Bicycle storage (bikes_allowed)' AS amenity,
    COUNT(*) AS scheduled_trips,
    SUM(guarantees_bikes) AS trips_explicitly_guaranteed,
    SUM(CASE WHEN bikes_allowed = 2 THEN 1 ELSE 0 END) AS trips_explicitly_refused,
    SUM(bikes_is_unknown) AS trips_no_information,
    printf('%d / %d', SUM(guarantees_bikes), COUNT(*)) AS ratio,
    ROUND(100.0 * SUM(guarantees_bikes) / COUNT(*), 2) AS pct_guaranteed
FROM v_trip_amenity
UNION ALL
SELECT
    'Wheelchair accessibility (wheelchair_accessible)',
    COUNT(*),
    SUM(guarantees_wheelchair),
    SUM(CASE WHEN wheelchair_accessible = 2 THEN 1 ELSE 0 END),
    SUM(wheelchair_is_unknown),
    printf('%d / %d', SUM(guarantees_wheelchair), COUNT(*)),
    ROUND(100.0 * SUM(guarantees_wheelchair) / COUNT(*), 2)
FROM v_trip_amenity
UNION ALL
SELECT
    'Either amenity guaranteed',
    COUNT(*),
    SUM(guarantees_any_amenity),
    NULL,
    SUM(CASE WHEN guarantees_any_amenity = 0 THEN 1 ELSE 0 END),
    printf('%d / %d', SUM(guarantees_any_amenity), COUNT(*)),
    ROUND(100.0 * SUM(guarantees_any_amenity) / COUNT(*), 2)
FROM v_trip_amenity;


-- @label: q5_amenity_by_mode
-- @title: The finding — amenity availability is a mode split, not a route split
-- @description: Bicycle provision is 100 % on rail and 0 % on rail-replacement
--   bus. Nothing in between. This is what makes the "worst routes" table below
--   interpretable.
SELECT
    a.route_type,
    rt.label AS mode,
    COUNT(DISTINCT a.route_id) AS routes,
    COUNT(*) AS scheduled_trips,
    SUM(a.guarantees_bikes) AS trips_with_bikes_guaranteed,
    printf('%d / %d', SUM(a.guarantees_bikes), COUNT(*)) AS bikes_ratio,
    ROUND(100.0 * SUM(a.guarantees_bikes) / COUNT(*), 2) AS pct_bikes,
    SUM(a.guarantees_wheelchair) AS trips_with_wheelchair_guaranteed,
    ROUND(100.0 * SUM(a.guarantees_wheelchair) / COUNT(*), 2) AS pct_wheelchair
FROM v_trip_amenity a
JOIN ref_route_type rt ON rt.route_type = a.route_type
GROUP BY a.route_type, rt.label
ORDER BY scheduled_trips DESC;


-- @label: q5_route_amenity_ratios
-- @title: Amenity ratio per route, worst first
-- @description: The per-route table the question asks for. Restricted to routes
--   with at least 25 scheduled trips so a two-trip route cannot take the bottom
--   of the table on noise; the unrestricted version is the next query.
SELECT
    a.route_id,
    a.route_short_name,
    a.route_long_name,
    rt.label AS mode,
    COUNT(*) AS scheduled_trips,
    SUM(a.guarantees_bikes) AS bikes_guaranteed,
    printf('%d / %d', SUM(a.guarantees_bikes), COUNT(*)) AS bikes_ratio,
    ROUND(100.0 * SUM(a.guarantees_bikes) / COUNT(*), 2) AS pct_bikes,
    SUM(a.guarantees_wheelchair) AS wheelchair_guaranteed,
    ROUND(100.0 * SUM(a.guarantees_wheelchair) / COUNT(*), 2) AS pct_wheelchair,
    ROUND(100.0 * SUM(a.guarantees_any_amenity) / COUNT(*), 2) AS pct_any_amenity,
    RANK() OVER (
        ORDER BY 1.0 * SUM(a.guarantees_any_amenity) / COUNT(*) ASC,
                 COUNT(*) DESC
    ) AS worst_rank
FROM v_trip_amenity a
JOIN ref_route_type rt ON rt.route_type = a.route_type
GROUP BY a.route_id, a.route_short_name, a.route_long_name, rt.label
HAVING COUNT(*) >= 25
ORDER BY pct_any_amenity ASC, scheduled_trips DESC
LIMIT 30;


-- @label: q5_worst_routes_all_sizes
-- @title: Every route scoring 0 % on both amenities
-- @description: No minimum-trip filter. 270 routes score zero; all 270 are
--   route_type 3 (rail-replacement bus), which is the point.
SELECT
    COUNT(*) AS routes_scoring_zero,
    SUM(scheduled_trips) AS trips_affected,
    SUM(CASE WHEN route_type = 3 THEN 1 ELSE 0 END) AS of_which_replacement_bus,
    SUM(CASE WHEN route_type = 2 THEN 1 ELSE 0 END) AS of_which_rail,
    MIN(scheduled_trips) AS smallest_route_trips,
    MAX(scheduled_trips) AS largest_route_trips
FROM (
    SELECT
        route_id,
        route_type,
        COUNT(*) AS scheduled_trips,
        SUM(guarantees_any_amenity) AS any_amenity
    FROM v_trip_amenity
    GROUP BY route_id, route_type
    HAVING SUM(guarantees_any_amenity) = 0
);


-- @label: q5_worst_routes_by_passenger_exposure
-- @title: Zero-amenity routes ranked by how many passengers they expose
-- @description: "Which routes score lowest" is only half the question a client
--   needs answered. All 270 score 0 %, so the actionable ranking is by
--   exposure: trips multiplied by the days they actually run. These are the
--   replacement-bus corridors to fix first.
SELECT
    RANK() OVER (ORDER BY SUM(tsd.operating_days) DESC) AS priority,
    a.route_short_name,
    a.route_long_name,
    COUNT(*) AS scheduled_trips,
    SUM(tsd.operating_days) AS annual_trips,
    printf('%d / %d', SUM(a.guarantees_any_amenity), COUNT(*)) AS amenity_ratio,
    ROUND(100.0 * SUM(a.guarantees_any_amenity) / COUNT(*), 2) AS pct_any_amenity
FROM v_trip_amenity a
JOIN v_trip_service_days tsd ON tsd.trip_id = a.trip_id
GROUP BY a.route_id, a.route_short_name, a.route_long_name
HAVING SUM(a.guarantees_any_amenity) = 0
ORDER BY annual_trips DESC
LIMIT 15;


-- @label: q5_amenity_by_route_category
-- @title: Amenity coverage by commercial route category
-- @description: IC / S / L / P / BUS. Confirms the split is mode-driven rather
--   than product-driven: every rail category scores 100 %, BUS scores 0 %.
SELECT
    COALESCE(a.route_short_name, '(unnamed)') AS route_category,
    rt.label AS mode,
    COUNT(DISTINCT a.route_id) AS routes,
    COUNT(*) AS scheduled_trips,
    printf('%d / %d', SUM(a.guarantees_bikes), COUNT(*)) AS bikes_ratio,
    ROUND(100.0 * SUM(a.guarantees_bikes) / COUNT(*), 2) AS pct_bikes,
    ROUND(100.0 * SUM(a.guarantees_wheelchair) / COUNT(*), 2) AS pct_wheelchair
FROM v_trip_amenity a
JOIN ref_route_type rt ON rt.route_type = a.route_type
GROUP BY a.route_short_name, rt.label
HAVING COUNT(*) >= 100
ORDER BY pct_bikes ASC, scheduled_trips DESC;


-- @label: q5_station_accessibility_gap
-- @title: The second unpopulated accessibility field
-- @description: stops.wheelchair_boarding is the station-level companion to
--   trips.wheelchair_accessible, and it is equally empty — all 652 stations
--   report 'No information'. Together the two gaps mean this feed cannot
--   support any accessibility statement at all, which is the finding to take
--   back to the publisher.
SELECT
    ra.label AS wheelchair_boarding,
    COUNT(*) AS stations,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM station), 2) AS pct_of_stations
FROM station s
JOIN ref_accessibility ra ON ra.code = s.wheelchair_boarding
GROUP BY ra.label
ORDER BY stations DESC;
