-- ===========================================================================
-- RailPulse — 06_realtime.sql
-- GTFS-Realtime landing tables (the "Live Stream Integration" nice-to-have).
-- ===========================================================================
-- WHAT THIS IS FOR
-- The static feed tells us what *should* happen. These tables accumulate what
-- *did*: every poll of the NMBS/SNCB trip-update and service-alert feeds is
-- appended as an immutable snapshot, so that running the poller on a timer
-- (scripts/poll_realtime.sh + a cron entry) grows a genuine delay history the
-- static timetable can be measured against.
--
-- CREATE TABLE **IF NOT EXISTS**, DELIBERATELY
-- `make build` drops and rebuilds the whole static core from the feed — that is
-- safe because the feed is the source of truth and can always be re-downloaded.
-- Real-time observations are the opposite: once 06:12's delays are gone, they
-- are gone. So these tables are additive and survive a rebuild.
--
-- WHY rt_trip_update.trip_id IS **NOT** A FOREIGN KEY
-- This is a considered decision, not an oversight. The static feed is
-- regenerated daily; the real-time feed references whatever timetable is live
-- *right now*. During the window between an upstream publish and our next
-- `make build`, real-time rows legitimately name trips our static snapshot has
-- never seen. A hard FK would reject exactly the observations that matter most
-- (a brand-new or re-planned service), and cascade-delete history on rebuild.
--
-- The link is therefore *soft but measured*: `railpulse verify` reports the
-- percentage of real-time trips that resolve against the current static feed,
-- and `v_rt_departure_performance` INNER JOINs the two so that any query about
-- punctuality silently and correctly ignores unmatched rows.
-- ===========================================================================


