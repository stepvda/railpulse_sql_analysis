# 🚉 RailPulse — Architecture Decision Log

Each entry records one decision that shaped the code in this repository: what the
situation was, what was chosen, what that costs, and what was rejected. An ADR is
written when a reasonable engineer could have gone the other way. Decisions that
have only one sane answer are not recorded here.

Every number quoted below was measured against the built database
(`data/railpulse.db`, feed version 2026-07-20, window 2025-12-20 → 2026-12-12).
Where a claim could not be measured, it says so.

**Status vocabulary** — *Accepted*: in force, implemented. *Accepted, revisit in
Sprint 2*: correct for the current scale, with a named trigger for change.

| # | Decision | Status |
|---|---|---|
| [ADR-01](#adr-01--cleaning-lives-in-sql-not-in-python-elt-not-etl) | Cleaning lives in SQL, not in Python (ELT, not ETL) | Accepted |
| [ADR-02](#adr-02--sqlite-as-the-sprint-1-engine) | SQLite as the Sprint 1 engine | Accepted, revisit in Sprint 2 |
| [ADR-03](#adr-03--station-and-platform-are-two-tables-not-one-stops-table) | `station` and `platform` are two tables, not one `stops` table | Accepted |
| [ADR-04](#adr-04--derived-time-columns-are-materialised-on-the-fact-table) | Derived time columns are materialised on the fact table | Accepted |
| [ADR-05](#adr-05--every-gtfs-code-column-is-a-foreign-key-into-a-ref_-table) | Every GTFS code column is a foreign key into a `ref_` table | Accepted |
| [ADR-06](#adr-06--bad-rows-are-quarantined-never-dropped) | Bad rows are quarantined, never dropped | Accepted |
| [ADR-07](#adr-07--weekly-frequency-is-derived-from-calendar_dates-and-read-modally) | Weekly frequency is derived from `calendar_dates`, and read modally | Accepted |
| [ADR-08](#adr-08--departures-are-annualised-by-operating-days-and-the-naive-count-is-published-beside-them) | Departures are annualised by operating days, and the naive count is published beside them | Accepted |
| [ADR-09](#adr-09--the-real-time-trip_id-is-a-soft-link-not-a-foreign-key) | The real-time `trip_id` is a soft link, not a foreign key | Accepted |
| [ADR-10](#adr-10--rt_-tables-are-additive-the-static-core-is-dropped-and-rebuilt) | `rt_*` tables are additive; the static core is dropped and rebuilt | Accepted |
| [ADR-11](#adr-11--indexes-are-built-after-the-bulk-load-and-only-when-a-query-names-them) | Indexes are built after the bulk load, and only when a query names them | Accepted |
| [ADR-12](#adr-12--staging-is-dropped-after-a-successful-build-and-the-database-is-not-committed) | Staging is dropped after a successful build, and the database is not committed | Accepted |
| [ADR-13](#adr-13--pandas-is-confined-to-the-rendering-layer) | pandas is confined to the rendering layer | Accepted |

---

## ADR-01 — Cleaning lives in SQL, not in Python (ELT, not ETL)

**Status:** Accepted.

**Context.** The challenge forbids pandas or any data-frame engine for filtering
and aggregating, and limits Python to network requests and executing raw SQL.
That constraint could have been met cheaply: a handful of `if` statements inside
the CSV reader is not a data-frame. It was not met cheaply, because the
constraint happens to point at a better design than the one it forbids.

**Decision.** `src/railpulse/ingest_static.py` copies each CSV row into a `stg_*`
table verbatim — no inspection, cast, trim, filter, default or reorder. Every
staging column in `sql/01_staging.sql` is `TEXT` apart from the synthetic
`src_line_no`, and none carries a `PRIMARY KEY`, `FOREIGN KEY`, `NOT NULL` or
`CHECK`, because staging must be able to hold bad data. A cell reading `87:39:00`
arrives in staging as the string `87:39:00`. All nine DQ rules, every cast and
every de-duplication live in `sql/03_transform.sql`.

**Consequences.** The whole cleaning layer is one file a reviewer can read, diff
and run by hand against the exact bytes that produced a row. Because a rule is a
SQL predicate rather than a Python branch, it can *quarantine* instead of raise
(ADR-06): the last build staged 7,057,090 rows, loaded 6,997,455 into the three
largest core tables and quarantined 12. The cost is a full second, untyped copy
of the feed — 480 MB of the built file (ADR-12) — and the fact that nothing is
validated until the transform runs. That is made safe by running the transform in
one transaction: a failure leaves the core model untouched.

**Alternatives rejected.** *Validate in the Python loader:* fastest to write, but
a rejected row would exist only as a log line, and the rule would be invisible to
anyone reading the SQL. *Load into typed staging tables:* a failed cast aborts
the batch and loses exactly the row worth studying.

---

## ADR-02 — SQLite as the Sprint 1 engine

**Status:** Accepted, revisit in Sprint 2.

**Context.** The workload is a single-writer batch rebuild followed by read-only
analysis: 2,165,507 fact rows, 4,697,139 calendar rows, no concurrent writers, no
users other than the analyst and one dashboard process. The brief names SQLite,
and the Sprint 2 roadmap names "Azure SQL or Azure Database for PostgreSQL".

**Decision.** SQLite for Sprint 1, with the PRAGMAs in `src/railpulse/db.py`
applied on every connection: `foreign_keys = ON` (SQLite disables enforcement by
default, which would make every `REFERENCES` clause in `02_schema.sql` decorative),
`journal_mode = WAL` so the dashboard can read while a build writes,
`synchronous = NORMAL` on the reasonable ground that a power cut costs a rebuild
rather than customer data, and `busy_timeout = 30000`.

**Consequences.** Zero-configuration, single-file, trivially reproducible; the
whole database is one artefact an evaluator can delete and recreate. The schema
uses SQLite-specific features that buy real savings — `WITHOUT ROWID` on
`service_date` and `text_translation`, negative `cache_size` — and those are the
lines a migration will have to touch.

**The named trigger for change.** SQLite serialises writers at the file level: one
write transaction at a time, database-wide. The moment more than one process needs
to write — the study guide's "50 separate scraper scripts", or Sprint 2's
timer-triggered Azure Function polling every 15 to 30 minutes while a rebuild
runs — writers begin queueing on `busy_timeout` and then failing with
`SQLITE_BUSY`. That is the
migration signal, not row count. PostgreSQL's MVCC is the answer to it.

**Alternatives rejected.** *PostgreSQL now:* correct destination, wrong sprint;
it adds a server to provision before a single question is answered. *DuckDB:*
genuinely better for the analytical scans here, but it is not what the brief
specifies and its concurrency story is no better for Sprint 2.

---

## ADR-03 — `station` and `platform` are two tables, not one `stops` table

**Status:** Accepted.

**Context.** GTFS ships stations and boarding points interleaved in a single
`stops.txt`, told apart by a `location_type` discriminator: 652 rows with
`location_type = 1` are stations, 2,243 rows with `location_type = 0` are the
platforms inside them. That is two grains in one file, which is precisely what
normalisation forbids.

**Decision.** `sql/03_transform.sql` splits the file on `location_type` into
`station` and `platform`, joined by a real foreign key. `platform` deliberately
does **not** carry `stop_name`.

**Evidence for dropping `stop_name` from the child.** Measured on staging: of the
2,243 `location_type = 0` rows, **0** have a `stop_name` that differs from their
parent station's. The column is therefore functionally dependent on `station_id`,
not on `stop_id` — storing it on the child is a transitive dependency and a
textbook 3NF violation. Queries join through to `station` instead.

**Consequences.** A station name is stored once and can be corrected once.
"Bruxelles-Central" becomes a single addressable entity (`gs:nmbssncb:S8813003`)
rather than a string repeated across its platforms, which is what makes Q2
expressible at all. `has_platform_code` is materialised alongside `platform_code`
so the "real, numbered platforms only" filter stays SARGable: 1,591 platforms
carry a number, and each of the 652 stations owns exactly one `platform_code IS
NULL` child that the feed uses when no track has been allocated.

**Alternatives rejected.** *One `stops` table mirroring GTFS:* faithful to the
source, but every station-level query would need a `WHERE location_type = 1`
guard and the evaluation criterion is normalisation, not fidelity. *A `stop_name`
column on `platform` "for convenience":* denormalisation with no measured read
benefit — the opposite of ADR-04, where the benefit was measured.

---

## ADR-04 — Derived time columns are materialised on the fact table

**Status:** Accepted.

**Context.** GTFS clock times are *service-relative*, not wall-clock: a train
leaving at 00:20 on a Saturday service is published as `24:20:00`. The raw feed
contains 31,154 such calls. Three different questions need three different shapes
of that one value, and computing any of them inside a `WHERE` or `GROUP BY` over
2,165,507 rows is the SARGability violation the study guide asks about —
`GROUP BY strftime('%H', departure_time)` forces a function call per row and makes
every index on the column unusable.

**Decision.** `stop_time` stores the raw `departure_time` TEXT *and* seven derived
companions, computed once in the transform: `departure_secs` (seconds since the
service day began, correct for ordering and durations), `departure_hour`
(`(secs / 3600) % 24` — the hour a passenger reads on the platform clock),
`day_offset` (`secs / 86400`), the boolean `is_boardable` / `is_alightable`
pair, and `arrival_secs` / `arrival_hour` on the same pattern.
`service_date.day_of_week` is materialised on the same reasoning.

**Consequences.** This is a knowing denormalisation: the derived columns are
functionally dependent on `departure_time` and could be recomputed. What is
bought is that Q1 groups an indexed `INTEGER` instead of parsing 2.2 M strings,
and `ix_stop_time_boardable_hour` can cover the query outright. `is_boardable`
also encodes a definition rather than an optimisation — 712,286 calls have
`pickup_type = 1`, of which 577,462 (26.67% of all calls) are technical
pass-throughs where the train serves the platform and nobody may board. Counting
those as departures would overstate every hub by roughly a quarter. The cost is
disk and the rule that these columns are only ever written by
`sql/03_transform.sql`.

**Alternatives rejected.** *Compute per query:* correct, and measurably slower on
every one of the five answers. *A generated/virtual column:* a `VIRTUAL` column
re-computes on every read, so it buys nothing here. A `STORED` one would work —
SQLite does index generated columns, including inside the composites in
`04_indexes.sql`, so performance is not the objection. The objection is locality:
the parsing expression would then live in the `CREATE TABLE` in `02_schema.sql`
while every other derivation and cleaning rule lives in `03_transform.sql`, and a
rule split across two files is a rule nobody reviews. (SQLite also forbids a
generated column inside a `PRIMARY KEY`, which constrains how far the pattern
could be taken.)

---

## ADR-05 — Every GTFS code column is a foreign key into a `ref_` table

**Status:** Accepted.

**Context.** GTFS encodes meaning as bare integers. `bikes_allowed = 1`,
`route_type = 2`, `pickup_type = 1`, `exception_type = 1`: four different
vocabularies, all stored as small integers, all silently averageable. The
accessibility vocabulary is the dangerous one — code `0` means *"no information"*,
not *"no"*, and conflating them is the most common error in accessibility
reporting (DQ-02).

**Decision.** Six reference tables (`ref_location_type`, `ref_route_type`,
`ref_pickup_drop`, `ref_accessibility`, `ref_exception_type`, `ref_transfer_type`)
are seeded from the GTFS Schedule Reference with 28 codes in total, and every code
column in the core model that has a vocabulary worth naming carries a
`REFERENCES` clause into one of them. Two honest exceptions: `trip.direction_id`
is a bare 0/1 with no labels to attach, so it is constrained by `CHECK`; and
`ref_location_type` is seeded but referenced by nothing, because the split it
describes (ADR-03) is resolved in the transform and never reaches the core model.
`ref_accessibility` additionally carries an `is_guaranteed` flag, set only for
code 1, so Q5 aggregates on a named property instead of hard-coding `= 1` in five
places.

**Consequences.** A magic number becomes both a joinable label and a validity
constraint. Because `foreign_keys = ON` is set on every connection (ADR-02), an
unrecognised code from a future feed aborts the load loudly instead of skewing an
average quietly. `PRAGMA foreign_key_check` returns clean on the built database.
The tables also document the gap between what GTFS permits and what SNCB uses: 10
route types are defined, 2 appear; 4 transfer types are defined, 1 appears; 2
exception types are defined, 1 appears. That gap is itself a finding, and it is
visible without leaving SQL.

**Alternatives rejected.** *`CHECK (route_type IN (0,1,2,...))`:* validates but
carries no label, so every report re-invents the mapping. *Resolve labels in the
dashboard:* moves a data definition into the presentation layer, where two charts
can disagree.

---

## ADR-06 — Bad rows are quarantined, never dropped

**Status:** Accepted.

**Context.** A pipeline that discards rows it dislikes produces clean-looking
output and destroys the evidence. Every filter in `03_transform.sql` is a
judgement about real data, and each of those judgements can be wrong.

**Decision.** `rejected_row` records every refused row with its source table, its
physical line number in the source file, the rule code that rejected it
(`DQ-03-IMPLAUSIBLE-DEPARTURE`, `DQ-04-ORPHAN-STOP-TIME-TRIP`, …), a
human-readable reason, and a JSON snapshot of the payload. The `stop_time`
transform makes the split explicit: a temporary table evaluates every rule once
into a single `reject_rule` column, then two statements partition on
`reject_rule IS NULL`, so every staged row lands in exactly one of the two.
`rejected_row` and `ingestion_run` are the only tables `02_schema.sql` does not
drop on rebuild — the transform deletes just the entries belonging to the static
feed it is about to reload.

**Consequences.** Every rejection is traceable to a line of a file, and the
quarantine is queryable next to the data it was excluded from. On this feed the
result is 12 rejected rows, all from DQ-03: calls timed 48 hours or more into
their own service day, spanning `63:18:00` to `87:39:00` and belonging to just
two trips. GTFS legitimately allows times past 24:00:00, so the threshold is a
judgement — and because the rows are retained, that judgement can be reversed by
reading them rather than re-downloading the feed. The seven orphan and duplicate
rule codes under DQ-04, DQ-05 and DQ-09 caught nothing here; they stay, because
"zero today" is a measurement, not a guarantee.

**Alternatives rejected.** *`WHERE` the bad rows away:* silent, and the row count
never adds up. *Abort the build on any bad row:* one implausible call in 2.17 M
would block the entire analysis.

---

## ADR-07 — Weekly frequency is derived from `calendar_dates`, and read modally

**Status:** Accepted.

**Context.** Q4 asks how many days a week each service runs. `calendar.txt` exists
to answer exactly that, and in this feed it does not: all seven weekday flags are
`0` for all 51,593 services (DQ-01). The operating pattern lives entirely in
`calendar_dates.txt` — 4,697,139 rows, every one `exception_type = 1` (ADDED).
The GTFS weekly pattern therefore has to be reconstructed from exploded dates,
and "N days a week" turns out to have more than one defensible reading.

**Decision.** The `service` table keeps the weekday columns (they are part of the
GTFS contract and a future feed may populate them) but adds
`has_weekday_pattern`, which is `0` for every row here, so no downstream query can
silently trust a column carrying no signal. `v_service_frequency` derives three
measures and classifies on the third:

| Measure | What it asks | Weakness |
|---|---|---|
| `distinct_weekdays` | how many of the 7 weekdays the service *ever* touches | a service running one Monday and one Friday all year scores 2 |
| `max_days_per_week` | its busiest week | rewards a single anomalous week |
| `typical_days_per_week` | the **modal** days-per-week across the weeks it runs at all | needs a tie-break rule |

The classification uses `typical_days_per_week`, with ties broken towards the
busier week. Weeks are bucketed by whole weeks from a fixed Monday epoch
(1970-01-05) rather than `strftime('%W')`, which resets at the year boundary and
would split the week straddling 2025-12-29 into two half-weeks.

**Consequences.** The two readings disagree for 22,139 of 51,593 services (42.91%),
and the choice moves the headline answer materially:

| Class | Modal reading (used) | Distinct-weekday reading |
|---|---|---|
| High Frequency (≥5 d/wk) | 23,340 — 45.24% | 32,124 — 62.26% |
| Medium Frequency (2–4) | 17,877 — 34.65% | 16,262 — 31.52% |
| Low Frequency/Special (1) | 10,376 — 20.11% | 3,207 — 6.22% |

The modal reading is chosen because "operates 5 days a week" is a claim about a
typical week, not about the union of every week. The distinct-weekday reading
would promote 8,784 services into "High Frequency" on the strength of dates they
touch once. The definition is pinned inside the view so Q4, the dashboard and any
ad-hoc query cannot drift apart.

**Alternatives rejected.** *Trust `calendar.txt`:* would classify all 51,593
services as running zero days a week. *Use `distinct_weekdays`:* simpler and
flattering, but it answers a different question.

---

## ADR-08 — Departures are annualised by operating days, and the naive count is published beside them

**Status:** Accepted.

**Context.** This is the most consequential analytical decision in the project.
The feed is a year-long timetable: 134,809 trips, each attached to its own service
calendar, spanning 2025-12-20 → 2026-12-12. Each call of a trip is exactly one
row in `stop_times`, whether that trip runs 250 times a year or once — the file
carries no repetition count at all. So `SELECT hour, COUNT(*) … GROUP BY
hour` — the obvious reading of "highest volume of scheduled departures" — measures
rows in a timetable file, not trains that leave a platform.

**Decision.** `v_trip_service_days` counts the operating days of every trip, and
Q1, Q2 and Q3 weight each boardable call by that number. The naive count is
reported in the same file, as a named contrast, never as a replacement.

**Consequences.** The answer changes, and so does the advice that follows from it.

| Metric | Naive rank 1 | Annualised rank 1 |
|---|---|---|
| Q1 peak hour | 10:00 (94,323 calls) | **17:00** (950,651 departures, 6.51% of the network day) |
| Q1 hour 17 | rank 10 (76,736 calls) | rank 1 |
| Q1 hour 10 | rank 1 | rank 8 (830,433) |
| Q2 busiest platform, Bruxelles-Central | platform 3 (11,982 calls) | **platform 4** (63,426 departures) |
| Q3 top morning destination | Anvers-Central (3,930 trips) | Anvers-Central (41,972 trips) |

The annualised profile shows the 07:00 and 17:00 commuter peaks any rail planner
would expect; the naive profile shows a midday bulge that is an artefact of
seasonal and off-peak services being over-represented one row each. Publishing
both is not hedging — the gap *is* the finding, and a capacity decision taken from
the naive count would invest in the wrong hour and, at Bruxelles-Central, in the
wrong platform. The cost is that every headline number must be qualified as
"annualised over the feed window", and that the weighting join is the most
expensive operation in the analysis pack.

**Alternatives rejected.** *Naive counts only:* the literal reading of the brief.
It changes the headline answer on two of the five questions (Q1's hour, Q2's
platform) and reorders Q3's top three, promoting Bruxelles-Midi over Louvain and
Charleroi-Central on 3,150 timetable rows that run comparatively rarely. *Pick
one representative date and count it:*
defensible and much cheaper, but the choice of date silently decides the answer,
and no single date covers both school-term and holiday patterns. *Annualised
only:* hides the methodology from a reader who would otherwise reproduce the naive
number and conclude the report is wrong.

---

## ADR-09 — The real-time `trip_id` is a soft link, not a foreign key

**Status:** Accepted.

**Context.** Everything else in this schema is bound by enforced foreign keys
(ADR-05), and `PRAGMA foreign_key_check` is clean. `rt_trip_update.trip_id` and
`rt_stop_time_update.stop_id` are the deliberate exceptions, and the exception
needs justifying rather than assuming.

**Context in detail.** The static feed is regenerated daily; the real-time feed
references whatever timetable is live *right now*. Between an upstream publish and
the next local `railpulse build`, real-time rows legitimately name trips this
snapshot has never seen — and a brand-new or re-planned service is exactly the
observation an operations analyst most wants.

**Decision.** `trip_id`, `route_id` and `stop_id` on the `rt_*` tables are plain
columns with no `REFERENCES` clause. The foreign keys *within* the real-time
tables — snapshot to trip-update to stop-time-update, all `ON DELETE CASCADE` —
are fully enforced, because those relationships are internally generated and
cannot drift.

**Consequences.** The link is soft but not unexamined. `v_rt_departure_performance`
INNER JOINs `rt_trip_update` to `trip`, so any punctuality query silently and
correctly ignores unmatched rows rather than reporting them as delay-free, and the
match rate is intended to be reported as a post-build check (`railpulse verify` in
the module map in `src/railpulse/__init__.py`; that module is not written yet).
The poller has now written its first snapshots and every real-time `trip_id`
recorded so far resolves against the current static feed — zero unmatched. That
figure proves very little: it is a few hundred trip-updates polled within hours
of a rebuild, which is exactly the case a hard foreign key would also have
survived, and the tables grow with every poll so the number is a moving one. It
says nothing about the drift window this design exists to cover. The guarantee on
offer is that drift cannot destroy history, not that drift is small.

**Alternatives rejected.** *A hard FK on `trip_id`:* rejects the most interesting
observations at insert time and, worse, would let a rebuild cascade-delete
irreplaceable history (ADR-10). *Nullify unmatched `trip_id`s on load:* Python
making a decision about a value, which ADR-01 forbids, and it discards the
operator's own identifier.

---

## ADR-10 — `rt_*` tables are additive; the static core is dropped and rebuilt

**Status:** Accepted.

**Context.** The two halves of this database have opposite reproducibility
properties, and treating them the same way would destroy one of them. The static
feed is a 26 MB zip that can be re-downloaded at will: nothing derived from it is
precious. A real-time observation is a measurement of a moment — once 06:12's
delays are unrecorded, they are unrecoverable at any price.

**Decision.** `02_schema.sql` opens with `DROP TABLE IF EXISTS` in reverse
dependency order and rebuilds the entire static core from scratch on every run.
`06_realtime.sql` uses `CREATE TABLE IF NOT EXISTS` throughout and drops no
table — only its own view, `v_rt_departure_performance`, which holds no data —
so `railpulse build` leaves accumulated observations untouched.
`ingestion_run` and `rejected_row` follow the real-time rule rather than the
static one — they are the build's own audit log, and wiping them would erase the
record of what previous loads did.

**Consequences.** A rebuild is genuinely safe to run at any time, which is what
makes the pipeline pleasant to iterate on: every DQ rule can be changed and
re-run in minutes with no fear of losing data. `rt_snapshot` carries
`UNIQUE (feed, feed_timestamp_epoch)` as the idempotency guard, because an
additive table with an over-eager cron job double-counts every delay. The
asymmetry has to be remembered: a schema change to an `rt_*` table will not be
picked up by a rebuild, and will need an explicit migration. That is the price of
not being able to re-derive the data.

**Alternatives rejected.** *Drop everything:* simplest, and it throws away the
only irreplaceable data in the project. *Additive static tables too:* `INSERT OR
REPLACE` across a changing feed leaves stale trips and services behind with no
signal that they are stale.

---

## ADR-11 — Indexes are built after the bulk load, and only when a query names them

**Status:** Accepted.

**Context.** `04_indexes.sql` runs as step 5 of 8 in `build.py`, after
`03_transform.sql`, not as part of the schema in step 2. Two separate decisions
are bundled there: *when* indexes are created, and *which* ones exist at all.

**Decision — when.** Indexes are created after the data is in place. Building an
index while 2.17 M rows stream in means re-balancing a B-tree on every `INSERT`;
loading first lets SQLite sort once and write the tree sequentially. The file runs
with `atomic=False` because index creation does not need the all-or-nothing
guarantee the transform does, and `ANALYZE` closes it so the planner chooses from
real distributions rather than row-count guesses — it matters here, because
`is_boardable = 1` selects 67% of `stop_time` and is a poor leading filter on its
own.

**Decision — which.** Thirteen indexes exist: ten in `04_indexes.sql` and three in
`06_realtime.sql`. Every one is justified by a named query, and the leading column
of each composite is the equality predicate with the grouping column following.
Where an index would have been redundant, the file says so rather than staying
silent: Q3 needs `MIN(stop_sequence)` per trip, which the `(trip_id,
stop_sequence)` primary key already serves, so no index is created and a comment
records that it was considered.

**Consequences.** Faster builds, and no index that nobody can name a query for.
Indexes are not free — they cost disk and write throughput on every reload — so
unjustified ones are technical debt. The discipline has a maintenance obligation
attached: an index whose query is deleted should be deleted with it.

**Alternatives rejected.** *Declare indexes in `02_schema.sql`:* keeps the schema
in one file, at a measurable cost on every rebuild. *Index every foreign key by
reflex:* several would never be sought, and each would slow the 2.2 M-row load.

---

## ADR-12 — Staging is dropped after a successful build, and the database is not committed

**Status:** Accepted.

**Context.** ADR-01 buys reviewable cleaning rules by keeping a complete untyped
copy of the feed in `stg_*`. Measured with `dbstat` on the built file, that copy
occupies 480 MB against 1,067 MB for the core model and its indexes — roughly a
third of the database, and not one analytical statement reads it.

**Decision.** `07_cleanup.sql` drops all ten staging tables once the transform has
succeeded, and `build.py` follows with `VACUUM` (outside a transaction, which is
why that file runs with `atomic=False`) so the freed pages return to the operating
system, then re-runs `ANALYZE`. `railpulse build --keep-staging` retains them for
debugging a transform rule against the exact bytes that produced it, or inspecting
a row named by `rejected_row.src_line_no`. Separately, `.gitignore` excludes
`data/*.db`, its `-wal`/`-shm` companions and `data/raw/*`: the database is a
build artefact, fully reproducible from `railpulse build`, and at 1.5 GB it does
not belong in version control. Note that the zip is ignored along with the
database, so a fresh clone must download the feed once; `--offline` only helps a
working copy that already has it.

**Consequences.** The shipped database contains only what is queried, and the
repository stays small enough to clone. Reproducibility becomes a hard requirement
rather than a nice property — if the build cannot recreate the database, there is
no database. Note that the current `data/railpulse.db` was built with
`--keep-staging` and still contains all ten `stg_*` tables; that is why the file
measures 1,547 MB rather than the ~1,067 MB a default build produces.

**Alternatives rejected.** *Keep staging permanently:* a third of the file for
data with no reader. *Commit the database so evaluators need no API key:*
convenient, and it would put a 1.5 GB binary into git history permanently while
letting the pipeline rot unnoticed. *Never stage at all:* forfeits ADR-01.

---

## ADR-13 — pandas is confined to the rendering layer

**Status:** Accepted.

**Context.** The brief states two things about pandas. It is **not** allowed to
filter or aggregate data — "Python must *only* be used for the network `requests`
and executing raw SQL via `sqlite3`" — and it *is* optionally permitted "as a way
to visualize the data or to insert data into your database". The interesting
question is where the line sits, because a `DataFrame` that has been handed
already-aggregated rows is doing no analysis, while one line of `.groupby()` puts
a number outside SQL where no reviewer will find it.

**Decision.** The pipeline never imports it. `requirements.txt` pins exactly two
third-party packages — `requests` for the network and `python-dotenv` for the API
key — and everything else in `src/railpulse/` is the standard library (`csv`,
`io`, `zipfile`, `sqlite3`, `pathlib`). pandas lives only in
`requirements-dashboard.txt`, an optional install alongside Streamlit and Altair,
where its sole job is to wrap rows that a file in `sql/analysis/` has already
aggregated so a chart library can draw them. The rule is stated in both
requirements files so it is visible at the point of temptation.

**Consequences.** Every number in the report traces to a `.sql` file that can be
run by hand, and the dependency footprint of the pipeline is two packages. The
boundary is verifiable rather than aspirational: `grep -rn pandas src/` returns
exactly one line, the sentence in the package docstring that states the rule.
There is no import, in any module, and no `numpy`, `polars` or `pyarrow`
either. The cost is that some
reshaping is more verbose in SQL than it would be in a `pivot_table`; the Q1
weekday/weekend split is written as a `UNION ALL` unpivot for that reason.

**Alternatives rejected.** *pandas for the ingest loop:* explicitly forbidden, and
`read_csv` on a 2.2 M-row file would load it whole where the current loader streams
in 50,000-row batches. *No pandas at all:* the brief permits it for visualisation,
and rejecting it would mean hand-rolling row-to-chart plumbing for no gain.
