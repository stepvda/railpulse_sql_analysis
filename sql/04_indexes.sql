-- ===========================================================================
-- RailPulse — 04_indexes.sql
-- Secondary indexes, created *after* the bulk load.
-- ===========================================================================
-- WHY AFTER, NOT BEFORE
-- Building an index while 2.2 M rows stream in means re-balancing a B-tree on
-- every INSERT. Loading first and indexing second lets SQLite sort once and
-- write the tree sequentially. On this dataset that is the difference between
-- a ~2 minute build and a ~10 minute one.
--
-- WHY THESE INDEXES AND NOT OTHERS
-- Every index below exists to serve a specific analytical query, and each is
-- justified by an EXPLAIN QUERY PLAN before/after measurement recorded in
-- sql/analysis/q7_index_optimisation.sql and docs/analysis_report.md. An index
-- is not free: it costs disk, and it costs write throughput on every reload.
-- Indexes nobody can name a query for are technical debt, so there are none.
--
-- The leading column of each composite index is always the *equality*
-- predicate, and the range/grouping column follows — the standard rule that
-- lets SQLite seek straight to a contiguous slice instead of scanning.
--
-- A CAVEAT ON THE PLAN CLAIMS IN THE COMMENTS BELOW AND IN q7. A query plan is
-- chosen by the planner from the ANALYZE statistics and the SQLite version, and
-- it can legitimately change between builds — the same SARGable-violation query
-- has been observed here as both a bare SCAN and a covering-index SCAN on
-- different runs. So read every "expect ..." note as the *shape* to look for
-- (SEARCH vs SCAN, covering or not, a temp B-tree for grouping), not as a
-- byte-exact string. Reproduce with EXPLAIN QUERY PLAN on your own build.
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- Q1 "which hour is busiest?" and every hourly profile.
--
-- Covering index for the *unweighted* hourly histogram: is_boardable filters
-- and departure_hour groups, so `SELECT departure_hour, COUNT(*) ... WHERE
-- is_boardable = 1 GROUP BY departure_hour` is answered entirely from the index
-- (measured ~3.7x faster than without it — see q7_index_optimisation.sql).
--
-- trip_id is the third column so the index stays covering when a query also
-- needs the trip key. NOTE, HONESTLY: the *annualised* Q1 query does NOT use
-- this index. Its plan drives off `trip` and joins into stop_time on the
-- primary key (trip_id, stop_sequence) instead — verify with EXPLAIN QUERY PLAN
-- on q7_plan_heaviest_query, which SEARCHes stop_time USING the PK autoindex,
-- not this one. This index earns its place on the unweighted histogram and the
-- hourly profiles, not on the annualised join.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_stop_time_boardable_hour
    ON stop_time (is_boardable, departure_hour, trip_id);

-- ---------------------------------------------------------------------------
-- Q2 "busiest platforms at Bruxelles-Central".
--
-- Equality on stop_id (a handful of platforms) then is_boardable; departure_hour
-- rides along so the per-hour platform breakdown is also covered.
-- Without this, counting the calls at one station is a full scan of stop_time.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_stop_time_stop_boardable
    ON stop_time (stop_id, is_boardable, departure_hour);

-- ---------------------------------------------------------------------------
-- Q3 "top morning destinations" needs the *origin* call of each trip, i.e.
-- MIN(stop_sequence) per trip. The primary key (trip_id, stop_sequence) already
-- serves that perfectly, so no extra index is created here — noted explicitly
-- so a reader does not wonder whether it was forgotten.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- Q5 "accessibility per route" groups 134 809 trips by route_id.
-- Amenity columns are carried to make the index covering: the query never
-- needs to visit the trip table.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_trip_route_amenity
    ON trip (route_id, bikes_allowed, wheelchair_accessible);

-- ---------------------------------------------------------------------------
-- Q4 and the annualised weighting both resolve trip -> service -> service_date.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_trip_service
    ON trip (service_id);

-- service_date's PRIMARY KEY (service_id, service_date) already answers
-- "when does service X run?". This index answers the mirror question,
-- "which services run on date D?", used by the representative-day analysis
-- and by the real-time joins.
CREATE INDEX IF NOT EXISTS ix_service_date_date
    ON service_date (service_date, service_id);

-- ---------------------------------------------------------------------------
-- Q3's headsign ranking, and the destination filters in the dashboard.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_trip_headsign
    ON trip (trip_headsign);

-- ---------------------------------------------------------------------------
-- station -> platform navigation. Used by every station-scoped query
-- (Q2, the hub leaderboard, the dashboard station picker).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_platform_station
    ON platform (station_id, has_platform_code, platform_code);

-- Station lookup by name. Station names are not unique in GTFS in general
-- (they are in this feed), so this is a plain index, not a UNIQUE constraint.
CREATE INDEX IF NOT EXISTS ix_station_name
    ON station (station_name);

-- ---------------------------------------------------------------------------
-- Translation lookups from the dashboard's language switch.
-- (text_translation is WITHOUT ROWID with a composite PK leading on
--  table_name/field_name/field_value, so language-first lookups need help.)
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_translation_lang
    ON text_translation (language, table_name, field_name);

-- ---------------------------------------------------------------------------
-- Data-quality review: "show me everything rule DQ-03 rejected".
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_rejected_rule
    ON rejected_row (rule_code, source_table);

-- ---------------------------------------------------------------------------
-- Refresh the query planner's table/index statistics. Without this SQLite
-- plans from row-count guesses; with it, the plans are chosen from the real
-- distribution (e.g. that `is_boardable = 1` selects 67 % of rows and is
-- therefore a poor *leading* filter on its own — which is why every index that
-- uses it puts an equality column ahead of it, or pairs it with the grouping
-- column that makes the index covering).
-- ---------------------------------------------------------------------------
ANALYZE;