-- ===========================================================================
-- Reference tables for the GTFS-RT enumerations.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS ref_schedule_relationship (
    code        INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ref_alert_cause (
    code  INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ref_alert_effect (
    code  INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);


-- ===========================================================================
-- rt_snapshot — one row per successful poll of one feed.
-- ---------------------------------------------------------------------------
-- UNIQUE (feed, feed_timestamp_epoch) is the idempotency guard. The upstream
-- feed refreshes every ~30 s; if the poller runs more often than that (or a
-- cron run overlaps a manual one) the same payload comes back with the same
-- header timestamp and is rejected instead of double-counting every delay.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS rt_snapshot (
    snapshot_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed                 TEXT    NOT NULL CHECK (feed IN ('trip-update', 'alert')),
    fetched_at_utc       TEXT    NOT NULL,   -- when *we* called
    feed_timestamp_epoch INTEGER,            -- header.timestamp, when *they* built it
    feed_timestamp_utc   TEXT,               -- same value, human-readable
    entity_count         INTEGER NOT NULL,
    bytes_downloaded     INTEGER,
    source_url           TEXT,
    UNIQUE (feed, feed_timestamp_epoch)
);


-- ===========================================================================
-- rt_trip_update — one row per trip the operator is actively reporting on.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS rt_trip_update (
    snapshot_id            INTEGER NOT NULL
                           REFERENCES rt_snapshot(snapshot_id) ON DELETE CASCADE,
    rt_entity_id           TEXT    NOT NULL,
    trip_id                TEXT,             -- soft link to trip(trip_id) — see header
    route_id               TEXT,             -- soft link to route(route_id)
    start_date             TEXT,             -- ISO 'YYYY-MM-DD'
    start_time             TEXT,             -- 'HH:MM:SS'
    schedule_relationship  INTEGER
                           REFERENCES ref_schedule_relationship(code),
    vehicle_id             TEXT,
    update_timestamp_epoch INTEGER,
    PRIMARY KEY (snapshot_id, rt_entity_id)
);


-- ===========================================================================
-- rt_stop_time_update — the payload: predicted times and delays per call.
-- ---------------------------------------------------------------------------
-- `delay` is signed seconds against the published timetable, which is exactly
-- the metric the client asked for. Note that ~55 % of calls in a typical
-- snapshot carry schedule_relationship = 2 (SKIPPED) with no time at all —
-- those are cancellations, not zero-delay departures, and every query below
-- separates them.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS rt_stop_time_update (
    snapshot_id           INTEGER NOT NULL,
    rt_entity_id          TEXT    NOT NULL,
    stop_sequence         INTEGER NOT NULL,
    stop_id               TEXT,              -- soft link to platform(stop_id)
    arrival_epoch         INTEGER,
    arrival_delay_s       INTEGER,
    departure_epoch       INTEGER,
    departure_delay_s     INTEGER,
    schedule_relationship INTEGER
                          REFERENCES ref_schedule_relationship(code),
    PRIMARY KEY (snapshot_id, rt_entity_id, stop_sequence),
    FOREIGN KEY (snapshot_id, rt_entity_id)
        REFERENCES rt_trip_update(snapshot_id, rt_entity_id) ON DELETE CASCADE
);


-- ===========================================================================
-- rt_alert (+ children) — service disruptions.
-- ---------------------------------------------------------------------------
-- The multilingual header/description are split into rt_alert_text rather than
-- being stored as header_fr / header_nl / header_de / header_en columns. Four
-- repeating columns would be a textbook 1NF violation and would need a schema
-- migration the day the operator adds a fifth language.
-- ===========================================================================
CREATE TABLE IF NOT EXISTS rt_alert (
    snapshot_id  INTEGER NOT NULL
                 REFERENCES rt_snapshot(snapshot_id) ON DELETE CASCADE,
    rt_entity_id TEXT    NOT NULL,
    cause        INTEGER REFERENCES ref_alert_cause(code),
    effect       INTEGER REFERENCES ref_alert_effect(code),
    url          TEXT,
    PRIMARY KEY (snapshot_id, rt_entity_id)
);

CREATE TABLE IF NOT EXISTS rt_alert_text (
    snapshot_id  INTEGER NOT NULL,
    rt_entity_id TEXT    NOT NULL,
    field_name   TEXT    NOT NULL CHECK (field_name IN ('header', 'description')),
    language     TEXT    NOT NULL,
    text         TEXT    NOT NULL,
    PRIMARY KEY (snapshot_id, rt_entity_id, field_name, language),
    FOREIGN KEY (snapshot_id, rt_entity_id)
        REFERENCES rt_alert(snapshot_id, rt_entity_id) ON DELETE CASCADE
);

-- Which part of the network an alert is about. In the observed feed every
-- alert is agency-wide, but the table models the full GTFS-RT shape so a
-- future route- or stop-scoped alert lands correctly without a migration.
CREATE TABLE IF NOT EXISTS rt_alert_informed_entity (
    snapshot_id  INTEGER NOT NULL,
    rt_entity_id TEXT    NOT NULL,
    entity_seq   INTEGER NOT NULL,
    agency_id    TEXT,
    route_id     TEXT,
    stop_id      TEXT,
    trip_id      TEXT,
    PRIMARY KEY (snapshot_id, rt_entity_id, entity_seq),
    FOREIGN KEY (snapshot_id, rt_entity_id)
        REFERENCES rt_alert(snapshot_id, rt_entity_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rt_alert_active_period (
    snapshot_id  INTEGER NOT NULL,
    rt_entity_id TEXT    NOT NULL,
    period_seq   INTEGER NOT NULL,
    start_epoch  INTEGER,
    end_epoch    INTEGER,
    PRIMARY KEY (snapshot_id, rt_entity_id, period_seq),
    FOREIGN KEY (snapshot_id, rt_entity_id)
        REFERENCES rt_alert(snapshot_id, rt_entity_id) ON DELETE CASCADE
);


-- ===========================================================================
-- Indexes
-- ===========================================================================
CREATE INDEX IF NOT EXISTS ix_rt_trip_update_trip
    ON rt_trip_update (trip_id, start_date);

CREATE INDEX IF NOT EXISTS ix_rt_stu_stop
    ON rt_stop_time_update (stop_id, schedule_relationship, departure_delay_s);

CREATE INDEX IF NOT EXISTS ix_rt_snapshot_feed_time
    ON rt_snapshot (feed, fetched_at_utc);


-- ===========================================================================
-- Reference data (GTFS-Realtime v2.0 enumerations)
-- ===========================================================================
INSERT OR IGNORE INTO ref_schedule_relationship (code, label, description) VALUES
    (0, 'SCHEDULED',   'Running in accordance with its GTFS schedule'),
    (1, 'ADDED',       'An extra trip, not in the static feed'),
    (2, 'UNSCHEDULED', 'Running with no schedule (trip level) / SKIPPED (stop level)'),
    (3, 'CANCELED',    'Previously scheduled, now cancelled'),
    (5, 'REPLACEMENT', 'Replaces a previously scheduled trip'),
    (6, 'DUPLICATED',  'A duplicate of an existing trip'),
    (7, 'DELETED',     'Should not be shown to passengers at all');

INSERT OR IGNORE INTO ref_alert_cause (code, label) VALUES
    (1, 'UNKNOWN_CAUSE'), (2, 'OTHER_CAUSE'), (3, 'TECHNICAL_PROBLEM'),
    (4, 'STRIKE'), (5, 'DEMONSTRATION'), (6, 'ACCIDENT'), (7, 'HOLIDAY'),
    (8, 'WEATHER'), (9, 'MAINTENANCE'), (10, 'CONSTRUCTION'),
    (11, 'POLICE_ACTIVITY'), (12, 'MEDICAL_EMERGENCY');

INSERT OR IGNORE INTO ref_alert_effect (code, label) VALUES
    (1, 'NO_SERVICE'), (2, 'REDUCED_SERVICE'), (3, 'SIGNIFICANT_DELAYS'),
    (4, 'DETOUR'), (5, 'ADDITIONAL_SERVICE'), (6, 'MODIFIED_SERVICE'),
    (7, 'OTHER_EFFECT'), (8, 'UNKNOWN_EFFECT'), (9, 'STOP_MOVED'),
    (10, 'NO_EFFECT'), (11, 'ACCESSIBILITY_ISSUE');


-- ===========================================================================
-- v_rt_departure_performance — real-time observations joined to the timetable.
-- ---------------------------------------------------------------------------
-- The bridge between Sprint 1's static model and live operations, and the
-- basis of the hub punctuality leaderboard.
--
-- Conventions used here (and stated in every report built on it):
--   * "Observed departure"  = a stop-time update carrying a departure delay and
--     NOT flagged SKIPPED. Cancellations are counted separately; folding them
--     in as delay = 0 would flatter the operator.
--   * "On time"             = delay < 120 s. Two minutes is the threshold the
--     brief specifies and matches SNCB's own published definition.
--   * The most recent snapshot wins. Polling repeatedly means the same call is
--     observed many times as its prediction firms up; `observation_rank` = 1
--     keeps the latest, so averages are not dominated by whichever trains
--     happened to be polled most often.
-- ===========================================================================
DROP VIEW IF EXISTS v_rt_departure_performance;
CREATE VIEW v_rt_departure_performance AS
WITH observations AS (
    SELECT
        tu.trip_id,
        tu.start_date,
        stu.stop_sequence,
        stu.stop_id,
        stu.departure_delay_s,
        stu.arrival_delay_s,
        stu.schedule_relationship,
        s.fetched_at_utc,
        ROW_NUMBER() OVER (
            PARTITION BY tu.trip_id, tu.start_date, stu.stop_sequence
            ORDER BY s.snapshot_id DESC
        ) AS observation_rank
    FROM rt_stop_time_update stu
    JOIN rt_trip_update tu
          ON tu.snapshot_id  = stu.snapshot_id
         AND tu.rt_entity_id = stu.rt_entity_id
    JOIN rt_snapshot s ON s.snapshot_id = stu.snapshot_id
    WHERE tu.trip_id IS NOT NULL
)
SELECT
    o.trip_id,
    o.start_date,
    o.stop_sequence,
    o.stop_id,
    p.station_id,
    st.station_name,
    p.platform_code,
    t.route_id,
    r.route_short_name,
    t.trip_headsign,
    sched.departure_time    AS scheduled_departure_time,
    sched.departure_hour    AS scheduled_departure_hour,
    o.departure_delay_s,
    o.arrival_delay_s,
    o.schedule_relationship,
    o.fetched_at_utc,
    CASE WHEN o.schedule_relationship = 2 THEN 1 ELSE 0 END AS is_skipped,
    CASE
        WHEN o.schedule_relationship = 2        THEN NULL   -- cancelled: no verdict
        WHEN o.departure_delay_s IS NULL        THEN NULL
        WHEN o.departure_delay_s < 120          THEN 1
        ELSE 0
    END AS is_on_time
FROM observations o
JOIN trip      t     ON t.trip_id    = o.trip_id          -- INNER: drops feed drift
JOIN route     r     ON r.route_id   = t.route_id
LEFT JOIN platform p ON p.stop_id    = o.stop_id
LEFT JOIN station  st ON st.station_id = p.station_id
LEFT JOIN stop_time sched
       ON sched.trip_id       = o.trip_id
      AND sched.stop_sequence = o.stop_sequence
WHERE o.observation_rank = 1;
