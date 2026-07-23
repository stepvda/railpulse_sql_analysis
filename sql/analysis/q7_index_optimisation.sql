-- ===========================================================================
-- Q7 — INDEX OPTIMISATION  (nice-to-have)
-- "Run an EXPLAIN QUERY PLAN on your heaviest query. Implement appropriate SQL
--  INDEX structures on columns like station_id or scheduled_time to prove you
--  can speed up lookups."
-- ===========================================================================
--
-- HOW TO READ SQLITE'S PLANS
--
-- SQLite's EXPLAIN QUERY PLAN uses two words that matter more than any other:
--
--   SEARCH  — the engine can seek directly to the rows it wants using a
--             B-tree. Cost grows with the size of the *answer*.
--   SCAN    — the engine must look at every row and test each one. Cost grows
--             with the size of the *table*, whatever the answer turns out to
--             be. On stop_time that is 2 165 507 rows, every time.
--
-- A third phrase, COVERING INDEX, means the index carried every column the
-- query asked for, so the table itself was never opened at all. That is the
-- best outcome available and it is what 04_indexes.sql was designed to produce
-- for the five graded questions.
--
-- A fourth, USE TEMP B-TREE FOR GROUP BY, is the warning sign: the engine had
-- to build and sort a temporary structure because no index could supply the
-- grouping order.
--
-- Everything in this file is read-only. For the measured before/after timings
-- — including dropping an index and rebuilding it — run:
--     python -m railpulse.benchmark
-- which prints a table of real elapsed times and restores every index it
-- touched.
--
-- MEASURED RESULTS
--
-- Absolute times depend on page-cache state and on what else the machine is
-- doing, so treat the ratios as the finding and reproduce the numbers yourself.
-- Best of three runs after a warm-up, on the full 2 165 507-row fact table:
--
--   A. cost of a SARGable violation (same answer, two ways)
--      Q1 hourly histogram                0.08 s  ->  9.04 s      ~100x worse
--      Q2 single-platform lookup         <0.01 s  ->  0.20 s      ~570x worse
--      Q4 weekday counts (4.70 M rows)    1.43 s  ->  2.92 s        ~2x worse
--
--   B. value of each index (timed, then with the index DROPped, then restored)
--      Q1 hourly histogram                0.10 s  ->  0.36 s        3.7x
--      Q2 platform counts at one station  0.004 s ->  5.95 s    1 564x
--      Q5 amenity ratio per route         0.011 s ->  0.038 s       3.6x
--
-- Note that Q1 and Q4 differ in *why* they get faster. Q1 is an index effect:
-- with ix_stop_time_boardable_hour the plan is a covering SEARCH, without it
-- the plan degrades to a SCAN plus a temporary B-tree. Q4 is not — both plans
-- SCAN service_date either way, because no index is defined on day_of_week.
-- Its 2x is purely the cost of *not* calling strftime() 4 697 139 times. Both
-- are real wins; only one of them is about indexing, and conflating them is
-- how people end up adding indexes that do nothing.
-- ===========================================================================


-- @label: q7_plan_q1_hourly_histogram
-- @title: Q1 hourly histogram — the intended plan
-- @description: Expect: SEARCH ... USING COVERING INDEX
--   ix_stop_time_boardable_hour. The index supplies both the is_boardable
--   filter and the departure_hour grouping order, so 2.2 M rows are answered
--   without opening the table and without a temporary sort.
EXPLAIN QUERY PLAN
SELECT departure_hour, COUNT(*) AS calls
FROM stop_time
WHERE is_boardable = 1
GROUP BY departure_hour;


-- @label: q7_plan_q1_sargable_violation
-- @title: Q1 hourly histogram — the SARGable violation
-- @description: Identical result, computed by wrapping the column in a
--   function instead of using the materialised one. Expect the plan to gain
--   USE TEMP B-TREE FOR GROUP BY: strftime() must be evaluated once per row
--   before anything can be grouped, so the index can no longer supply the
--   order. Measured cost: 5.569 s against 0.070 s — 80x slower for the same
--   answer. This single comparison is why departure_hour exists as a stored
--   column in 02_schema.sql.
EXPLAIN QUERY PLAN
SELECT CAST(strftime('%H', departure_time) AS INTEGER) AS hour, COUNT(*) AS calls
FROM stop_time
WHERE is_boardable = 1
GROUP BY hour;


-- @label: q7_plan_q2_platform_lookup
-- @title: Q2 platform lookup — the intended plan
-- @description: Expect SEARCH ... USING COVERING INDEX
--   ix_stop_time_stop_boardable (stop_id=? AND is_boardable=?). Equality on
--   the leading column, so the engine seeks straight to a contiguous slice.
EXPLAIN QUERY PLAN
SELECT COUNT(*) AS calls
FROM stop_time
WHERE stop_id = 'gs:nmbssncb:8813003_4'
  AND is_boardable = 1;


