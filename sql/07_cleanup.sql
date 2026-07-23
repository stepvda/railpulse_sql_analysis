-- ===========================================================================
-- RailPulse — 07_cleanup.sql
-- Drop the staging layer once the core model has been built from it.
-- ===========================================================================
-- Staging holds a second, untyped copy of the entire feed — roughly 400 MB of
-- the finished database, and none of it is queried by a single analytical
-- statement. Once 03_transform.sql has succeeded, its job is done.
--
-- Run `railpulse build --keep-staging` to retain these tables when you want to
-- debug a transform rule against the exact bytes that produced it, or to
-- inspect a row referenced by `rejected_row.src_line_no`.
--
-- The database is VACUUMed afterwards (from build.py, since VACUUM cannot run
-- inside a transaction) so the freed pages are actually returned to the OS.
-- ===========================================================================

DROP TABLE IF EXISTS stg_agency;
DROP TABLE IF EXISTS stg_feed_info;
DROP TABLE IF EXISTS stg_stops;
DROP TABLE IF EXISTS stg_routes;
DROP TABLE IF EXISTS stg_trips;
DROP TABLE IF EXISTS stg_stop_times;
DROP TABLE IF EXISTS stg_calendar;
DROP TABLE IF EXISTS stg_calendar_dates;
DROP TABLE IF EXISTS stg_transfers;
DROP TABLE IF EXISTS stg_translations;
