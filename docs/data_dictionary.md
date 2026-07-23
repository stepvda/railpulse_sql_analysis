# RailPulse — Data Dictionary

Every table and every view in `data/railpulse.db`, with its purpose, its grain,
its row count and its columns.

> ⓘ **Unfamiliar with a term used here?** [`glossary.md`](glossary.md) defines every GTFS, database and project-specific word this project uses, with examples from this data.

**Source feed.** SNCB/NMBS GTFS Static, Belgian Mobility Open Data portal,
`https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/static`.
Licence CC BY 4.0, attribution "NMBS-SNCB - Open Data - 2026-07-20".

**Feed identity as loaded.** `feed_id` `nmbssncb`, `feed_version` `2026-07-20`,
`feed_lang` `fr`, validity window **2025-12-20 → 2026-12-12** (358 distinct
operating dates). Station and headsign names are therefore **French**; nl/de/en
live in `text_translation`.

**Build provenance.** `ingestion_run` records one row per load (a rebuild adds a
new row and keeps the old ones — it is an audit log, not a status flag). Each
records the start/finish time, the number of rows staged, loaded and rejected,
and the status. The live figures for the current database are printed by
`railpulse info`; they are not transcribed here because they change with every
build and the point of the audit table is that the database describes itself.
`PRAGMA foreign_key_check` returns clean after a normal build. Note that
`rows_loaded` counts only `stop_time` + `trip` + `service_date`, so it is not
"staged minus rejected" — see [`ingestion_run`](#ingestion_run).

**Size on disk.** Roughly **1 GB** after a normal build, which drops the staging
layer and `VACUUM`s. Two things make an exact byte count pointless to quote: a
`--keep-staging` build is ~50% larger (staging is a second, untyped copy of the
whole feed), and the real-time tables are append-only and grow with every poll.
Run `railpulse info` for the current size.

---

## 📐 Four conventions the schema follows

Read these once and every column table below becomes shorter.

**1. Dates are ISO-8601 `TEXT`, never GTFS `YYYYMMDD`.**
GTFS ships `20251220`; SQLite's `date()`, `julianday()` and `strftime()` do not
understand that form, and lexical ordering of the two forms happens to agree
only by luck. Every date column is rewritten to `'YYYY-MM-DD'` at transform time
and guarded by a `CHECK (... LIKE '____-__-__')`. The feed publishes its own
validity window with a **leading space** (`' 20251220'`), so the transform
`TRIM`s before slicing (rule DQ-06).

**2. Clock times are kept raw *and* given integer companions.**
GTFS clock times are service-relative and legitimately exceed 24:00:00 — 31 142
calls in `stop_time` do, the latest at `47:09:00`. Discarding the raw text would
lose traceability; keeping only the raw text would make every time question a
string-parsing exercise. So `stop_time` stores all three shapes:

| Shape | Example | Answers |
|---|---|---|
| `departure_time` TEXT | `'24:20:00'` | "what does the feed actually say?" |
| `departure_secs` INTEGER | `87600` | ordering, durations, headways |
| `departure_hour` INTEGER | `0` | "which hour does the passenger read?" |

plus `day_offset` (0 same day, 1 after midnight). Computing the hour per query
— `GROUP BY strftime(...)` — over 2.2 M rows is both slower and a SARGability
violation, so it is materialised once at load time.

**3. Every code column carries a `FOREIGN KEY` into a `ref_` table.**
GTFS encodes meaning as bare integers. Storing `1` in `bikes_allowed` and hoping
the reader remembers what it means is how analyses go wrong. Each `ref_` table
turns a magic number into a joinable label *and*, because the fact columns
reference it, into a validity constraint: an unexpected code from a future feed
fails loudly at load time instead of silently skewing an average. SQLite only
enforces this when `PRAGMA foreign_keys = ON`, which `src/railpulse/db.py` sets
on every connection.

**4. Empty string is normalised to `NULL`.**
A GTFS CSV has no way to say "absent" other than an empty field, so the raw feed
carries both `''` and genuinely missing columns. Carrying both spellings into
the core model would mean every predicate needs `IS NULL OR = ''`. The transform
applies `NULLIF(TRIM(x), '')` throughout (rule DQ-08), so in the core model
**missing is always `NULL` and never `''`**. The one deliberate exception is the
accessibility family, where an empty field means the specific GTFS value
`0 = "no information"` rather than "unknown to us" (rule DQ-02).

### Reading the column tables

- **Null** — `no` means the column is declared `NOT NULL` or is part of the
  primary key; `yes` means the column may be absent.
- **Key** — `PK` primary key, `FK` foreign key, `UQ` part of a unique
  constraint.
- **GTFS source** — the file and field the value comes from, or `—` for a
  column RailPulse derives.
- A quirk worth knowing: SQLite's legacy behaviour permits `NULL` in a
  `TEXT PRIMARY KEY` of a rowid table unless `NOT NULL` is also declared. The
  tables below mark PK columns `no` because that is the model's intent and the
  data honours it; the physical `notnull` flag on those specific columns is 0.

---

## Reference tables

Nine hand-seeded code lists. The six the core model points at
(`ref_location_type` … `ref_transfer_type`) are seeded by `02_schema.sql`,
before the transform runs, because the FKs in `03_transform.sql` resolve against
them. The three GTFS-Realtime lists (`ref_schedule_relationship`,
`ref_alert_cause`, `ref_alert_effect`) are seeded later, by `06_realtime.sql`,
with `INSERT OR IGNORE` so a rebuild does not disturb them. All are tiny, so the
actual seeded rows are printed in full — a reader should never have to guess
what code 2 means.

### `ref_location_type`

**Purpose.** The GTFS `stops.txt` discriminator that tells a station apart from
a platform. RailPulse splits that single file into two tables on this code, so
the table documents a decision rather than constraining a column.
**Grain.** One row per GTFS location type.
**Rows.** 5.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `location_type` | INTEGER | no | PK | `stops.location_type` | The GTFS code |
| `label` | TEXT | no | | — | Short human name |
| `description` | TEXT | no | | — | One-line explanation |

| `location_type` | `label` | `description` |
|---|---|---|
| 0 | Stop/Platform | A boarding point where passengers board or alight |
| 1 | Station | A physical structure containing one or more platforms |
| 2 | Entrance/Exit | A location where passengers enter or leave a station |
| 3 | Generic Node | A location within a station used to link pathways |
| 4 | Boarding Area | A specific location on a platform |

Only codes 0 (2 243 rows) and 1 (652 rows) occur in this feed.

### `ref_route_type`

**Purpose.** Mode of transport for a route. Referenced by `route.route_type`.
**Grain.** One row per GTFS route type.
**Rows.** 10.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `route_type` | INTEGER | no | PK | `routes.route_type` | The GTFS code |
| `label` | TEXT | no | | — | Mode name |
| `description` | TEXT | no | | — | One-line explanation |

| `route_type` | `label` | `description` |
|---|---|---|
| 0 | Tram | Tram, streetcar or light rail within a city |
| 1 | Metro | Underground rail system within a city |
| 2 | Rail | Intercity or long-distance rail — the SNCB core network |
| 3 | Bus | Short- and long-distance bus, incl. rail replacement |
| 4 | Ferry | Boat service |
| 5 | Cable Tram | Street-level rail with a cable running beneath |
| 6 | Aerial Lift | Cable car, gondola or aerial tramway |
| 7 | Funicular | Rail system designed for steep inclines |
| 11 | Trolleybus | Electric bus drawing power from overhead wires |
| 12 | Monorail | Railway in which the track consists of a single rail |

Only 2 (1 531 routes) and 3 (270 routes) occur. The mode split is exactly the
line along which Q5's bicycle-storage finding falls.

### `ref_pickup_drop`

**Purpose.** Whether a call is a commercial boarding opportunity. Referenced
twice by `stop_time` — once for pick-up, once for drop-off.
**Grain.** One row per GTFS pickup/drop-off code.
**Rows.** 4.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `code` | INTEGER | no | PK | `stop_times.pickup_type` / `.drop_off_type` | The GTFS code |
| `label` | TEXT | no | | — | Short human name |
| `description` | TEXT | no | | — | One-line explanation |

| `code` | `label` | `description` |
|---|---|---|
| 0 | Regularly scheduled | Normal commercial pick-up / drop-off |
| 1 | Not available | No boarding/alighting — a technical pass-through |
| 2 | Phone agency | Must phone the agency to arrange |
| 3 | Coordinate with driver | Must coordinate with the driver |

Only 0 and 1 occur. Code 1 is the reason `v_departure` exists.

### `ref_accessibility`

**Purpose.** The single 0/1/2 vocabulary GTFS reuses for `bikes_allowed`,
`wheelchair_accessible` and `wheelchair_boarding`.
**Grain.** One row per accessibility code.
**Rows.** 3.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `code` | INTEGER | no | PK | `trips.bikes_allowed` / `trips.wheelchair_accessible` / `stops.wheelchair_boarding` | The GTFS code |
| `label` | TEXT | no | | — | Short human name |
| `description` | TEXT | no | | — | One-line explanation |
| `is_guaranteed` | INTEGER | no | | — | 1 only for an explicit positive guarantee; the flag Q5 aggregates on |

| `code` | `label` | `description` | `is_guaranteed` |
|---|---|---|---|
| 0 | No information | The feed makes no statement — this is NOT a "no" | 0 |
| 1 | Yes | Explicitly accommodated / guaranteed | 1 |
| 2 | No | Explicitly not accommodated | 0 |

Code 0 meaning "no information" and not "no" is rule DQ-02, and it is the single
most common error in accessibility reporting. `is_guaranteed` exists so that
`= 1` is written once here rather than in five different queries.

### `ref_exception_type`

**Purpose.** Whether a `calendar_dates.txt` row adds or removes a service day.
**Grain.** One row per GTFS exception type.
**Rows.** 2.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `exception_type` | INTEGER | no | PK | `calendar_dates.exception_type` | The GTFS code |
| `label` | TEXT | no | | — | Short human name |
| `description` | TEXT | no | | — | One-line explanation |

| `exception_type` | `label` | `description` |
|---|---|---|
| 1 | Added | Service has been ADDED for the specified date |
| 2 | Removed | Service has been REMOVED for the specified date |

All 4 697 139 rows of `service_date` are type 1. Every query that walks the
calendar still filters `exception_type = 1` explicitly, because the day a
`Removed` row appears the unfiltered query would silently start over-counting.

### `ref_transfer_type`

**Purpose.** The kind of connection guarantee a `transfers.txt` row expresses.
**Grain.** One row per GTFS transfer type.
**Rows.** 4.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `transfer_type` | INTEGER | no | PK | `transfers.transfer_type` | The GTFS code |
| `label` | TEXT | no | | — | Short human name |
| `description` | TEXT | no | | — | One-line explanation |

| `transfer_type` | `label` | `description` |
|---|---|---|
| 0 | Recommended | Recommended transfer point |
| 1 | Timed | Departing vehicle waits for the arriving one |
| 2 | Minimum time required | A minimum transfer time is needed |
| 3 | Not possible | Transfer is not possible here |

All 733 rows of `transfer` are type 2.

### `ref_schedule_relationship`

**Purpose.** GTFS-Realtime enumeration describing how a live trip or call
relates to the published timetable. Referenced by `rt_trip_update` and
`rt_stop_time_update`.
**Grain.** One row per GTFS-RT schedule relationship code.
**Rows.** 7.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `code` | INTEGER | no | PK | GTFS-RT `scheduleRelationship` | The GTFS-RT code |
| `label` | TEXT | no | | — | Spec name |
| `description` | TEXT | no | | — | One-line explanation |

| `code` | `label` | `description` |
|---|---|---|
| 0 | SCHEDULED | Running in accordance with its GTFS schedule |
| 1 | ADDED | An extra trip, not in the static feed |
| 2 | UNSCHEDULED | Running with no schedule (trip level) / SKIPPED (stop level) |
| 3 | CANCELED | Previously scheduled, now cancelled |
| 5 | REPLACEMENT | Replaces a previously scheduled trip |
| 6 | DUPLICATED | A duplicate of an existing trip |
| 7 | DELETED | Should not be shown to passengers at all |

Code 4 does not exist in the spec, hence the gap. Note that code 2 means
different things at trip level and at stop level; at stop level it is the
cancellation flag, and `v_rt_departure_performance` treats it as such.

### `ref_alert_cause`

**Purpose.** Why a service alert was issued.
**Grain.** One row per GTFS-RT alert cause.
**Rows.** 12.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `code` | INTEGER | no | PK | GTFS-RT `alert.cause` | The GTFS-RT code |
| `label` | TEXT | no | | — | Spec name |

| `code` | `label` | `code` | `label` |
|---|---|---|---|
| 1 | UNKNOWN_CAUSE | 7 | HOLIDAY |
| 2 | OTHER_CAUSE | 8 | WEATHER |
| 3 | TECHNICAL_PROBLEM | 9 | MAINTENANCE |
| 4 | STRIKE | 10 | CONSTRUCTION |
| 5 | DEMONSTRATION | 11 | POLICE_ACTIVITY |
| 6 | ACCIDENT | 12 | MEDICAL_EMERGENCY |

### `ref_alert_effect`

**Purpose.** What a service alert does to the service.
**Grain.** One row per GTFS-RT alert effect.
**Rows.** 11.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `code` | INTEGER | no | PK | GTFS-RT `alert.effect` | The GTFS-RT code |
| `label` | TEXT | no | | — | Spec name |

| `code` | `label` | `code` | `label` |
|---|---|---|---|
| 1 | NO_SERVICE | 7 | OTHER_EFFECT |
| 2 | REDUCED_SERVICE | 8 | UNKNOWN_EFFECT |
| 3 | SIGNIFICANT_DELAYS | 9 | STOP_MOVED |
| 4 | DETOUR | 10 | NO_EFFECT |
| 5 | ADDITIONAL_SERVICE | 11 | ACCESSIBILITY_ISSUE |
| 6 | MODIFIED_SERVICE | | |

---

## Core model

### `feed_info`

**Purpose.** Provenance for the timetable. Every number in every report is
bounded by this validity window, so a figure is never quoted undated.
**Grain.** One row per GTFS feed loaded.
**Rows.** 1.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `feed_id` | TEXT | no | PK | `feed_info.feed_id` | Publisher's feed identifier (`nmbssncb`) |
| `feed_publisher_name` | TEXT | no | | `feed_info.feed_publisher_name` | Publisher (`nmbssncb`) |
| `feed_publisher_url` | TEXT | yes | | `feed_info.feed_publisher_url` | `http://www.belgiantrain.be/` |
| `feed_lang` | TEXT | yes | | `feed_info.feed_lang` | Language of the primary strings (`fr`) |
| `feed_start_date` | TEXT | yes | | `feed_info.feed_start_date` | First day covered, ISO (`2025-12-20`) |
| `feed_end_date` | TEXT | yes | | `feed_info.feed_end_date` | Last day covered, ISO (`2026-12-12`) |
| `feed_version` | TEXT | yes | | `feed_info.feed_version` | Publisher's version string (`2026-07-20`) |

`feed_contact_email` and `default_lang` are empty in the source and are not
carried. `feed_contact_url` *is* populated (`http://www.belgiantrain.be/`) but
duplicates `feed_publisher_url` exactly, so it is dropped as redundant rather
than as absent. Both date columns arrive with a leading space and are `TRIM`med
(DQ-06); `CHECK` constraints enforce the ISO shape.

### `agency`

**Purpose.** The transport operator. One row today; the column is still a real
foreign key so the model extends to De Lijn, TEC or STIB without a change.
**Grain.** One row per operator.
**Rows.** 1.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `agency_id` | TEXT | no | PK | `agency.agency_id` | `nmbssncb` |
| `agency_name` | TEXT | no | | `agency.agency_name` | `NMBS/SNCB` |
| `agency_url` | TEXT | yes | | `agency.agency_url` | `http://www.belgiantrain.be/` |
| `agency_timezone` | TEXT | no | | `agency.agency_timezone` | `Europe/Brussels` — the timezone all GTFS clock times are read in |
| `agency_lang` | TEXT | yes | | `agency.agency_lang` | `fr` |
| `agency_phone` | TEXT | yes | | `agency.agency_phone` | NULL — empty in the feed |

### `station`

**Purpose.** A named rail hub — the level a passenger means by
"Bruxelles-Central". GTFS ships stations and platforms interleaved in one
`stops.txt` with a `location_type` discriminator; that is two grains in one
file, which is exactly what normalisation forbids, so RailPulse splits them.
**Grain.** One row per GTFS stop with `location_type = 1`.
**Rows.** 652.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `station_id` | TEXT | no | PK | `stops.stop_id` | e.g. `gs:nmbssncb:S8813003` |
| `station_name` | TEXT | no | | `stops.stop_name` | French name; unique across all 652 rows in this feed |
| `latitude` | REAL | yes | | `stops.stop_lat` | WGS84; populated for all 652, range 47.8132 to 53.4563 |
| `longitude` | REAL | yes | | `stops.stop_lon` | WGS84; populated for all 652, range 2.3546 to 16.379 |
| `wheelchair_boarding` | INTEGER | no | FK → `ref_accessibility(code)` | `stops.wheelchair_boarding` | 0 = "no information" for all 652 rows — the field is empty throughout the feed |

The coordinate range extends well beyond Belgium because the feed includes the
international destinations SNCB trains reach: the extremes are Wien HBF (AT) in
the east, Paris Nord (FR) in the west, Hamburg-Harburg in the north and Salzburg
Hbf (AT) in the south. `station_name` carries no `UNIQUE` constraint even though
it happens to be unique here, because GTFS does not guarantee it in general;
`ix_station_name` is a plain index.

### `platform`

**Purpose.** A boarding point inside a station, and the level Q2's
"busiest platform" question is asked at.
**Grain.** One row per GTFS stop with `location_type = 0`.
**Rows.** 2 243.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `stop_id` | TEXT | no | PK | `stops.stop_id` | e.g. `gs:nmbssncb:8813003_4` |
| `station_id` | TEXT | no | FK → `station(station_id)`, UQ | `stops.parent_station` | Owning station |
| `platform_code` | TEXT | yes | UQ | `stops.platform_code` | Track number as printed on the sign; NULL on exactly 652 rows |
| `latitude` | REAL | yes | | `stops.stop_lat` | Populated on all 2 243 rows |
| `longitude` | REAL | yes | | `stops.stop_lon` | Populated on all 2 243 rows |
| `stop_desc` | TEXT | yes | | `stops.stop_desc` | Facility type; populated on all 2 243 rows |
| `has_platform_code` | INTEGER | no | | — | 1 when `platform_code` is a real track number; 1 591 rows |

`stop_name` is **deliberately absent**. In this feed a child stop's `stop_name`
is always identical to its parent station's (0 of 2 243 differ), so keeping it
here would be a pure transitive dependency and a 3NF violation. Queries join
through to `station`.

`has_platform_code` is a 0/1 companion to `platform_code`. Note that
`platform_code IS NOT NULL` is itself SARGable (a range seek), so this is not a
SARGability fix; the flag's value is that a small-cardinality integer composes
cleanly into the front of the composite index
`ix_platform_station (station_id, has_platform_code, platform_code)`, which is seeked
rather than scanned.

Every one of the 652 stations owns exactly one `platform_code IS NULL` child.
The feed uses it for calls where no track has been allocated, so those 652 rows
are a real modelling feature, not missing data.

`stop_desc` takes four values: `NMBSSNCB RAIL PLATFORM` (1 708),
`NMBSSNCB RAIL+BUS PLATFORM` (530), `NMBSSNCB  PLATFORM` (3, with the double
space as published) and `NMBSSNCB BUS PLATFORM` (2).

Platform-code frequency, top of the distribution: NULL 652, `1` 536, `2` 498,
`3` 163, `4` 116, `5` 65, `6` 36, `7` 23.

### `route`

**Purpose.** A commercial line — what a passenger calls "the IC to Ostend".
**Grain.** One row per GTFS route.
**Rows.** 1 801 (1 531 rail, 270 rail-replacement bus).

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `route_id` | TEXT | no | PK | `routes.route_id` | e.g. `gr:nmbssncb:973` |
| `agency_id` | TEXT | no | FK → `agency(agency_id)` | `routes.agency_id` | Operator |
| `route_short_name` | TEXT | yes | | `routes.route_short_name` | Service brand; populated on all 1 801 rows |
| `route_long_name` | TEXT | yes | | `routes.route_long_name` | e.g. `Hal -- Malines`; populated on all 1 801 rows |
| `route_type` | INTEGER | no | FK → `ref_route_type(route_type)` | `routes.route_type` | 2 = Rail (1 531), 3 = Bus (270) |
| `route_color` | TEXT | yes | | `routes.route_color` | Six-digit hex without `#`; populated throughout, `016AB3` on 1 538 routes |
| `route_text_color` | TEXT | yes | | `routes.route_text_color` | Six-digit hex; `FFFFFF` throughout |

`route_desc` and `route_url` are empty on all 1 801 source rows and
`route_sort_order` is absent from the file entirely, so none is carried (DQ-08).

Short-name frequency, top of the distribution: `IC` 579, `L` 333, `BUS` 270,
`P` 166, `TRN` 71, `EXT` 54, `T` 32, then the `S`-series suburban lines
(`S61` 27, `S1` 25, `S32` 22).

### `service`

**Purpose.** A calendar pattern that trips attach to.
**Grain.** One row per GTFS `service_id`.
**Rows.** 51 593.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `service_id` | TEXT | no | PK | `calendar.service_id` | e.g. `gc:nmbssncb:000000` |
| `start_date` | TEXT | no | | `calendar.start_date` | First day of the pattern, ISO |
| `end_date` | TEXT | no | | `calendar.end_date` | Last day of the pattern, ISO; `CHECK (end_date >= start_date)` |
| `monday` | INTEGER | no | | `calendar.monday` | 0/1 weekday flag — **0 for all 51 593 rows** |
| `tuesday` | INTEGER | no | | `calendar.tuesday` | as above |
| `wednesday` | INTEGER | no | | `calendar.wednesday` | as above |
| `thursday` | INTEGER | no | | `calendar.thursday` | as above |
| `friday` | INTEGER | no | | `calendar.friday` | as above |
| `saturday` | INTEGER | no | | `calendar.saturday` | as above |
| `sunday` | INTEGER | no | | `calendar.sunday` | as above |
| `has_weekday_pattern` | INTEGER | no | | — | 1 when at least one weekday flag is set. **0 for all 51 593 rows.** |

This is rule **DQ-01**, the most consequential data-quality finding in the feed.
`calendar.txt` should answer "which weekdays does this service run?"; here it
answers nothing, and the real calendar lives entirely in `calendar_dates.txt`.
The columns are retained because they are part of the GTFS contract and a future
feed may populate them, but `has_weekday_pattern` records per row whether they
can be trusted, and Q4 derives the weekly rhythm from `service_date` instead —
see `v_service_frequency`.

`start_date` takes 329 distinct values spanning 2025-12-20 to 2026-12-05;
`end_date` spans 2025-12-24 to 2026-12-12. The union is the feed window.

### `service_date`

**Purpose.** The exploded operating calendar — the table that actually says when
anything runs.
**Grain.** One row per (service, calendar day).
**Rows.** 4 697 139.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `service_id` | TEXT | no | PK, FK → `service(service_id)` | `calendar_dates.service_id` | Which pattern |
| `service_date` | TEXT | no | PK | `calendar_dates.date` | The calendar day, ISO |
| `exception_type` | INTEGER | no | FK → `ref_exception_type(exception_type)` | `calendar_dates.exception_type` | 1 = Added for all 4 697 139 rows |
| `day_of_week` | INTEGER | no | | — | 0 = Sunday … 6 = Saturday, SQLite's `strftime('%w')` convention |

Declared `WITHOUT ROWID`: the primary key *is* the row, which saves roughly
150 MB and one B-tree hop per lookup on a table this size.

`day_of_week` is materialised at load time rather than computed per query. Q4
groups all 4.7 M rows by weekday; calling `strftime('%w', …)` there would cost a
function call per row and make any index on the column unusable.

358 distinct dates, 2025-12-20 to 2026-12-12.

### `trip`

**Purpose.** One vehicle journey: a route, running on a service calendar,
towards a headsign destination.
**Grain.** One row per GTFS trip.
**Rows.** 134 809.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `trip_id` | TEXT | no | PK | `trips.trip_id` | e.g. `gt:nmbssncb:88____:075::8822004:8814308:18:2224:20261206:1` |
| `route_id` | TEXT | no | FK → `route(route_id)` | `trips.route_id` | Commercial line |
| `service_id` | TEXT | no | FK → `service(service_id)` | `trips.service_id` | Which days it runs |
| `trip_headsign` | TEXT | yes | | `trips.trip_headsign` | Terminal destination shown to passengers; populated on all 134 809 rows, 222 distinct values |
| `trip_short_name` | TEXT | yes | | `trips.trip_short_name` | Public train number, e.g. `13939`; populated on all rows |
| `block_id` | TEXT | yes | | `trips.block_id` | Vehicle-rotation grouping; populated on all rows |
| `direction_id` | INTEGER | yes | | `trips.direction_id` | **NULL for all 134 809 rows** — empty throughout the feed |
| `bikes_allowed` | INTEGER | no | FK → `ref_accessibility(code)` | `trips.bikes_allowed` | 1 = Yes on 123 051 trips; 0 = "no information" on 11 758 |
| `wheelchair_accessible` | INTEGER | no | FK → `ref_accessibility(code)` | `trips.wheelchair_accessible` | **0 = "no information" for all 134 809 rows** |

Two columns carry no signal in this feed and both matter. `direction_id` being
entirely NULL means outbound/inbound cannot be separated without inferring it
from stop sequences. `wheelchair_accessible` being entirely 0 is the headline
finding of Q5: the feed makes no wheelchair statement anywhere, which is not the
same as the network being inaccessible, and reporting it as "not accessible"
would invent a fact.

The `bikes_allowed` split is exactly by mode — all 123 051 rail trips are 1, all
11 758 bus trips are 0 — which is why Q5's lowest-scoring routes are every
rail-replacement bus route.

`shape_id` is empty on all source rows and is not carried.

### `stop_time`

**Purpose.** The fact table. One scheduled call of one trip at one platform;
everything else in the model is a dimension hanging off this.
**Grain.** One row per (trip, stop sequence).
**Rows.** 2 165 507 — 2 165 519 staged, 12 quarantined by DQ-03.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `trip_id` | TEXT | no | PK, FK → `trip(trip_id)` | `stop_times.trip_id` | Which journey |
| `stop_sequence` | INTEGER | no | PK | `stop_times.stop_sequence` | Position along the journey, 1-based |
| `stop_id` | TEXT | no | FK → `platform(stop_id)` | `stop_times.stop_id` | Which boarding point |
| `arrival_time` | TEXT | yes | | `stop_times.arrival_time` | Raw GTFS `'HH:MM:SS'`, may exceed 24:00:00; populated on all rows |
| `departure_time` | TEXT | yes | | `stop_times.departure_time` | As above; populated on all rows |
| `arrival_secs` | INTEGER | yes | | — | Seconds since the service day began; `CHECK (>= 0)` |
| `departure_secs` | INTEGER | yes | | — | As above; maximum in this feed 169 740 (`47:09:00`) |
| `departure_hour` | INTEGER | yes | | — | `(departure_secs / 3600) % 24` — the clock hour on the platform; `CHECK` 0-23 |
| `arrival_hour` | INTEGER | yes | | — | As above, for arrival |
| `day_offset` | INTEGER | no | | — | Calendar days after the service date: 0 on 2 134 365 rows, 1 on 31 142 |
| `pickup_type` | INTEGER | no | FK → `ref_pickup_drop(code)` | `stop_times.pickup_type` | May a passenger board? |
| `drop_off_type` | INTEGER | no | FK → `ref_pickup_drop(code)` | `stop_times.drop_off_type` | May a passenger alight? |
| `stop_headsign` | TEXT | yes | | `stop_times.stop_headsign` | **NULL for all 2 165 507 rows** — empty throughout the feed |
| `is_boardable` | INTEGER | no | | — | 1 when `pickup_type <> 1`; 1 453 221 rows |
| `is_alightable` | INTEGER | no | | — | 1 when `drop_off_type <> 1`; 1 453 278 rows |

The three derived time columns exist because three different questions need
three different shapes of the same value — see convention 2. Q1 must use
`departure_hour`: the raw text would scatter after-midnight departures into
fictional hours 24 and 25.

Boarding permissions, cross-tabulated:

| `pickup_type` | `drop_off_type` | Rows | What it is |
|---|---|---|---|
| 0 | 0 | 1 318 454 | Ordinary commercial call |
| 0 | 1 | 134 767 | Boarding only — typically the origin |
| 1 | 0 | 134 824 | Alighting only — typically the terminus |
| 1 | 1 | 577 462 | Technical pass-through: the train serves the platform, nobody may use it |

Counting all 2.17 M rows as "departures" would overstate every hub in the report.
`v_departure` filters on `is_boardable = 1`, which removes 712 286 rows in total
(the 577 462 full pass-throughs plus the 134 824 terminus arrivals).

The 12 quarantined rows are calls 48 hours or more into their own service day —
`63:18:00` at the lowest and `87:39:00` at the highest, in three clusters around
63 h, 65 h and 87 h. GTFS permits times past 24:00:00 for trips
crossing midnight, but a rail call two days into its service day is a data
error, not a timetable. They are in `rejected_row`, not deleted.

`shape_dist_traveled` is empty on all source rows and `timepoint` is absent from
the file; neither is carried.

### `transfer`

**Purpose.** Minimum connection times, so an itinerary planner does not promise
a change that cannot physically be made.
**Grain.** One row per (from-platform, to-platform, from-trip, to-trip)
connection rule.
**Rows.** 733.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `transfer_id` | INTEGER | no | PK | — | Surrogate key, autoincrement |
| `from_stop_id` | TEXT | no | FK → `platform(stop_id)`, UQ | `transfers.from_stop_id` | Arriving platform |
| `to_stop_id` | TEXT | no | FK → `platform(stop_id)`, UQ | `transfers.to_stop_id` | Departing platform |
| `transfer_type` | INTEGER | no | FK → `ref_transfer_type(transfer_type)` | `transfers.transfer_type` | 2 = Minimum time required, on all 733 rows |
| `min_transfer_time` | INTEGER | yes | | `transfers.min_transfer_time` | Seconds; populated on all 733 rows, range 0 to 420 |
| `from_trip_id` | TEXT | yes | FK → `trip(trip_id)`, UQ | `transfers.from_trip_id` | Set on 74 rows, NULL on 659 |
| `to_trip_id` | TEXT | yes | FK → `trip(trip_id)`, UQ | `transfers.to_trip_id` | Set on 74 rows, NULL on 659 |

The surrogate primary key is used because the GTFS natural key
`(from_stop_id, to_stop_id)` is not unique once trip-scoped transfers exist; the
`UNIQUE` constraint spans all four key columns instead.

The 733 rows split 659 / 74 on whether a trip is named.

The 659 with no trip are station-level rules, and 651 of those are
self-transfers where `from_stop_id = to_stop_id` — always at the station's
`platform_code IS NULL` child, one per station. They cover 651 of the 652
stations; Gand-Saint-Pierre (`gs:nmbssncb:S8892007`) is the sole station without
one. Their `min_transfer_time` is 300 s on 617, 240 s on 15, 0 s on 11, 120 s on
6, 180 s on 1 and 60 s on 1.

The other 8 no-trip rows are not self-transfers: they are four cross-station
pairs, each stored in both directions — Arcades/Watermael (300 s),
Hergenrath/Hergenrath-Frontière (240 s), Jambes/Jambes-Est (420 s) and
Athus-Frontière/Aubange-Frontière-Luxembourg (420 s). Each is a walking
connection between two stations close enough to be treated as one interchange,
which is why a query that assumes every transfer is intra-station will
mis-attribute these eight.

The remaining 74 rows name a specific arriving and departing trip; all 74 are
within a single station, 8 of them between the same platform and itself.

`from_route_id` and `to_route_id` are absent from the source file.

### `text_translation`

**Purpose.** Dutch, German and English labels for a feed published in French.
Belgium is trilingual and the dashboard language switch reads this table.
**Grain.** One row per (table, field, French source value, language).
**Rows.** 2 599.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `table_name` | TEXT | no | PK | `translations.table_name` | `stops` or `trips` |
| `field_name` | TEXT | no | PK | `translations.field_name` | `stop_name` or `trip_headsign` |
| `field_value` | TEXT | no | PK | `translations.field_value` | The French source string — the join key |
| `language` | TEXT | no | PK | `translations.language` | `nl`, `de`, `en` (`fr` permitted, none present) |
| `translation` | TEXT | no | | `translations.translation` | The translated string |

Declared `WITHOUT ROWID`. The composite PK leads on `table_name`/`field_name`/
`field_value`, so `ix_translation_lang (language, table_name, field_name)` exists
for the language-first lookups the dashboard makes.

The key is the French **value**, not a row id, because `record_id` is empty on
all 2 599 source rows (rule **DQ-07**). That is GTFS-permitted but lossy: two
distinct records that happen to share a French string cannot be translated
differently. Coverage:

| `table_name` | `field_name` | `nl` | `de` | `en` |
|---|---|---|---|---|
| `stops` | `stop_name` | 652 | 642 | 642 |
| `trips` | `trip_headsign` | 221 | 221 | 221 |

Of the 652 stations, 651 have a Dutch name translation and 641 a German/English one (the row counts of 652/642/642 include a handful of entries that no longer match a current station — see below). So coverage is near-total but not literally complete. Three of the
2 599 rows — the `nl`, `de` and `en` translations of a single `stop_name` value —
match no current `stop_name` or `trip_headsign`. They are stale entries, kept
because dropping them would lose information the publisher intended to ship.

---

## Operational tables

Both survive a rebuild. They are the build's own audit log, and wiping them on
every load would destroy the record of what previous loads did.

### `ingestion_run`

**Purpose.** What was fetched, from where, how big it was and how long it took.
Makes a reload reproducible and lets any report print "data as of …".
**Grain.** One row per execution of an ingestion job.
**Rows.** 1.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `run_id` | INTEGER | no | PK | — | Surrogate key, autoincrement |
| `started_at_utc` | TEXT | no | | — | ISO-8601 UTC timestamp |
| `finished_at_utc` | TEXT | yes | | — | NULL while a run is in flight |
| `source` | TEXT | no | | — | `gtfs-static`, `gtfs-rt-trip-update`, `gtfs-rt-alert` |
| `source_url` | TEXT | yes | | — | Endpoint called |
| `http_status` | INTEGER | yes | | — | Response status; NULL when served from the local cache |
| `bytes_downloaded` | INTEGER | yes | | — | Payload size |
| `source_last_modified` | TEXT | yes | | — | Upstream `Last-Modified` header, for change detection |
| `rows_staged` | INTEGER | yes | | — | Rows landed in `stg_*`, summed over all ten tables |
| `rows_loaded` | INTEGER | yes | | — | `COUNT(stop_time) + COUNT(trip) + COUNT(service_date)` only — not every core row |
| `rows_rejected` | INTEGER | yes | | — | Rows quarantined; counted as `rejected_row WHERE source_table LIKE 'stg%'` |
| `status` | TEXT | no | | — | `running`, `ok` or `failed`; `CHECK`-constrained |
| `notes` | TEXT | yes | | — | Free text |

The one row: `gtfs-static`, 2026-07-23T08:04:59Z → 08:06:34Z, 7 057 090 staged,
6 997 455 loaded, 12 rejected, `ok`, notes `offline rebuild`. `http_status` and
`bytes_downloaded` are NULL because that run read the cached ZIP in `data/raw/`
rather than calling the portal.

### `rejected_row`

**Purpose.** The quarantine. A row that cannot satisfy the core model's
constraints is never silently dropped — it lands here with the file, the line
number, the rule that rejected it and the original payload as JSON.
**Grain.** One row per rejected source row (or per duplicated key group, for the
DQ-05 summary rules).
**Rows.** 12.

| Column | Type | Null | Key | GTFS source | Meaning |
|---|---|---|---|---|---|
| `rejected_id` | INTEGER | no | PK | — | Surrogate key, autoincrement |
| `run_id` | INTEGER | yes | FK → `ingestion_run(run_id)` | — | Which run rejected it; stamped by `build.py` after the transform (populated on all rows) |
| `source_table` | TEXT | no | | — | Staging table the row came from, e.g. `stg_stop_times` |
| `src_line_no` | INTEGER | yes | | — | Physical line number in the source `.txt`, for traceability |
| `rule_code` | TEXT | no | | — | e.g. `DQ-03-IMPLAUSIBLE-DEPARTURE` |
| `reason` | TEXT | no | | — | Human-readable explanation |
| `payload` | TEXT | yes | | — | JSON snapshot of the offending row |

All 12 rows are `stg_stop_times` / `DQ-03-IMPLAUSIBLE-DEPARTURE`. Rules DQ-04,
DQ-05 and DQ-09 are implemented and armed but caught nothing in this feed —
zero orphan foreign keys, zero duplicate keys, zero orphan trips. `run_id` is
NULL because the transform's quarantine inserts do not currently stamp the
active run; the rows are still traceable through `source_table` and
`src_line_no`.

`03_transform.sql` deletes only `WHERE source_table LIKE 'stg%'` before a
reload, so real-time quarantine entries — which are not reproducible — survive.

---

## Real-time tables

Created by `06_realtime.sql` with `CREATE TABLE IF NOT EXISTS` and **not**
dropped by a rebuild. A static feed can always be re-downloaded; once 06:12's
delays are gone they are gone, so these tables are additive by design.

**Six of the seven carry data.** `src/railpulse/api_client.py` fetches both
feeds, `src/railpulse/ingest_realtime.py` lands them and
`scripts/poll_realtime.sh` drives the loop. The shipped database holds one
polling window: **17 snapshots taken between 2026-07-23T08:19:12Z and
08:53:16Z**, 9 of the trip-update feed and 8 of the alert feed. Row counts below
are as at the end of that window; because these tables are append-only, every
further poll raises them.

| Table | Rows | Per snapshot |
|---|---|---|
| `rt_snapshot` | 17 | — |
| `rt_trip_update` | 1 109 | 120-126 trip entities per trip-update poll |
| `rt_stop_time_update` | 14 600 | — |
| `rt_alert` | 169 | 21-22 alerts per alert poll |
| `rt_alert_text` | 1 352 | exactly 8 per alert: header + description × `fr`/`nl`/`de`/`en` |
| `rt_alert_informed_entity` | 169 | exactly 1 per alert, agency-scoped every time |
| `rt_alert_active_period` | 0 | the feed ships an empty `activePeriod` on every alert |

Worth knowing: the portal serves both feeds as **JSON**, not protobuf, which is
why the project has no `gtfs-realtime-bindings` dependency. Field paths below
use the camelCase JSON spelling.

Three columns are `NULL` on all 1 109 `rt_trip_update` rows, and the reasons
differ. `route_id` and `vehicle_id` are simply not sent — this feed omits
`tripUpdate.trip.routeId` and `tripUpdate.vehicle` entirely.
`update_timestamp_epoch` is a loader gap: the feed serialises
`tripUpdate.timestamp` as a `{low, high, unsigned}` object rather than a bare
integer, and `ingest_realtime.py` does not unwrap it, so the value is discarded.
Nothing downstream reads the column, but it is empty by accident rather than by
design.

`rt_trip_update.trip_id` is deliberately **not** a foreign key. The static feed
is regenerated daily; the real-time feed references whatever timetable is live
right now. Between an upstream publish and the next `railpulse build`,
real-time rows legitimately name trips our static snapshot has never seen. A
hard FK would reject exactly the observations that matter most — a brand-new or
re-planned service — and cascade-delete history on rebuild.

### `rt_snapshot`

**Purpose.** The provenance anchor for every real-time observation.
**Grain.** One row per successful poll of one feed.
**Rows.** 17 — 9 trip-update, 8 alert.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK | — | Surrogate key, autoincrement |
| `feed` | TEXT | no | UQ | — | `trip-update` or `alert`; `CHECK`-constrained |
| `fetched_at_utc` | TEXT | no | | — | When *we* called |
| `feed_timestamp_epoch` | INTEGER | yes | UQ | `header.timestamp` | When *they* built the payload |
| `feed_timestamp_utc` | TEXT | yes | | `header.timestamp` | Same value, human-readable |
| `entity_count` | INTEGER | no | | `entity[]` length | Entities in the payload |
| `bytes_downloaded` | INTEGER | yes | | — | Payload size |
| `source_url` | TEXT | yes | | — | Endpoint called |

`UNIQUE (feed, feed_timestamp_epoch)` is the idempotency guard. If the poller
runs faster than the operator rebuilds the payload, or a cron run overlaps a
manual one, the identical payload comes back with the same header timestamp and
is rejected instead of double-counting every delay. It fired during the shipped
window: an alert poll returned feed timestamp 1784794733 unchanged and its
snapshot was skipped, which is why there are 9 trip-update snapshots but only
8 alert ones. The two feeds do not refresh at the same rate — two trip-update
payloads fetched 20 s apart carried header timestamps 20 s apart, while the
alert payload went unchanged across consecutive polls.

### `rt_trip_update`

**Purpose.** The trips the operator is actively reporting on right now.
**Grain.** One row per (snapshot, real-time entity).
**Rows.** 1 109 across 9 polls. `schedule_relationship` is 0 (SCHEDULED) on
every one, and all 1 109 resolve against a `trip` row in the current static
feed — no drift in this window.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK, FK → `rt_snapshot(snapshot_id)` ON DELETE CASCADE | — | Which poll |
| `rt_entity_id` | TEXT | no | PK | `entity[].id` | e.g. `rt:nmbssncb:88____:007::…` |
| `trip_id` | TEXT | yes | | `tripUpdate.trip.tripId` | Soft link to `trip(trip_id)` — see above; populated on all 1 109 rows |
| `route_id` | TEXT | yes | | `tripUpdate.trip.routeId` | Soft link to `route(route_id)`; NULL throughout — the feed does not send it |
| `start_date` | TEXT | yes | | `tripUpdate.trip.startDate` | ISO; the feed ships `YYYYMMDD` |
| `start_time` | TEXT | yes | | `tripUpdate.trip.startTime` | `'HH:MM:SS'` |
| `schedule_relationship` | INTEGER | yes | FK → `ref_schedule_relationship(code)` | `tripUpdate.trip.scheduleRelationship` | Trip-level status |
| `vehicle_id` | TEXT | yes | | `tripUpdate.vehicle.id` | NULL throughout — the feed sends no `vehicle` block |
| `update_timestamp_epoch` | INTEGER | yes | | `tripUpdate.timestamp` | NULL throughout: the feed serialises this as a `{low, high, unsigned}` object, not a bare integer, and the loader drops it |

### `rt_stop_time_update`

**Purpose.** The payload — predicted times and signed delays per call. This is
the table an on-time-performance metric is computed from.
**Grain.** One row per (snapshot, entity, stop sequence).
**Rows.** 14 600.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK, FK → `rt_trip_update` ON DELETE CASCADE | — | Which poll |
| `rt_entity_id` | TEXT | no | PK, FK → `rt_trip_update` ON DELETE CASCADE | — | Which trip update |
| `stop_sequence` | INTEGER | no | PK | `stopTimeUpdate[].stopSequence` | Position along the journey |
| `stop_id` | TEXT | yes | | `stopTimeUpdate[].stopId` | Soft link to `platform(stop_id)` |
| `arrival_epoch` | INTEGER | yes | | `stopTimeUpdate[].arrival.time` | Predicted arrival, Unix epoch |
| `arrival_delay_s` | INTEGER | yes | | `stopTimeUpdate[].arrival.delay` | Signed seconds against the timetable |
| `departure_epoch` | INTEGER | yes | | `stopTimeUpdate[].departure.time` | Predicted departure, Unix epoch |
| `departure_delay_s` | INTEGER | yes | | `stopTimeUpdate[].departure.delay` | Signed seconds — the client's metric |
| `schedule_relationship` | INTEGER | yes | FK → `ref_schedule_relationship(code)` | `stopTimeUpdate[].scheduleRelationship` | **stop-level** enum: 0 SCHEDULED, **1 SKIPPED**, **2 NO_DATA**, 3 UNSCHEDULED |

⚠️ **The stop-level enum is not the trip-level enum, though they share the
integers.** At stop level, **1 = SKIPPED** (a cancellation of this call) and
**2 = NO_DATA** (the operator has no prediction — the train is still expected).
This is the opposite of the trip-level meaning of 2 (UNSCHEDULED), and reading
stop-level 2 as a cancellation is a large error: the great majority of calls in
a typical snapshot carry 2/NO_DATA, and treating those as cancelled both
overstates cancellations and empties the punctuality denominator. `v_rt_departure_performance`
counts only stop-level 1 as a cancellation (`is_skipped`), reports 2 separately
(`has_no_data`), and leaves `is_on_time` NULL for both. The `ref_stop_schedule_relationship`
table (separate from the trip-level `ref_schedule_relationship`) carries the
correct labels and an `is_cancellation` flag. Per-poll counts drift with every
snapshot; `railpulse info` and the Q6 coverage query report the live figures.

### `rt_alert`

**Purpose.** A service disruption notice.
**Grain.** One row per (snapshot, alert entity).
**Rows.** 169 across 8 polls. `cause` is 10 (CONSTRUCTION) on 161 and
1 (UNKNOWN_CAUSE) on 8; `effect` is 6 (MODIFIED_SERVICE) on 88, 1 (NO_SERVICE)
on 57, 8 (UNKNOWN_EFFECT) on 16 and 3 (SIGNIFICANT_DELAYS) on 8. `url` is
populated on every row.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK, FK → `rt_snapshot(snapshot_id)` ON DELETE CASCADE | — | Which poll |
| `rt_entity_id` | TEXT | no | PK | `entity[].id` | e.g. `rs:nmbssncb:1c07417c` |
| `cause` | INTEGER | yes | FK → `ref_alert_cause(code)` | `alert.cause` | Why |
| `effect` | INTEGER | yes | FK → `ref_alert_effect(code)` | `alert.effect` | What it does to the service |
| `url` | TEXT | yes | | `alert.url.translation[].text` | Detail page; the feed ships one URL per language, of which one is stored |

### `rt_alert_text`

**Purpose.** The multilingual header and description of an alert.
**Grain.** One row per (snapshot, alert, field, language).
**Rows.** 1 352 — exactly 8 per alert, `header` and `description` in each of
`fr`, `nl`, `de` and `en`, with no gaps.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK, FK → `rt_alert` ON DELETE CASCADE | — | Which poll |
| `rt_entity_id` | TEXT | no | PK, FK → `rt_alert` ON DELETE CASCADE | — | Which alert |
| `field_name` | TEXT | no | PK | — | `header` or `description`; `CHECK`-constrained |
| `language` | TEXT | no | PK | `…Text.translation[].language` | `fr`, `nl`, `de`, `en` in the sample |
| `text` | TEXT | no | | `…Text.translation[].text` | The translated string |

Storing this as `header_fr` / `header_nl` / `header_de` / `header_en` columns
would be a textbook 1NF violation and would need a schema migration the day the
operator adds a fifth language.

### `rt_alert_informed_entity`

**Purpose.** Which part of the network an alert is about.
**Grain.** One row per (snapshot, alert, informed-entity position).
**Rows.** 169 — exactly one per alert.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK, FK → `rt_alert` ON DELETE CASCADE | — | Which poll |
| `rt_entity_id` | TEXT | no | PK, FK → `rt_alert` ON DELETE CASCADE | — | Which alert |
| `entity_seq` | INTEGER | no | PK | array position | Position in `alert.informedEntity[]` |
| `agency_id` | TEXT | yes | | `informedEntity[].agencyId` | Agency-wide scope |
| `route_id` | TEXT | yes | | `informedEntity[].routeId` | Route scope |
| `stop_id` | TEXT | yes | | `informedEntity[].stopId` | Stop scope |
| `trip_id` | TEXT | yes | | `informedEntity[].trip.tripId` | Trip scope |

On all 169 loaded rows the only key present is `agency_id`; `route_id`,
`stop_id` and `trip_id` are NULL throughout, so every alert observed so far is
agency-wide. The table still models the full GTFS-RT shape so a future route- or
stop-scoped alert lands correctly without a migration. Note the consequence for
analysis: nothing in this feed's alerts can currently be attributed to a
specific station or line.

### `rt_alert_active_period`

**Purpose.** When an alert applies.
**Grain.** One row per (snapshot, alert, active-period position).
**Rows.** 0 — the only empty real-time table.

| Column | Type | Null | Key | GTFS-RT source | Meaning |
|---|---|---|---|---|---|
| `snapshot_id` | INTEGER | no | PK, FK → `rt_alert` ON DELETE CASCADE | — | Which poll |
| `rt_entity_id` | TEXT | no | PK, FK → `rt_alert` ON DELETE CASCADE | — | Which alert |
| `period_seq` | INTEGER | no | PK | array position | Position in `alert.activePeriod[]` |
| `start_epoch` | INTEGER | yes | | `activePeriod[].start` | Unix epoch; NULL = "already active" |
| `end_epoch` | INTEGER | yes | | `activePeriod[].end` | Unix epoch; NULL = "until further notice" |

`activePeriod` is an empty array on every alert the feed has served — 21 in the
cached sample and all 169 loaded rows — so the table stays empty even with the
poller running. Read it as "no bounded window published", not as "no alert was
active".

---

## Views — the semantic layer

A view here is a *named join*, not a materialised copy. Its job is to make sure
that "a departure", "a morning trip" and "a high-frequency service" mean exactly
one thing across every query, chart and conversation with the client, instead of
five analysts each re-deriving them slightly differently in five `WHERE`
clauses. All five graded answers are written against these views.

### `v_departure`

**Purpose.** THE canonical departure event, enriched with its platform, station,
trip and route.
**Grain.** One row per scheduled call at which a passenger may actually board —
the same grain as `stop_time`, minus the excluded rows. No fan-out: every join
is to a dimension on its primary key.
**Rows.** 1 453 221, from 2 165 507 in `stop_time`.

**Definitional decisions this view encodes:**

- **`is_boardable = 1`.** Excludes all 712 286 calls with `pickup_type = 1` —
  the 577 462 technical pass-throughs where the train serves the platform but
  nobody may get on, plus the 134 824 terminus arrivals. Counting them would
  inflate every hub in the report.
- **`departure_secs IS NOT NULL`.** Excludes calls with no published departure.
  In this feed the filter removes nothing further, because every surviving
  `stop_time` row has both times populated; it is there so the definition stays
  correct against a feed where that is not true.
- **`route_type` is deliberately not filtered.** The network includes 270
  rail-replacement bus routes which are genuinely part of SNCB's scheduled
  service. Individual queries opt in or out and say which they did.
- **Hour semantics.** `departure_hour` is the clock hour a passenger reads, so a
  GTFS `24:20:00` counts towards hour 00 and not a fictional hour 24.

**Columns.** `trip_id`, `stop_sequence`, `stop_id`, `station_id`,
`station_name`, `platform_code`, `has_platform_code`, `departure_time`,
`departure_secs`, `departure_hour`, `day_offset`, `pickup_type`,
`drop_off_type`, `route_id`, `service_id`, `trip_headsign`, `trip_short_name`,
`bikes_allowed`, `wheelchair_accessible`, `route_short_name`, `route_long_name`,
`route_type`, `route_type_label`. Types and meanings are inherited unchanged
from `stop_time`, `platform`, `station`, `trip`, `route` and `ref_route_type`.

`is_boardable` is intentionally absent from the projection: inside this view it
is always 1, and exposing it would invite a redundant filter.

### `v_trip_service_days`

**Purpose.** How many calendar days each trip actually runs. The weighting
factor that converts timetable rows into real departures.
**Grain.** One row per trip.
**Rows.** 134 809 — every trip has at least one operating day.

| Column | Type | Meaning |
|---|---|---|
| `trip_id` | TEXT | From `trip` |
| `operating_days` | INTEGER | Count of `service_date` rows with `exception_type = 1`; range 1 to 318, mean 9.3 |
| `first_operating_day` | TEXT | Earliest operating date, ISO |
| `last_operating_day` | TEXT | Latest operating date, ISO |

**Definitional decisions.** Only `exception_type = 1` days are counted. The feed
describes 358 individual dates, not "a week", and each trip carries its own
calendar: some run 318 times, some run once. So a plain `COUNT(*)` over
`stop_time` answers "how many rows are in the timetable file", not "how many
trains actually depart". Joining this view and summing `operating_days` is what
moves Q1's answer from 10:00 to 17:00.

### `v_trip_origin`

**Purpose.** Where each trip starts. "A trip that departs before 12:00" is a
statement about the origin, not about every intermediate station the trip
happens to leave in the morning.
**Grain.** One row per trip — the first boardable call, by `stop_sequence`.
**Rows.** 134 809. Of these, 54 367 have `departure_secs < 43200`, i.e. an
origin departure before 12:00:00 — the population Q3 is asked about.

**Columns.** `trip_id`, `stop_sequence`, `stop_id`, `station_id`,
`station_name`, `platform_code`, `departure_time`, `departure_secs`,
`departure_hour`, `day_offset`, `route_id`, `service_id`, `trip_headsign`,
`trip_short_name`, `route_short_name`, `route_long_name`, `route_type`.

**Definitional decisions.** "First" means lowest `stop_sequence` **among
boardable calls**, since the view is built on `v_departure`; a trip that
technically passes a platform before its commercial origin is not counted as
starting there. Implemented with `ROW_NUMBER() OVER (PARTITION BY trip_id ORDER
BY stop_sequence) = 1`, which reads the table once, rather than a correlated
`MIN(stop_sequence)` subquery that reads it once per trip.

### `v_service_frequency`

**Purpose.** The weekly rhythm of every service, derived — because
`calendar.txt` cannot supply it (DQ-01).
**Grain.** One row per service.
**Rows.** 51 593 — every service in `service` has at least one operating date.

| Column | Type | Meaning |
|---|---|---|
| `service_id` | TEXT | From `service` |
| `operating_days` | INTEGER | Raw annual total, for weighting |
| `distinct_weekdays` | INTEGER | How many of the 7 weekdays the service ever touches |
| `typical_days_per_week` | INTEGER | **Modal** days-per-week across the weeks the service is active |
| `max_days_per_week` | INTEGER | Busiest single week |
| `active_weeks` | INTEGER | Weeks in which the service runs at all |
| `first_operating_day` | TEXT | ISO |
| `last_operating_day` | TEXT | ISO |
| `frequency_class` | TEXT | `High Frequency`, `Medium Frequency` or `Low Frequency/Special` |

**Definitional decisions.**

- **Three measures, because they disagree and the disagreement is informative.**
  `distinct_weekdays` scores a Mon-Fri commuter service 5 whether it runs for
  one week or fifty. `typical_days_per_week` is the honest reading of "operates
  N days a week" and is what the classification uses.
- **The class boundaries are the brief's:** `>= 5` High, 2-4 Medium, else Low.
  They live in the view so Q4, the dashboard and any ad-hoc query cannot drift
  apart on where the cut sits.
- **Modal ties break towards the busier week**, so a service split 50/50 between
  4 and 5 days is not under-reported.
- **Weeks are bucketed from a fixed Monday epoch** (1970-01-05) rather than with
  `strftime('%W')`, which resets its counter each new year and would split the
  week straddling 2025-12-29 → 2026-01-04 into two half-weeks, dragging those
  services' typical count down.

Distribution as built:

| `typical_days_per_week` | Services | | `frequency_class` | Services | Share |
|---|---|---|---|---|---|
| 1 | 10 376 | | Low Frequency/Special | 10 376 | 20.11% |
| 2 | 14 215 | | Medium Frequency | 17 877 | 34.65% |
| 3 | 1 384 | | High Frequency | 23 340 | 45.24% |
| 4 | 2 278 | | | | |
| 5 | 16 541 | | | | |
| 6 | 379 | | | | |
| 7 | 6 420 | | | | |

### `v_trip_amenity`

**Purpose.** Passenger-amenity flags resolved to labels, with "no" and
"unstated" kept apart.
**Grain.** One row per trip.
**Rows.** 134 809.

| Column | Type | Meaning |
|---|---|---|
| `trip_id`, `route_id`, `service_id`, `trip_headsign` | TEXT | From `trip` |
| `route_short_name`, `route_long_name`, `route_type` | TEXT / INTEGER | From `route` |
| `bikes_allowed` | INTEGER | Raw GTFS code |
| `bikes_allowed_label` | TEXT | From `ref_accessibility` |
| `guarantees_bikes` | INTEGER | 1 only for code 1; 123 051 trips |
| `bikes_is_unknown` | INTEGER | 1 for code 0; 11 758 trips |
| `wheelchair_accessible` | INTEGER | Raw GTFS code |
| `wheelchair_label` | TEXT | From `ref_accessibility` |
| `guarantees_wheelchair` | INTEGER | 1 only for code 1; **0 trips** |
| `wheelchair_is_unknown` | INTEGER | 1 for code 0; **all 134 809 trips** |
| `guarantees_any_amenity` | INTEGER | Bikes OR wheelchair explicitly guaranteed |
| `guarantees_both_amenities` | INTEGER | Bikes AND wheelchair; 0 trips |

**Definitional decisions.** The view refuses to collapse GTFS code 0 into "no".
`guarantees_*` is strictly code 1, sourced from `ref_accessibility.is_guaranteed`
rather than a hard-coded literal; `*_is_unknown` is exposed alongside it so a
query can always separate an explicit refusal from silence. Reporting this
feed's `wheelchair_accessible` as "not accessible" would invent a fact about
134 809 trips.

### `v_station_daily_departures`

**Purpose.** Network-shape summary for the hub leaderboard and the dashboard's
station picker.
**Grain.** One row per station, including the 17 stations with no boardable
departure at all — the join chain is `LEFT`, so they appear with zeros rather
than vanishing.
**Rows.** 652.

| Column | Type | Meaning |
|---|---|---|
| `station_id`, `station_name`, `latitude`, `longitude` | — | From `station` |
| `numbered_platforms` | INTEGER | `COUNT(DISTINCT stop_id) FILTER (WHERE has_platform_code = 1)` — real tracks, excluding the unallocated-track child |
| `timetabled_departures` | INTEGER | Boardable calls as rows in the timetable |
| `routes_served` | INTEGER | Distinct routes calling |
| `distinct_destinations` | INTEGER | Distinct `trip_headsign` values |
| `annual_departures` | INTEGER | `SUM(operating_days)` — calls weighted by how often they really run; `COALESCE`d to 0 |

**Definitional decisions.** `timetabled_departures` and `annual_departures` are
both exposed for the same reason Q1 reports two numbers: the first counts rows,
the second counts trains, and the gap between them is the finding.
`numbered_platforms` excludes the `platform_code IS NULL` child every station
owns, so it matches what a passenger would count on the concourse.

Top three by `annual_departures`:

| `station_name` | `numbered_platforms` | `timetabled_departures` | `routes_served` | `distinct_destinations` | `annual_departures` |
|---|---|---|---|---|---|
| Bruxelles-Central | 6 | 50 028 | 338 | 101 | 334 810 |
| Bruxelles-Nord | 13 | 49 710 | 357 | 102 | 333 242 |
| Bruxelles-Midi | 22 | 45 553 | 364 | 104 | 306 810 |

### `v_rt_departure_performance`

**Purpose.** Real-time observations joined to the timetable — the bridge between
the static model and live operations, and the basis of any punctuality
leaderboard.
**Grain.** One row per (trip, service date, stop sequence): the **latest**
observation of that call, not one row per poll.
**Rows.** 2 014, de-duplicated down from the 14 600 raw stop-time updates. Of
those, 1 214 are `is_skipped = 1`; `is_on_time` is 1 on 571, 0 on 166 and NULL
on 1 277 (the skipped calls plus 63 with no delay reported).

**Columns.** `trip_id`, `start_date`, `stop_sequence`, `stop_id`, `station_id`,
`station_name`, `platform_code`, `route_id`, `route_short_name`,
`trip_headsign`, `scheduled_departure_time`, `scheduled_departure_hour`,
`departure_delay_s`, `arrival_delay_s`, `schedule_relationship`,
`fetched_at_utc`, `is_skipped`, `is_on_time`.

**Definitional decisions this view encodes:**

- **The most recent snapshot wins.** Polling repeatedly means the same call is
  observed many times as its prediction firms up.
  `ROW_NUMBER() … ORDER BY snapshot_id DESC = 1` keeps the latest, so averages
  are not dominated by whichever trains happened to be polled most often.
- **"Observed departure" excludes cancellations.** `is_skipped = 1` marks
  `schedule_relationship = 2`; `is_on_time` is `NULL` for those rows rather
  than 0, so a cancelled train is never counted as a late one — or, worse, as an
  on-time one.
- **"On time" means `departure_delay_s < 120`.** Two minutes is the threshold
  the brief sets (`project-instructions/README.md`: "thresholding delays under
  2 minutes"). It is *not* SNCB's own published punctuality threshold, which is
  looser, so the rate computed here is not comparable to the operator's
  headline figure. `is_on_time` is also `NULL` when no delay was reported.
- **`JOIN trip` is INNER, on purpose.** Real-time rows naming a trip the current
  static feed does not contain are silently dropped, so feed drift cannot
  contaminate a punctuality average. `platform`, `station` and `stop_time` are
  joined `LEFT`, so an unrecognised stop still yields a row with a `NULL`
  scheduled time rather than disappearing.

---

## Staging layer (`stg_*`)

Ten tables, one per file in the GTFS ZIP: `stg_agency`, `stg_feed_info`,
`stg_stops`, `stg_routes`, `stg_trips`, `stg_stop_times`, `stg_calendar`,
`stg_calendar_dates`, `stg_transfers`, `stg_translations`.

**Purpose.** The landing zone. The challenge forbids using pandas to filter or
aggregate, so the pipeline is deliberately ELT rather than ETL: Python streams
each CSV row verbatim into the matching `stg_` table and never inspects, filters,
casts or reshapes a value. Every cleaning rule, type cast, de-duplication and
integrity check lives in `03_transform.sql`, where a reviewer can read it.

**Grain.** One row per physical line of the corresponding `.txt` file.

**Design rules, uniform across all ten tables:**

- **Every column is `TEXT`.** A GTFS file is text; casting is a transform
  concern, and a failed cast at load time would abort the run and lose the row.
- **No `PRIMARY KEY`, no `FOREIGN KEY`, no `NOT NULL`, no `CHECK`.** Staging
  must be able to hold *bad* data — that is the entire point. Constraints are
  what the core model adds, and rows that cannot satisfy them are quarantined
  into `rejected_row` with a reason.
- **Column names mirror the GTFS spec exactly**, so the loader maps by header
  name. The SNCB feed ships its columns in alphabetical order rather than spec
  order, so positional loading would silently corrupt the data.
- **`src_line_no`** is the one added column: the physical line number in the
  source file, so a quarantined row can be traced back to the exact line of the
  exact file.

**Lifecycle.** `07_cleanup.sql` drops all ten once the transform has succeeded,
and `build.py` then `VACUUM`s so the freed pages return to the OS. Staging is
458 MiB of untyped duplicate by `dbstat` — `stg_stop_times` 249 MiB and
`stg_calendar_dates` 188 MiB between them account for almost all of it — and no
analytical statement queries any of it.
`railpulse build --keep-staging` retains them — for debugging a transform rule
against the exact bytes that produced it, or inspecting a row referenced by
`rejected_row.src_line_no`. **The shipped database was built that way, so the
`stg_*` tables are present in it.**

Staged row counts, and what the transform did with them:

| Staging table | Rows | Core destination | Loaded | Rejected |
|---|---|---|---|---|
| `stg_agency` | 1 | `agency` | 1 | 0 |
| `stg_feed_info` | 1 | `feed_info` | 1 | 0 |
| `stg_stops` | 2 895 | `station` + `platform` | 652 + 2 243 | 0 |
| `stg_routes` | 1 801 | `route` | 1 801 | 0 |
| `stg_trips` | 134 809 | `trip` | 134 809 | 0 |
| `stg_stop_times` | 2 165 519 | `stop_time` | 2 165 507 | 12 (DQ-03) |
| `stg_calendar` | 51 593 | `service` | 51 593 | 0 |
| `stg_calendar_dates` | 4 697 139 | `service_date` | 4 697 139 | 0 |
| `stg_transfers` | 733 | `transfer` | 733 | 0 |
| `stg_translations` | 2 599 | `text_translation` | 2 599 | 0 |

Fields present in staging but **not carried** into the core model, because they
are empty on every row (rule DQ-08) or absent from the file header entirely:

| Staging column | State in this feed |
|---|---|
| `stg_stops.stop_code`, `.stop_url`, `.zone_id`, `.wheelchair_boarding` | empty string on all 2 895 rows |
| `stg_stops.level_id`, `.stop_timezone` | column absent from the file — NULL throughout |
| `stg_routes.route_desc`, `.route_url` | empty string on all 1 801 rows |
| `stg_routes.route_sort_order` | column absent from the file |
| `stg_trips.shape_id`, `.direction_id`, `.wheelchair_accessible` | empty string on all 134 809 rows |
| `stg_trips.bikes_allowed` | empty on 11 758 rows (every bus trip) |
| `stg_stop_times.stop_headsign`, `.shape_dist_traveled` | empty string on all 2 165 519 rows |
| `stg_stop_times.timepoint` | column absent from the file |
| `stg_translations.record_id`, `.record_sub_id` | empty string on all 2 599 rows — this is DQ-07 |
| `stg_transfers.from_route_id`, `.to_route_id` | column absent from the file |
| `stg_calendar.monday` … `.sunday` | `0` on all 51 593 rows — this is DQ-01 |
| `stg_agency.agency_fare_url` | empty string on the single row |
| `stg_agency.agency_email` | column absent from the file |
| `stg_feed_info.default_lang`, `.feed_contact_email` | empty string on the single row |
| `stg_feed_info.feed_contact_url` | populated, but identical to `feed_publisher_url` |

`direction_id` and `wheelchair_accessible` **are** carried as columns — as NULL
and as code 0 respectively — because they are part of the GTFS contract and
their emptiness is itself a reportable finding. The rest are dropped.

---

## GTFS field → RailPulse column

For anyone arriving from the GTFS Schedule Reference. `—` means the field is not
carried; `derived` means RailPulse computes it rather than reading it.

### `agency.txt`

| GTFS field | RailPulse |
|---|---|
| `agency_id` | `agency.agency_id` |
| `agency_name` | `agency.agency_name` |
| `agency_url` | `agency.agency_url` |
| `agency_timezone` | `agency.agency_timezone` |
| `agency_lang` | `agency.agency_lang` |
| `agency_phone` | `agency.agency_phone` |
| `agency_fare_url`, `agency_email` | — |

### `feed_info.txt`

| GTFS field | RailPulse |
|---|---|
| `feed_id` | `feed_info.feed_id` |
| `feed_publisher_name` | `feed_info.feed_publisher_name` |
| `feed_publisher_url` | `feed_info.feed_publisher_url` |
| `feed_lang` | `feed_info.feed_lang` |
| `feed_start_date` | `feed_info.feed_start_date` — `TRIM`med, `YYYYMMDD` → ISO |
| `feed_end_date` | `feed_info.feed_end_date` — same |
| `feed_version` | `feed_info.feed_version` |
| `default_lang`, `feed_contact_email`, `feed_contact_url` | — |

### `stops.txt` — split on `location_type`

| GTFS field | RailPulse |
|---|---|
| `stop_id` where `location_type = 1` | `station.station_id` |
| `stop_id` where `location_type = 0` | `platform.stop_id` |
| `stop_name` | `station.station_name` only — always identical on the child, so not stored twice |
| `stop_lat` / `stop_lon` | `station.latitude` / `.longitude`, `platform.latitude` / `.longitude` |
| `stop_desc` | `platform.stop_desc` |
| `parent_station` | `platform.station_id` |
| `platform_code` | `platform.platform_code`; `platform.has_platform_code` derived |
| `wheelchair_boarding` | `station.wheelchair_boarding` — empty in feed, defaulted to 0 |
| `location_type` | not stored; it is the split predicate. Codes documented in `ref_location_type` |
| `stop_code`, `stop_url`, `zone_id`, `stop_timezone`, `level_id` | — |

### `routes.txt`

| GTFS field | RailPulse |
|---|---|
| `route_id` | `route.route_id` |
| `agency_id` | `route.agency_id` |
| `route_short_name` | `route.route_short_name` |
| `route_long_name` | `route.route_long_name` |
| `route_type` | `route.route_type` → `ref_route_type` |
| `route_color` | `route.route_color` |
| `route_text_color` | `route.route_text_color` |
| `route_desc`, `route_url`, `route_sort_order` | — |

### `trips.txt`

| GTFS field | RailPulse |
|---|---|
| `trip_id` | `trip.trip_id` |
| `route_id` | `trip.route_id` |
| `service_id` | `trip.service_id` |
| `trip_headsign` | `trip.trip_headsign` |
| `trip_short_name` | `trip.trip_short_name` |
| `block_id` | `trip.block_id` |
| `direction_id` | `trip.direction_id` — NULL throughout this feed |
| `bikes_allowed` | `trip.bikes_allowed` → `ref_accessibility` |
| `wheelchair_accessible` | `trip.wheelchair_accessible` → `ref_accessibility` |
| `shape_id` | — (no `shapes.txt` in this feed) |

### `stop_times.txt`

| GTFS field | RailPulse |
|---|---|
| `trip_id` | `stop_time.trip_id` |
| `stop_sequence` | `stop_time.stop_sequence` |
| `stop_id` | `stop_time.stop_id` |
| `arrival_time` | `stop_time.arrival_time`; `arrival_secs`, `arrival_hour` derived |
| `departure_time` | `stop_time.departure_time`; `departure_secs`, `departure_hour`, `day_offset` derived |
| `pickup_type` | `stop_time.pickup_type` → `ref_pickup_drop`; `is_boardable` derived |
| `drop_off_type` | `stop_time.drop_off_type` → `ref_pickup_drop`; `is_alightable` derived |
| `stop_headsign` | `stop_time.stop_headsign` — NULL throughout this feed |
| `shape_dist_traveled`, `timepoint` | — |

### `calendar.txt` and `calendar_dates.txt`

| GTFS field | RailPulse |
|---|---|
| `calendar.service_id` | `service.service_id` |
| `calendar.start_date` / `.end_date` | `service.start_date` / `.end_date`, ISO |
| `calendar.monday` … `.sunday` | `service.monday` … `.sunday` — all 0; `service.has_weekday_pattern` derived |
| `calendar_dates.service_id` | `service_date.service_id` |
| `calendar_dates.date` | `service_date.service_date`, ISO; `service_date.day_of_week` derived |
| `calendar_dates.exception_type` | `service_date.exception_type` → `ref_exception_type` |

### `transfers.txt`

| GTFS field | RailPulse |
|---|---|
| `from_stop_id` / `to_stop_id` | `transfer.from_stop_id` / `.to_stop_id` |
| `transfer_type` | `transfer.transfer_type` → `ref_transfer_type` |
| `min_transfer_time` | `transfer.min_transfer_time` |
| `from_trip_id` / `to_trip_id` | `transfer.from_trip_id` / `.to_trip_id` |
| `from_route_id`, `to_route_id` | — (absent from this feed) |
| — | `transfer.transfer_id`, surrogate PK |

### `translations.txt`

| GTFS field | RailPulse |
|---|---|
| `table_name` | `text_translation.table_name` |
| `field_name` | `text_translation.field_name` |
| `field_value` | `text_translation.field_value` — the join key, because `record_id` is empty |
| `language` | `text_translation.language` |
| `translation` | `text_translation.translation` |
| `record_id`, `record_sub_id` | — (empty on all 2 599 rows) |

### GTFS-Realtime

| GTFS-RT path | RailPulse |
|---|---|
| `header.timestamp` | `rt_snapshot.feed_timestamp_epoch` / `.feed_timestamp_utc` |
| `entity[].id` | `rt_trip_update.rt_entity_id`, `rt_alert.rt_entity_id` |
| `tripUpdate.trip.tripId` | `rt_trip_update.trip_id` — soft link, no FK |
| `tripUpdate.trip.routeId` | `rt_trip_update.route_id` |
| `tripUpdate.trip.startDate` / `.startTime` | `rt_trip_update.start_date` / `.start_time` |
| `tripUpdate.trip.scheduleRelationship` | `rt_trip_update.schedule_relationship` → `ref_schedule_relationship` |
| `tripUpdate.vehicle.id` | `rt_trip_update.vehicle_id` |
| `tripUpdate.timestamp` | `rt_trip_update.update_timestamp_epoch` |
| `stopTimeUpdate[].stopSequence` / `.stopId` | `rt_stop_time_update.stop_sequence` / `.stop_id` |
| `stopTimeUpdate[].arrival.time` / `.delay` | `rt_stop_time_update.arrival_epoch` / `.arrival_delay_s` |
| `stopTimeUpdate[].departure.time` / `.delay` | `rt_stop_time_update.departure_epoch` / `.departure_delay_s` |
| `stopTimeUpdate[].scheduleRelationship` | `rt_stop_time_update.schedule_relationship` |
| `alert.cause` / `.effect` | `rt_alert.cause` / `.effect` → `ref_alert_cause` / `ref_alert_effect` |
| `alert.headerText` / `.descriptionText` | `rt_alert_text` (one row per field per language) |
| `alert.url` | `rt_alert.url` |
| `alert.informedEntity[]` | `rt_alert_informed_entity` |
| `alert.activePeriod[]` | `rt_alert_active_period` |
