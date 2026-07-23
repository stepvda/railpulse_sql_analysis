# 📖 Glossary — every term this project uses

This project sits on top of two specialist vocabularies: **public-transport
data** (GTFS) and **relational databases**. It also invents a handful of terms
of its own. If any word in this repository stopped you, it is defined here.

Entries are ordered so you can read the page top to bottom as a primer, not just
look words up. Each one says what the term means, and — where it matters —
*why this project cares*.

**Contents**

- [Part 1 — Public transport and GTFS](#part-1--public-transport-and-gtfs)
- [Part 2 — Databases, SQL and modelling](#part-2--databases-sql-and-modelling)
- [Part 3 — Performance vocabulary](#part-3--performance-vocabulary)
- [Part 4 — Words this project invented](#part-4--words-this-project-invented)
- [Part 5 — The three ideas that shape everything](#part-5--the-three-ideas-that-shape-everything)

---

## Part 1 — Public transport and GTFS

### GTFS (General Transit Feed Specification)

The worldwide standard format for publishing public-transport timetables. It
grew out of a collaboration between Google and TriMet, the transit agency in
Portland, Oregon, around 2005–2006 — the goal was to get transit schedules into
Google Maps. It was called the *Google* Transit Feed Specification at first and
renamed *General* when it outgrew that; it is now used by thousands of operators
worldwide, including every Belgian one.

Physically, a GTFS feed is just **a ZIP file full of CSV files**. That is the
whole format. SNCB's is 26 MB and contains ten `.txt` files that are really
comma-separated tables:

| File | One row is | Rows in SNCB's feed |
|---|---|---|
| `agency.txt` | the operator | 1 |
| `stops.txt` | a station **or** a platform | 2,895 |
| `routes.txt` | a commercial line | 1,801 |
| `trips.txt` | one journey of one vehicle | 134,809 |
| `stop_times.txt` | one stop made by one journey | 2,165,519 |
| `calendar.txt` | a pattern of operating days | 51,593 |
| `calendar_dates.txt` | one specific operating date | 4,697,139 |
| `transfers.txt` | a connection between platforms | 733 |
| `translations.txt` | a name in another language | 2,599 |
| `feed_info.txt` | who published this and when | 1 |

Because it is only CSV, GTFS has **no types, no constraints and no foreign
keys**. A departure time is the text `"07:42:00"`. Nothing stops a trip
referencing a route that does not exist. Turning that into a database with real
constraints is precisely what Sprint 1 of this project is.

### GTFS Static vs GTFS Realtime (GTFS-RT)

- **Static** is the published plan: what *should* happen, for months ahead. It
  is regenerated once a day. This is the 26 MB ZIP above.
- **Realtime (GTFS-RT)** is what *is* happening right now: delays,
  cancellations, disruption messages. It refreshes roughly every 30 seconds and
  only describes the next few hours.

The static feed can never tell you whether a train was late — a plan is by
definition never late. Any punctuality claim needs the realtime feed, which is
why this project polls and stores it (`rt_*` tables) as well.

> **Note specific to this project:** the Belgian portal documents its realtime
> feeds as *Protocol Buffers* (a compact binary format that normally needs a
> special library to read). The gateway actually serves **JSON**. That is
> convenient — it means the project needs no extra dependency — but it is not
> what the documentation says, so it is worth knowing.

### Agency

The transport operator. Here: NMBS/SNCB, the Belgian national railway. (NMBS is
the Dutch name, SNCB the French; Belgium publishes both.) One row in this feed,
but the schema keeps it as a proper table so De Lijn or TEC could be loaded into
the same model unchanged.

### Route (and why it is not a "line")

A **route** is a commercial service pattern with a name — `IC` (InterCity), `S5`
(a Brussels suburban line), `L` (local/stopping), `P` (peak-only), `BUS` (a
replacement bus). It is *not* a physical railway line and *not* a single train.

SNCB publishes 1,801 routes: 1,531 rail and 270 rail-replacement bus.

### Trip

**One journey, by one vehicle, on one date-pattern, in one direction.** The
07:42 Brussels→Antwerp is one trip. The 08:42 is a *different* trip, even though
it is the same route with the same stops.

134,809 trips in this feed.

> ⚠️ **A trip is NOT a commercial train, and assuming it is will give you wrong
> answers.** SNCB splits each published train number into an average of **21.3
> separate trip rows**, because the exact stopping pattern and timings drift
> across the year and every variant needs its own row. Train 522 appears as
> **182 distinct trip rows**. Each trip row covers an average of only **9.3
> dates**; the longest-lived trip in the whole feed covers 318, and only 565 of
> the 134,809 reach 200.
>
> This is the single fact behind the Q1 result. Counting trip rows counts
> *variants*, not trains — see [annualised
> departures](#annualised-departures).

### Stop, station, platform, and `parent_station`

GTFS puts two different things in one file, distinguished by a column called
`location_type`:

- `location_type = 1` → a **station**. "Bruxelles-Central". What a passenger
  means when they name a place. 652 of these.
- `location_type = 0` → a **stop**, in practice a **platform**. "Bruxelles-
  Central platform 4". 2,243 of these. Each names its station in a column
  called `parent_station`.

Trains are timetabled against *platforms*, not stations — which is what makes
the "busiest platform" question (Q2) answerable at all.

> **A quirk worth knowing:** every station in this feed also owns exactly one
> platform-shaped child with **no platform number**. The feed uses it for calls
> where no track has been allocated yet. At Bruxelles-Central, 1,348 departures
> sit on it. This project reports them separately rather than pretending they
> belong to a platform.

This project splits GTFS's single `stops.txt` into two tables, `station` and
`platform` — see [Normalisation](#normalisation-and-the-normal-forms) for why.

### Stop time (a "call")

**One trip stopping at one platform, once.** It records the arrival time, the
departure time, and whether passengers may board or alight.

This is the project's **fact table** — the event grain everything is really
about — at **2,165,507 rows**. A trip with 16 stops produces 16 stop_times.
(One table is longer still: `service_date`, the exploded calendar, at 4,697,139
rows. But that is a dimension-support table of dates, not the events the
analysis measures.)

This document — and the project — uses the word **call** as a synonym, because
"stop time" reads like a clock value rather than an event. "A call at platform
4" is one train stopping there once.

### `pickup_type` and `drop_off_type` (and pass-through calls)

Two small integers on every call:

| Value | Meaning |
|---|---|
| `0` | normal — passengers may board / alight |
| `1` | **not available** — they may not |
| `2` | must phone the agency |
| `3` | must arrange with the driver |

When **both** are `1`, the train physically passes and serves the platform but
**nobody may get on or off**. This project calls those **pass-through calls**,
and there are **577,462** of them — over a quarter of the whole table.

This matters enormously. Counting them as "departures" would inflate the
network-wide total by **49%** — and unevenly, which is worse than a uniform
error because it changes the *order* of any ranking. Anvers-Central would gain
74.2%; Bruxelles-Central 0.1%. (A pass-through is a train with no commercial
business at that station, so through-stations collect them and terminal-heavy
stations do not.) The `v_departure` view excludes them once, so no query has to
remember to.

### Headsign (`trip_headsign`)

The destination displayed on the front of the train and on the platform screen —
literally the sign above the driver's head. "Anvers-Central". It is the trip's
**final** destination, which is why Q3 ("busiest morning destinations") groups
by it.

### Service and service calendar

A **service** is not a train. It is a **pattern of dates** — an answer to "on
which days does this run?" Every trip points at one.

GTFS offers two ways to express it, and this feed's choice is the single most
consequential quirk in the whole project:

1. `calendar.txt` — seven yes/no weekday flags plus a date range.
   *"Runs Monday to Friday from December to June."*
2. `calendar_dates.txt` — an explicit list of individual dates.
   *"Runs on 5 Jan, 6 Jan, 7 Jan…"*

**SNCB publishes all seven weekday flags as `0` for all 51,593 services** and
puts the entire real calendar in `calendar_dates.txt` — **4,697,139 individual
dates**. So the obvious query for Q4 (`CASE WHEN monday + ... + sunday >= 5`)
classifies the entire Belgian rail network as "Low Frequency". The weekly
rhythm has to be *derived* from the 4.7 million dates instead. See
[`data_quality.md`](data_quality.md) rule DQ-01.

### Service day (and why times can read `25:30:00`)

A **service day** is an operational day, not a calendar day. A train that leaves
at 00:20 on Sunday morning belongs to *Saturday's* service — same crew roster,
same timetable page, same everything except the date on the wall.

GTFS expresses this by letting clock times **run past 24:00**. A departure at
twenty past midnight on a Saturday service is published as `"24:20:00"`. This
feed publishes **31,154** such calls, of which **31,142** load (the other 12 are
quarantined by DQ-03 for being 48 hours or more into their own service day).

Three consequences this project handles explicitly:

- `"24:20:00"` is **not a valid time** to most software. Parsing it with a
  normal time function fails or silently returns null.
- The **passenger-facing hour is 0**, not 24. Counting `24:20:00` in "hour 24"
  invents an hour that does not exist and empties the real hour 0.
- The project therefore stores three columns: the raw text (`departure_time`),
  the seconds since the service day began (`departure_secs` = 87,600), and the
  real clock hour (`departure_hour` = 0). Plus `day_offset` = 1, meaning "this
  happens on the next calendar day".

### `bikes_allowed` and `wheelchair_accessible`

Two amenity fields on each trip, both using the same three-value code:

| Code | Meaning |
|---|---|
| `0` | **no information** — the operator said nothing |
| `1` | **yes** — explicitly guaranteed |
| `2` | **no** — explicitly refused |

**Code `0` is a silence, not a refusal.** Treating "no information" as "not
accessible" invents a fact, and it is the single most common error in
accessibility reporting. This project counts only code `1` as a guarantee and
reports `0` in its own column — which turns out to *be* the finding for Q5,
because `wheelchair_accessible` is `0` for **all 134,809 trips**.

### Block (`block_id`)

A GTFS *block* ties together trips worked by the **same physical vehicle** in
sequence. Two trips with the same `block_id` are the same train continuing under
a new trip identity, so a passenger can stay aboard from one to the next without
changing. It is populated on every trip in this feed and carried into the model
for completeness; no Sprint-1 analysis uses it.

### Transfer

A minimum connection time between two platforms — how long a passenger needs to
change trains. 733 rows here. **725** connect two platforms of the *same*
station, and of those **659** name the same platform twice (`from_stop_id =
to_stop_id`) — the feed's way of stating a dwell/turnaround time at one track
rather than a walk between two.

### Liveboard

The departures board at a station: the next N trains, with their delays. The
project brief mentions the iRail API's liveboard endpoint; this project derives
the same information from GTFS instead, because one authenticated request gets
the whole national timetable rather than one polled request per station.

---

## Part 2 — Databases, SQL and modelling

### Schema

The structure of a database: which tables exist, which columns they have, what
types those are, and which rules they must obey. `sql/02_schema.sql` is this
project's schema.

### Primary key (PK)

The column (or set of columns) that uniquely identifies a row. `station_id`
identifies a station. No two rows may share one, and it may never be empty.

A **composite** primary key uses more than one column together. `stop_time`'s
key is `(trip_id, stop_sequence)` — "the 5th stop of trip X" — because neither
alone is unique.

### Foreign key (FK)

A column that must contain a value that exists in another table's primary key.
`trip.route_id` must name a real route. It is the database *enforcing* that
relationship: an insert that breaks it is rejected.

> ⚠️ **SQLite disables foreign keys by default**, for historical
> compatibility. Without `PRAGMA foreign_keys = ON`, every `REFERENCES` clause
> in a schema file is a comment with extra steps. This project sets it on every
> single connection (`src/railpulse/db.py`), which is why it can claim 34
> *enforced* foreign keys rather than 34 aspirational ones.

### Unique key / constraint

Guarantees no two rows repeat a value, without that value being the primary key.
`platform` has `UNIQUE (station_id, platform_code)`: one station cannot have two
"platform 4"s.

### Cardinality

How many rows on each side of a relationship:

- **one-to-many** — one station has many platforms. The commonest kind.
- **many-to-one** — the same relationship read backwards: many trips belong to
  one route.
- **one-to-one** — rare; usually a sign two tables should be one.
- **many-to-many** — many trips call at many platforms. A relational database
  cannot store this directly; it needs a third table in between. `stop_time`
  *is* that table, and it carries its own data (times, boarding rules), which
  is what makes it interesting.

### Grain

**What exactly one row of this table represents.** The most important sentence
in any table's documentation, and the one most often missing.

- `trip` — one journey.
- `stop_time` — one journey stopping at one platform, once.
- `service_date` — one service pattern being active on one date.

Getting the grain wrong is how you accidentally count the same train eight times.

### Normalisation and the normal forms

Organising tables so each fact is stored **exactly once**. If a fact lives in
two places, the two places will eventually disagree — and a database that
contradicts itself is worse than no database.

The three normal forms that matter in practice:

| Form | Rule | In plain terms |
|---|---|---|
| **1NF** | no repeating groups; every cell holds one value | don't put four languages in four columns `name_fr, name_nl, name_de, name_en` — use a rows-per-language table |
| **2NF** | 1NF, and no column depends on only *part* of a composite key | if the key is `(trip_id, stop_sequence)`, a column that depends only on `trip_id` belongs on `trip` |
| **3NF** | 2NF, and no column depends on another *non-key* column | see below |

**A worked 3NF example from this project.** GTFS's `stops.txt` gives every
platform a `stop_name`. But a platform's name is determined by its *station*,
not by the platform itself — it is `station_id` that decides it. That is a
**transitive dependency**: `stop_id → station_id → station_name`. Storing
`station_name` on `platform` would mean the name of Bruxelles-Central is written
7 times, and a rename could update 6 of them.

So this project stores `station_name` on `station` only, and `platform` joins to
get it. (Verified before deciding: **0 of 2,243** child stops disagreed with
their parent's name, so nothing was lost.)

### Denormalisation

Deliberately breaking normalisation for speed, with your eyes open.

This project does it once, on purpose, and documents it: `stop_time` stores
`departure_secs`, `departure_hour`, `day_offset` and `is_boardable`, all of
which are *derivable* from `departure_time` and `pickup_type`. Storing them
makes the hourly histogram roughly **100× faster** (see
[SARGable](#sargable-and-sargable-violations)). The trade is real and stated:
those columns can drift from their source, so `railpulse verify` asserts they
still agree.

### Surrogate key vs natural key

- **Natural key** — a real-world identifier that already exists. `route_id`
  from the feed.
- **Surrogate key** — an invented number with no meaning. `transfer.transfer_id`
  is one, because the "natural" key `(from_stop_id, to_stop_id)` turns out not
  to be unique once trip-specific transfers exist.

### Fact table vs dimension table

The vocabulary of analytical modelling:

- A **fact table** records *events*. It is long and narrow, grows forever, and
  its columns are mostly measurements and keys pointing elsewhere.
  **`stop_time` is this project's fact table** — 2.17M rows, one per event.
- A **dimension table** records *things* you describe events by. Short, wide,
  full of names and attributes. `station`, `route`, `trip`, `service`.

The test: "would I say `SELECT COUNT(*)` on this and call it a business
number?" 2.17M calls is a number. 652 stations is inventory.

### Star schema vs snowflake schema

- **Star** — one fact table, dimensions hanging directly off it. Fewer joins,
  some duplication.
- **Snowflake** — dimensions themselves split into further tables. More joins,
  less duplication.

`stop_time → platform → station` is a **snowflaked** dimension: you traverse two
hops to get a station name. That was the right trade here (a station's name is
stored once), but it is a trade, not a free win.

### Staging table

A table that holds **raw, untouched input** before anything is done to it. All
text, no constraints — because staging must be able to hold *bad* data. That is
the point: a row that violates a rule can be set aside **with an explanation**
rather than crashing the load or being silently dropped.

This project's staging tables are named `stg_*` and are deleted after a
successful build.

### ETL vs ELT

- **ETL** (Extract → Transform → Load): clean the data in your program, then
  put the clean result in the database.
- **ELT** (Extract → **Load** → Transform): put the raw data in the database
  first, then clean it *with SQL*.

**This project is ELT.** Python copies CSV rows in verbatim; every cleaning rule
is a SQL statement in `sql/03_transform.sql`. Two reasons: the challenge forbids
pandas, and — more usefully — it means every rule is readable in one file, in
one language, by anyone who can read SQL.

### View

A **saved query** that behaves like a table. It stores no data; it re-runs each
time you select from it.

Its real job here is **defining a word once**. `v_departure` encodes what this
project means by "a departure" (boardable, has a published time). Without it,
five analysts write five slightly different `WHERE` clauses and produce five
different answers to the same question.

### CTE (Common Table Expression)

The `WITH name AS (SELECT ...)` block at the top of a query. A named temporary
result you can refer to below, which turns one unreadable nested query into a
readable sequence of steps. Used heavily in `sql/analysis/`.

### Subquery

A query inside another query. A **correlated** subquery is one that references
the outer query and therefore runs once *per outer row* — which is usually where
the performance problem is.

### Window function

An aggregate that **does not collapse rows**. `SUM(x)` over a group returns one
row; `SUM(x) OVER (...)` returns every row, each with the group's total attached.

This project uses them for:

- `ROW_NUMBER() OVER (PARTITION BY trip_id ORDER BY stop_sequence)` — number
  each trip's calls so we can keep only the first (the trip's origin).
- `RANK() OVER (ORDER BY departures DESC)` — rank hours without a second pass.
- `SUM(x) OVER ()` — put the grand total on every row, to compute a percentage
  without querying twice.

### Transaction, and ACID

A **transaction** is a group of statements that either **all** happen or **none**
do. **ACID** is the four guarantees a real database makes about them:

| Letter | Guarantee |
|---|---|
| **A**tomicity | all-or-nothing; no half-finished work |
| **C**onsistency | constraints hold before and after |
| **I**solation | concurrent transactions don't see each other's half-done work |
| **D**urability | once committed, it survives a crash |

Concretely here: `03_transform.sql` runs as one transaction. If it fails on the
statement that loads the 2.17-millionth call, the database rolls back to empty
rather than leaving you with a fact table that is 80% populated and looks fine.

> **Careful:** the "C" in ACID and the "C" in CAP are different words that
> happen to share a letter. See [`SQL&DB_theory.md`](../SQL&DB_theory.md) §4.

### Idempotent

Running it twice does the same thing as running it once.

The realtime poller is idempotent: `rt_snapshot` has a `UNIQUE` constraint on
`(feed, feed_timestamp)`, so polling again before the operator has published
anything new is skipped rather than counting every delay twice.

### PRAGMA

SQLite's command for engine settings — not standard SQL, SQLite-specific.
`PRAGMA foreign_keys = ON`, `PRAGMA journal_mode = WAL`.

### WAL (Write-Ahead Logging)

A SQLite mode where changes are appended to a side file before being folded into
the main database. The practical benefit: **readers are not blocked by a
writer**. That is why the Streamlit dashboard can query the database while a
build is writing to it.

WAL does **not** help writer-versus-writer contention — there is still only ever
one writer.

### `WITHOUT ROWID`

A SQLite table where rows are stored *inside* the primary-key index instead of
in a separate table addressed by a hidden `rowid`. It saves space and removes
one lookup, and is worth choosing when the primary key is most of the row.

Used here on `service_date`, whose two key columns are nearly the whole row. A
normal table would additionally hold a separate primary-key index containing
`service_id` + `service_date` + `rowid` for all 4,697,139 rows — on the order of
150 MB, by arithmetic on the column widths. (That is an estimate from the data
sizes, not a measured before/after.)

---

## Part 3 — Performance vocabulary

### Index

A sorted lookup structure (a B-tree) that lets the engine jump straight to the
rows it wants instead of reading every row. The cost: disk space, and slower
writes, because every insert must also update every index.

An index nobody can name a query for is pure cost. Every index in
`sql/04_indexes.sql` names the query that justifies it.

### Index seek vs index scan (SQLite says SEARCH vs SCAN)

The two words to look for in a query plan:

- **SEARCH** (a *seek*) — the engine jumps directly to the matching rows. Cost
  grows with the size of the **answer**.
- **SCAN** — the engine reads everything and tests each row. Cost grows with the
  size of the **table**, no matter how small the answer is.

On a 2.17-million-row table that difference is roughly everything.

### Covering index

An index that happens to contain **every column the query asked for**, so the
engine never opens the table at all. The plan says `USING COVERING INDEX`, and
it is the best outcome available.

### `EXPLAIN QUERY PLAN`

Prefix any SQL statement with it and SQLite tells you *how* it intends to answer
— which index it will use, whether it will scan, whether it will need a
temporary sort. It does not run the query. See
[`q7_index_optimisation.sql`](../sql/analysis/q7_index_optimisation.sql).

Important limitation: a plan describes **access paths only**. It cannot see
per-row work. Two queries with identical plans can differ 2× in runtime because
one calls a date function 4.7 million times. Plans tell you where to look; only
a stopwatch tells you what it cost.

### SARGable, and SARGable violations

**SARGable** = "Search ARGument able" = a `WHERE` or `GROUP BY` condition the
engine can satisfy **using an index**.

The rule is simple: **the moment you wrap an indexed column in a function, the
index becomes unusable.** The engine can look up `departure_hour = 17` in a
sorted tree; it cannot look up `strftime('%H', departure_time) = '17'`, because
it has no idea what `strftime` will return until it computes it — for every
single row.

```sql
-- SARGable: the index answers this directly
WHERE departure_hour = 17

-- SARGable violation: the index cannot be used at all
WHERE CAST(strftime('%H', departure_time) AS INTEGER) = 17
```

Measured on this project's data, the second form costs **about 100× more time**
for an identical answer. That measurement is the entire justification for
storing `departure_hour` as a column — see
[Denormalisation](#denormalisation).

The same trap: `WHERE substr(stop_id, 1, 21) = '...'` instead of
`WHERE stop_id = '...'` (~500× slower here), and the classic
`WHERE strftime('%Y', some_date) = '2026'` instead of
`WHERE some_date >= '2026-01-01' AND some_date < '2027-01-01'`.

### `ANALYZE`

Tells SQLite to measure the actual distribution of data in each table and index,
and store it. Without it, the planner guesses from row counts; with it, it
chooses from reality. Run at the end of every build.

### `VACUUM`

Rebuilds the database file compactly, returning freed space to the operating
system. Run after dropping the staging tables, which is why the finished
database is roughly 1 GB rather than the ~1.5 GB peak it reaches mid-build.

---

## Part 4 — Words this project invented

These are not standard terminology. They are defined here because the project
uses them constantly.

### Boardable call

A call at which a passenger may actually board — `pickup_type <> 1`. The
`v_departure` view counts only these. Excludes the 577,462
[pass-through calls](#pickup_type-and-drop_off_type-and-pass-through-calls).

### Annualised departures

**The number of trains that actually depart across the feed's whole year**, as
opposed to the number of rows in the timetable file.

This is the most important idea in the project's analysis, so it is worth being
slow about.

The feed covers 358 dates. Each of its 134,809 trips carries its own service
calendar. A summer-Sunday excursion and a Monday-to-Friday commuter train are
**one row each** in the timetable — but the second one departs roughly 250 times
more often.

So `COUNT(*)` answers *"how many rows are in the file?"*, not *"how many trains
depart?"* Multiplying each call by the number of days its service actually runs
converts one into the other:

```
annualised departures = Σ (each boardable call × days its service operates)
```

It changes the answer to Q1 from **10:00** to **17:00**, and moves the evening
peak from rank 10 to rank 1. Both numbers are reported everywhere in this
project, because the gap between them *is* the finding.

### The naive count

Shorthand for the unweighted `COUNT(*)` version of a number — the one you get
before applying the annualisation above. Always reported alongside the
annualised figure rather than hidden.

### `day_offset`

How many calendar days after the start of its service day a call actually
happens. `0` = same day. `1` = after midnight (published as `24:00:00`–
`47:59:59`). See [Service day](#service-day-and-why-times-can-read-253000).

### Modal days per week

The measure Q4 uses to classify service frequency. For each service, look at
every week in which it runs at all, count its operating days in each, and take
the **most common** count.

Chosen over the two alternatives because it describes a service's *normal* week
and is not moved by one unusual one. All three readings are published side by
side in `q4_definition_sensitivity`, because the headline "High Frequency" share
swings by **17 percentage points** depending on which you pick — and a single
number would imply a precision the question does not have.

### DQ-nn

The nine data-quality rules in `sql/03_transform.sql`, each with a stable tag
(`DQ-01` … `DQ-09`) so a rule can be referenced from a comment, a document, a
quarantined row, or a conversation. Full descriptions:
[`data_quality.md`](data_quality.md).

### The quarantine (`rejected_row`)

The table where rows that fail a DQ rule are put. **Nothing is silently
dropped.** Each quarantined row keeps its source file, its physical line number
in that file, the rule that rejected it, a human-readable reason, and the
original data as JSON.

12 rows are quarantined from this feed, all by DQ-03 (calls published 48 hours
or more into their own service day, the worst at `87:39:00`).

### Soft link

A column that names a row in another table but is **deliberately not** a foreign
key. The project has exactly one: `rt_trip_update.trip_id`.

The reason: the static feed is rebuilt daily, while the realtime feed describes
whatever timetable is live *right now*. In the window between an upstream
publish and your next rebuild, realtime rows legitimately name trips your
database has never seen. A hard foreign key would reject exactly the
observations that matter most — a brand-new or re-planned service — and would
cascade-delete history on every rebuild.

So the link is soft but **measured**: `railpulse verify` reports what percentage
of realtime trips resolve against the current static feed (currently 100%), and
the punctuality view inner-joins so unmatched rows are simply and correctly
ignored.

### Text-to-SQL (the SQL Chat page)

Turning a question written in **plain English** into a SQL query automatically,
using a language model. The dashboard's optional *SQL Chat* page does this: you
type "*top 10 busiest stations by annual departures*", a local model writes the
SQL, and it runs against the read-only database.

Two ideas from this glossary matter for reading its code. It is **guarded by
defence in depth**, not by trusting the model: a [read-only](#wal-write-ahead-logging)
connection (a write simply raises), a text guardrail that only lets a single
`SELECT`/`WITH` through, and execution *caps* — a timeout and a row limit — so a
model-written cartesian join cannot hang the app. And the model is given the
schema as **prompt context**; the rich version of that context is the same
`v_departure`/annualise guidance the rest of this project relies on, so a capable
model produces the same kind of correct SQL a human would. It is a local preview
of Sprint 4's GenAI capstone; details in
[`decisions.md` ADR-14](decisions.md#adr-14--sql-chat-text-to-sql-is-guarded-by-defence-in-depth-not-by-the-model).

### On-time

A departure whose realtime delay is **under 120 seconds**. Two minutes is the
threshold the project brief specifies and matches SNCB's own published
definition.

Cancellations are counted separately, never folded in as "on time" — deleting a
late train is not the same as running it punctually.

---

## Part 5 — The three ideas that shape everything

If you remember nothing else from this glossary, remember these. Each one is a
case where the obvious approach gives a confidently wrong answer.

### 1. Rows in a timetable are not trains that run

`SELECT departure_hour, COUNT(*) ... GROUP BY departure_hour` is the natural way
to find the busiest hour, and on this data it answers **10:00**. The real answer
is **17:00**. The query is syntactically perfect; it just answers a different
question than the one asked, because a year-long feed weights every service
equally regardless of how often it runs.

→ [Annualised departures](#annualised-departures)

### 2. A field that exists is not a field that is filled

`calendar.txt` has weekday columns; they are all zero. `trips.txt` has
`wheelchair_accessible`; it is empty for every one of 134,809 trips. Both
queries you would naturally write against them return an answer, confidently,
and both answers are meaningless.

The habit this teaches: before trusting a column, count its distinct values.

→ [Service and service calendar](#service-and-service-calendar),
[`bikes_allowed` and `wheelchair_accessible`](#bikes_allowed-and-wheelchair_accessible)

### 3. Silence is not "no"

GTFS code `0` means *the operator did not say*. Reporting it as "not
wheelchair accessible" would publish a claim about SNCB's fleet that the data
does not support. The honest finding — that the field is entirely unpopulated
and therefore **no journey planner using this feed can answer a wheelchair
question at all** — is both more useful and more actionable than a false
percentage.

→ [`bikes_allowed` and `wheelchair_accessible`](#bikes_allowed-and-wheelchair_accessible)

---

## Where to go next

| If you want… | Read |
|---|---|
| What the project found | [`analysis_report.md`](analysis_report.md) |
| What each table and column means | [`data_dictionary.md`](data_dictionary.md) |
| The diagram | [`erd.md`](erd.md) |
| What was cleaned and why | [`data_quality.md`](data_quality.md) |
| Why the design is the way it is | [`decisions.md`](decisions.md) |
| The deeper database theory | [`SQL&DB_theory.md`](../SQL&DB_theory.md) |
| The data source and its rules | [`api_and_compliance.md`](api_and_compliance.md) |