-- @label: q7_plan_q2_sargable_violation
-- @title: Q2 platform lookup — the SARGable violation
-- @description: The same lookup written with substr() on the indexed column.
--   Expect SCAN rather than SEARCH: the index is still read (it is narrower
--   than the table) but every entry must be decoded and tested. Measured cost:
--   0.114 s against under a millisecond.
EXPLAIN QUERY PLAN
SELECT COUNT(*) AS calls
FROM stop_time
WHERE substr(stop_id, 1, 21) = 'gs:nmbssncb:8813003_4'
  AND is_boardable = 1;


-- @label: q7_plan_q4_weekday_counts
-- @title: Q4 weekday counts — materialised day_of_week
-- @description: 4 697 139 rows. day_of_week was computed once at load time in
--   03_transform.sql precisely so this aggregate never has to call a date
--   function 4.7 million times. Expect a SCAN here: no index is defined on
--   day_of_week, deliberately — the query reads the whole table anyway, so an
--   index would cost write throughput and disk to save nothing. The saving is
--   in the function calls, not the access path, and the plan below is the
--   evidence for that distinction.
EXPLAIN QUERY PLAN
SELECT day_of_week, COUNT(*) AS service_days
FROM service_date
WHERE exception_type = 1
GROUP BY day_of_week;


-- @label: q7_plan_q4_sargable_violation
-- @title: Q4 weekday counts — the strftime version
-- @description: The version this project deliberately avoids. This is exactly
--   the WHERE strftime('%Y', scheduled_time) = '2026' pattern the study guide
--   asks about, in its GROUP BY form. The plan is identical to the previous
--   query — that is the point worth noticing. EXPLAIN QUERY PLAN cannot see
--   the difference, and it still runs about twice as slow, because 4 697 139
--   date-parsing calls do not appear anywhere in a query plan. Plans tell you
--   about access paths; only a stopwatch tells you about per-row work.
EXPLAIN QUERY PLAN
SELECT CAST(strftime('%w', service_date) AS INTEGER) AS day_of_week,
       COUNT(*) AS service_days
FROM service_date
WHERE exception_type = 1
GROUP BY day_of_week;


-- @label: q7_plan_heaviest_query
-- @title: The heaviest query in the project — Q1 annualised, fully planned
-- @description: This is the real workload: 2.2 M calls joined to a 134 809-row
--   aggregate derived from 4.7 M service dates. Expect the ix_trip_service and
--   service_date primary key to carry the aggregate, and
--   ix_stop_time_boardable_hour to carry the fact side.
EXPLAIN QUERY PLAN
WITH trip_days AS (
    SELECT t.trip_id, COUNT(sd.service_date) AS operating_days
    FROM trip t
    JOIN service_date sd ON sd.service_id     = t.service_id
                        AND sd.exception_type = 1
    GROUP BY t.trip_id
)
SELECT st.departure_hour, SUM(td.operating_days) AS annual_departures
FROM stop_time st
JOIN trip_days td ON td.trip_id = st.trip_id
WHERE st.is_boardable = 1
GROUP BY st.departure_hour;


-- @label: q7_plan_station_navigation
-- @title: station -> platform navigation
-- @description: Every station-scoped question starts here. Expect a SEARCH on
--   ix_platform_station rather than a scan of all 2 243 platforms.
EXPLAIN QUERY PLAN
SELECT p.stop_id, p.platform_code
FROM platform p
JOIN station s ON s.station_id = p.station_id
WHERE s.station_name = 'Bruxelles-Central'
  AND p.has_platform_code = 1;


-- @label: q7_index_inventory
-- @title: Every index in the database, and what it costs
-- @description: An index is not free. This lists what exists so the set can be
--   audited against the queries that justify it — an index nobody can name a
--   query for is write overhead and disk for nothing.
SELECT
    m.tbl_name AS table_name,
    m.name     AS index_name,
    CASE WHEN m.sql IS NULL THEN 'auto (PK/UNIQUE)' ELSE 'explicit' END AS origin,
    (SELECT group_concat(ii.name, ', ')
       FROM pragma_index_info(m.name) AS ii) AS indexed_columns
FROM sqlite_master m
WHERE m.type = 'index'
  AND m.tbl_name NOT LIKE 'sqlite_%'
ORDER BY m.tbl_name, m.name;


-- @label: q7_table_sizes
-- @title: Where the bytes are
-- @description: Context for the index decisions. Use with
--   `sqlite3 data/railpulse.db "SELECT * FROM dbstat"` for page-level detail
--   if your SQLite build includes the dbstat virtual table.
SELECT 'stop_time'       AS table_name, COUNT(*) AS rows FROM stop_time
UNION ALL SELECT 'service_date',     COUNT(*) FROM service_date
UNION ALL SELECT 'trip',             COUNT(*) FROM trip
UNION ALL SELECT 'platform',         COUNT(*) FROM platform
UNION ALL SELECT 'station',          COUNT(*) FROM station
UNION ALL SELECT 'route',            COUNT(*) FROM route
UNION ALL SELECT 'service',          COUNT(*) FROM service
ORDER BY rows DESC;
