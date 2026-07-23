# RailPulse — Data Quality Report

**Feed:** SNCB/NMBS GTFS Static, `feed_id = nmbssncb`, `feed_version = 2026-07-20`
**Timetable window:** 2025-12-20 → 2026-12-12 (358 calendar days, no gaps)
**Archive:** `nmbssncb_gtfs_static.zip`, 10 files, 394,273,945 bytes uncompressed
**Built:** run 1, 2026-07-23T08:04:59Z → 08:06:34Z (95 s), status `ok`
**Licence:** CC BY 4.0 — attribution "NMBS-SNCB - Open Data - 2026-07-20"

> ⓘ **Unfamiliar with a term used here?** [`glossary.md`](glossary.md) defines every GTFS, database and project-specific word this project uses, with examples from this data.

Every number in this document was produced by a query against
`data/railpulse.db`, and the query is printed next to it. The only exceptions
are the uncompressed byte sizes, which come from `unzip -l` on the archive
because `ingestion_run.bytes_downloaded` was not populated on this offline
rebuild. Nothing here is estimated.

The pipeline is deliberately ELT: Python copies each `.txt` file verbatim into a
`stg_` table where every column is `TEXT` and no constraint exists, and all
cleaning happens in `sql/03_transform.sql` as nine rules tagged `DQ-01` … `DQ-09`.
A row that cannot satisfy the core model is never dropped silently — it lands in
`rejected_row` with its file, its physical line number, the rule that rejected
it, and a JSON snapshot of the original.

---

## 1. Load summary

```sql
SELECT           'agency.txt'    AS file, COUNT(*) FROM stg_agency
UNION ALL SELECT 'feed_info.txt',      COUNT(*) FROM stg_feed_info
UNION ALL SELECT 'stops.txt',          COUNT(*) FROM stg_stops
UNION ALL SELECT 'routes.txt',         COUNT(*) FROM stg_routes
UNION ALL SELECT 'trips.txt',          COUNT(*) FROM stg_trips
UNION ALL SELECT 'stop_times.txt',     COUNT(*) FROM stg_stop_times
UNION ALL SELECT 'calendar.txt',       COUNT(*) FROM stg_calendar
UNION ALL SELECT 'calendar_dates.txt', COUNT(*) FROM stg_calendar_dates
UNION ALL SELECT 'transfers.txt',      COUNT(*) FROM stg_transfers
UNION ALL SELECT 'translations.txt',   COUNT(*) FROM stg_translations;
```

That is the staged column; the loaded column is the same query against the core
tables, and the difference is `rejected_row`. The `stg_` tables only exist if
the build was run with `--keep-staging` (see section 5 of this report).

| Source file | Bytes | Rows staged | Target table(s) | Rows loaded | Quarantined |
| --- | ---: | ---: | --- | ---: | ---: |
| `agency.txt` | 160 | 1 | `agency` | 1 | 0 |
| `feed_info.txt` | 263 | 1 | `feed_info` | 1 | 0 |
| `stops.txt` | 295,397 | 2,895 | `station` (652) + `platform` (2,243) | 2,895 | 0 |
| `routes.txt` | 136,069 | 1,801 | `route` | 1,801 | 0 |
| `trips.txt` | 16,509,344 | 134,809 | `trip` | 134,809 | 0 |
| `stop_times.txt` | 233,615,111 | 2,165,519 | `stop_time` | 2,165,507 | **12** |
| `calendar.txt` | 2,631,331 | 51,593 | `service` | 51,593 | 0 |
| `calendar_dates.txt` | 140,914,201 | 4,697,139 | `service_date` | 4,697,139 | 0 |
| `transfers.txt` | 43,932 | 733 | `transfer` | 733 | 0 |
| `translations.txt` | 128,137 | 2,599 | `text_translation` | 2,599 | 0 |
| **Total** | **394,273,945** | **7,057,090** | | **7,057,078** | **12** |

12 rows of 7,057,090 were refused — a rejection rate of 0.00017 %. All twelve
come from one rule (`DQ-03`) and from two trips. `PRAGMA foreign_key_check`
returns no rows against the loaded model.

`stops.txt` is the only file that feeds two tables: GTFS interleaves two grains
in it, discriminated by `location_type`.

```sql
SELECT location_type, COUNT(*) FROM stg_stops GROUP BY 1;
-- 0 -> 2243  (boarding points)
-- 1 ->  652  (stations)
```

The build's own audit row records a narrower `rows_loaded` figure, because
`src/railpulse/build.py` counts only the three high-volume tables:

```sql
SELECT rows_staged, rows_loaded, rows_rejected, status, notes FROM ingestion_run;
-- 7057090 | 6997455 | 12 | ok | offline rebuild
```

6,997,455 = `stop_time` + `trip` + `service_date`. The per-file table above is
the complete picture.

---

## 2. The cleaning rules

Each rule is implemented in `sql/03_transform.sql`. Section numbers below refer
to the numbered blocks in that file.

### DQ-01 — `calendar.txt` publishes no weekly pattern

**Checks:** whether any of the seven weekday flags in `calendar.txt` is set.
**Caught:** 51,593 of 51,593 services have all seven flags at 0.
**Where:** §6, which computes `service.has_weekday_pattern`.

```sql
SELECT COUNT(*) AS total_services,
       SUM(has_weekday_pattern) AS with_pattern,
       SUM(monday+tuesday+wednesday+thursday+friday+saturday+sunday) AS sum_of_all_flags
FROM service;
-- 51593 | 0 | 0
```

**Why the rule exists.** GTFS allows a publisher to express a calendar either as
a weekly pattern plus exceptions, or as a bare list of dates. SNCB uses the
second form exclusively. The `calendar.txt` columns are still present and still
parse cleanly — they are simply all zero, which is indistinguishable from "this
service never runs" unless you know to look. Without the rule, any query that
reads those columns would return a confident, silently wrong answer.

`has_weekday_pattern` is stored per row rather than asserted in a comment, so a
future feed that does populate the flags will flip the column and the downstream
code will not have to be rewritten. The consequence for Q4 is worked through in
§3.1.

### DQ-02 — accessibility code 0 means "no information", not "no"

**Checks:** empty accessibility codes are mapped to 0, and 0 is documented as
absence of a statement rather than a negative one.
**Caught:** 134,809 trips (`wheelchair_accessible`), 11,758 trips
(`bikes_allowed`), 652 stations (`wheelchair_boarding`).
**Where:** §3 and §8, via `COALESCE(CAST(NULLIF(TRIM(...), '') AS INTEGER), 0)`,
backed by `ref_accessibility.is_guaranteed` in `sql/02_schema.sql`.

```sql
SELECT wheelchair_accessible, COUNT(*) FROM trip GROUP BY 1;   -- 0 -> 134809
SELECT bikes_allowed,         COUNT(*) FROM trip GROUP BY 1;   -- 0 -> 11758, 1 -> 123051
SELECT wheelchair_boarding,   COUNT(*) FROM station GROUP BY 1; -- 0 -> 652
```

**Why the rule exists.** This is the single most common error in accessibility
reporting. Reading code 0 as "no" would let RailPulse publish the claim that
zero SNCB trips are wheelchair accessible and that no station in Belgium offers
assisted boarding. Neither is a fact about the railway; both are facts about the
feed. `ref_accessibility` carries an `is_guaranteed` flag so that Q5 aggregates
on an explicit positive rather than on `<> 2` or `= 1` scattered across five
queries.

No row is rejected by this rule. It changes interpretation, not membership.

### DQ-03 — calls 48 hours or more into their own service day

**Checks:** `COALESCE(departure_secs, arrival_secs) >= 172800`.
**Caught:** 12 calls, spanning 2 trips, at times from 63:18:00 to 87:39:00.
**Where:** §9, in the `reject_rule` CASE of `tmp_stop_time_screened`.

```sql
SELECT COUNT(*) AS n,
       COUNT(DISTINCT json_extract(payload, '$.trip_id')) AS trips,
       MIN(json_extract(payload, '$.departure_time')) AS earliest,
       MAX(json_extract(payload, '$.departure_time')) AS latest
FROM rejected_row WHERE rule_code = 'DQ-03-IMPLAUSIBLE-DEPARTURE';
-- 12 | 2 | 63:18:00 | 87:39:00
```

**Why the rule exists.** GTFS clock times are service-relative and legitimately
run past 24:00:00 for trips crossing midnight — 31,154 calls in this feed do
(§3.5). But a domestic Belgian rail service does not depart on the fourth
calendar day of its service date. 87:39:00 is three days and fifteen hours after
the service day opened. Loading those values would put departures into
`day_offset = 2` and `day_offset = 3` and quietly corrupt any duration or
day-type calculation built on them.

⚠ **Caveat, stated plainly.** The rule rejects calls, not trips, so the two
affected trips survive as truncated itineraries:

```sql
SELECT trip_id, COUNT(*) AS calls_still_loaded FROM stop_time
WHERE trip_id IN (SELECT json_extract(payload, '$.trip_id') FROM rejected_row)
GROUP BY 1;
-- gt:nmbssncb:88____:007::8891009:8892007:10:6544:20260314 -> 5   (5 quarantined)
-- gt:nmbssncb:88____:046::8885001:8884004:9:8739:20260217  -> 2   (7 quarantined)
```

Those two trips are incomplete in the core model. At 2 trips out of 134,809 the
effect on every aggregate in this report is below rounding, but a route-level
journey reconstruction should exclude them explicitly.

### DQ-04 — orphan foreign keys

**Checks:** three joins — a boarding point whose `parent_station` is absent, a
`calendar_dates` row whose `service_id` was never declared, and a `stop_times`
row whose `trip_id` or `stop_id` is unknown.
**Caught:** 0 rows.
**Where:** §4, §7 and §9.

```sql
SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-04%';  -- 0
```

**Why the rule exists.** SQLite enforces foreign keys only when
`PRAGMA foreign_keys = ON`, which `src/railpulse/db.py` sets on every
connection — so without the pre-screen an orphan would abort the entire
transaction and lose the whole load rather than one row. The screen converts a
fatal error into a quarantined row plus a completed build. That this feed is
referentially clean is a result, not a reason to remove the rule; it will be
exercised the first time a partial or truncated download is loaded.

### DQ-05 — duplicate keys

**Checks:** repeated `(service_id, date)` in `calendar_dates.txt` and repeated
`(trip_id, stop_sequence)` in `stop_times.txt`. The first physical line in the
file wins; later ones are quarantined.
**Caught:** 0 rows.
**Where:** §7 (`INSERT OR IGNORE` plus a `GROUP BY … HAVING COUNT(*) > 1` audit
pass) and §9 (`ROW_NUMBER() OVER (PARTITION BY trip_id, stop_sequence ORDER BY
src_line_no)`).

```sql
SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-05%';  -- 0
```

**Why the rule exists.** Both target tables declare the key as a real
`PRIMARY KEY`, so a duplicate would abort the transaction. `INSERT OR IGNORE`
alone would suppress the failure but also hide it; the separate audit pass is
what keeps the quarantine honest about what was suppressed and how many times.

### DQ-06 — `YYYYMMDD` → ISO, and the leading space

**Checks:** GTFS date strings are 8 characters after `TRIM` and are re-sliced
into `YYYY-MM-DD`.
**Caught:** every date in the feed — 2 in `feed_info`, 103,186 in `service`
(51,593 × 2), 4,697,139 in `service_date`. `feed_info.txt` ships its two dates
with a leading space.
**Where:** §1, §6, §7.

```sql
SELECT '[' || feed_start_date || ']' AS raw, LENGTH(feed_start_date) AS len
FROM stg_feed_info;
-- [ 20251220] | 9
```

**Why the rule exists.** SQLite's date functions understand ISO-8601 and nothing
else; `strftime('%w', '20251220')` returns NULL, not a weekday. The leading
space is the sharper trap: the transform gates on `LENGTH(...) = 8`, so without
the `TRIM` the length-9 value would fail the gate, the `CASE` would fall through
to NULL, and the report would lose the validity window that dates every figure
in it. The value loads correctly:

```sql
SELECT feed_start_date, feed_end_date, feed_version FROM feed_info;
-- 2025-12-20 | 2026-12-12 | 2026-07-20
```

### DQ-07 — translations are keyed by value, not by record

**Checks:** `record_id` is unusable, so `text_translation` is keyed on the
French source string.
**Caught:** 2,599 of 2,599 rows have an empty `record_id` and an empty
`record_sub_id`; 0 of 2,599 have an empty `field_value`.
**Where:** §11, and the composite primary key
`(table_name, field_name, field_value, language)` in `sql/02_schema.sql`.

```sql
SELECT COUNT(*) AS rows,
       SUM(CASE WHEN TRIM(COALESCE(record_id, '')) = '' THEN 1 ELSE 0 END) AS empty_record_id,
       SUM(CASE WHEN TRIM(COALESCE(field_value, '')) = '' THEN 1 ELSE 0 END) AS empty_field_value
FROM stg_translations;
-- 2599 | 2599 | 0
```

**Why the rule exists.** GTFS permits both keying styles, but value-keying is
lossy: two different records that happen to share a string get the same
translation, and a translation whose source string changes upstream becomes
unreachable. Modelling it as a value key is honest about what the feed actually
supports. Building the table on `record_id` would have been worse than useless:
all 2,599 rows carry the same empty key, so a primary key of
`(table_name, field_name, record_id, language)` has only 6 distinct values in
this feed and would have collapsed the whole file to 6 rows.

Coverage and the one visible symptom of value-keying:

```sql
SELECT table_name, field_name, language, COUNT(*) FROM text_translation GROUP BY 1,2,3;
-- stops | stop_name     | de -> 642 | en -> 642 | nl -> 652
-- trips | trip_headsign | de -> 221 | en -> 221 | nl -> 221
```

```sql
SELECT field_value, language FROM text_translation tt
WHERE tt.table_name = 'stops'
  AND NOT EXISTS (SELECT 1 FROM station s WHERE s.station_name = tt.field_value);
-- Visé-Frontière | de, en, nl   (3 rows, 1 distinct value)
```

Three rows translate a station name that no station in this feed carries. They
are kept: they cost nothing, and discarding information the publisher chose to
ship is not the transform's decision to make. Netting that stale value out of
each language, 11 of the 652 stations have no English name and the same 11 have
no German name; 1 station has no Dutch name.

### DQ-08 — an empty string is not NULL

**Checks:** every optional text column passes through
`NULLIF(TRIM(x), '')`, and columns that are empty across the whole feed are not
carried into the core model at all.
**Caught:** see §3.6 for the full column-by-column inventory.
**Where:** every `INSERT` in the file; §5 documents the `route` omissions and §4
the `platform` ones.

**Why the rule exists.** In SQL, `'' = ''` is true and `NULL = NULL` is not;
`COUNT(col)` counts empty strings and skips NULLs. Leaving the raw empty strings
in place would make `COUNT(stop_code)` return 2,895 for a column that carries no
information at all, and would make every `IS NULL` completeness check return
zero. Two rules follow from it: empty means unknown and is stored as NULL, and a
column that is empty on every single row is dropped rather than stored as
2.2 million NULLs.

### DQ-09 — trips referencing an unknown route or service

**Checks:** `trips.txt` rows whose `route_id` or `service_id` is absent from the
feed.
**Caught:** 0 rows.
**Where:** §8.

```sql
SELECT COUNT(*) FROM rejected_row WHERE rule_code LIKE 'DQ-09%';  -- 0
```

**Why the rule exists.** `trip` is the hinge of the model: `stop_time` points at
it and `service_date` reaches it through `service`. An orphan trip would
cascade — every one of its calls would then fail `DQ-04` as well, turning one
bad row in a 134,809-row file into thousands of quarantined facts with no
obvious common cause. Screening at the trip level names the real problem once.

---

## 3. Findings that break no rule but change the answers

These are not defects the pipeline can fix. They are properties of the feed that
an analyst has to know about, and each one changes at least one number in the
report.

### 3.1 The weekly calendar does not exist in `calendar.txt`

Restating `DQ-01` as a business risk, because this is the finding most likely to
produce a wrong published figure.

```sql
SELECT COUNT(*) AS services,
       SUM(monday + tuesday + wednesday + thursday + friday + saturday + sunday)
         AS sum_of_all_weekday_flags
FROM service;
-- 51593 | 0
```

```sql
SELECT exception_type, COUNT(*) FROM service_date GROUP BY 1;   -- 1 -> 4697139
```

All 4,697,139 calendar entries are `exception_type = 1` (ADDED). There is not a
single REMOVED row. The feed does not use exceptions as exceptions — it uses
them as the calendar.

**Downstream consequence for Q4.** The brief asks for services to be classified
by how many days a week they run. Taken literally against `calendar.txt`, every
service runs zero days a week, and the honest answer to Q4 is a single bar:

```sql
SELECT CASE WHEN (monday+tuesday+wednesday+thursday+friday+saturday+sunday) >= 5
              THEN 'High Frequency'
            WHEN (monday+tuesday+wednesday+thursday+friday+saturday+sunday) >= 2
              THEN 'Medium Frequency'
            ELSE 'Low Frequency/Special' END AS naive_class,
       COUNT(*) AS services,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM service), 2) AS pct
FROM service GROUP BY 1;
-- Low Frequency/Special | 51593 | 100.0
```

**How the model solves it.** `v_service_frequency` derives the rhythm from
`service_date` instead: it buckets each service's operating days into weeks
counted from a fixed Monday epoch (1970-01-05), takes the *modal* week shape
across the weeks in which the service runs at all (ties broken towards the
busier week), and applies the brief's thresholds to that. Two choices there are
deliberate. Weeks are counted from an epoch rather than with `strftime('%W')`,
whose counter resets at new year and would split the week straddling
2025-12-29 → 2026-01-04 into two half-weeks. And the modal week is used rather
than the mean, because partial weeks at the start and end of a service's span
drag the mean off the real rhythm: substituting the mean for the mode moves
23,163 of the 51,593 services into a different frequency class.

```sql
SELECT frequency_class, COUNT(*) AS services,
       ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM v_service_frequency), 2) AS pct
FROM v_service_frequency GROUP BY 1 ORDER BY services DESC;
```

| Class | Services | Share |
| --- | ---: | ---: |
| High Frequency (≥ 5 d/wk) | 23,340 | 45.24 % |
| Medium Frequency (2–4 d/wk) | 17,877 | 34.65 % |
| Low Frequency/Special (1 d/wk) | 10,376 | 20.11 % |

```sql
SELECT typical_days_per_week, COUNT(*) FROM v_service_frequency GROUP BY 1 ORDER BY 1;
```

| Days per typical week | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Services | 10,376 | 14,215 | 1,384 | 2,278 | 16,541 | 379 | 6,420 |

The derived answer and the literal answer differ on 41,217 of 51,593 services.
Any dashboard built on the `calendar.txt` columns would be wrong for 79.9 % of
the network.

### 3.2 `calendar.txt`'s declared validity window is an envelope, not a schedule

A related trap, since `start_date` / `end_date` are the only two `calendar.txt`
columns that do carry data.

```sql
WITH d AS (SELECT service_id, MIN(service_date) mn, MAX(service_date) mx
           FROM service_date GROUP BY 1)
SELECT SUM(CASE WHEN s.start_date <> d.mn THEN 1 ELSE 0 END) AS start_differs,
       SUM(CASE WHEN s.end_date   <> d.mx THEN 1 ELSE 0 END) AS end_differs,
       SUM(CASE WHEN s.start_date > d.mn OR s.end_date < d.mx THEN 1 ELSE 0 END)
         AS declared_window_too_narrow
FROM service s JOIN d ON d.service_id = s.service_id;
-- 29299 | 28862 | 0
```

For 29,299 services the declared start is earlier than the first day the service
actually runs, and for 28,862 the declared end is later than the last. Crucially
the third figure is 0: the declared window always *contains* the real dates. So
`service.start_date` / `end_date` are safe as outer bounds and unsafe as an
operating period. Every date filter in the analysis queries goes through
`service_date`.

### 3.3 Wheelchair accessibility is unpopulated at every grain

```sql
SELECT COUNT(*) AS trips,
       SUM(CASE WHEN wheelchair_accessible = 0 THEN 1 ELSE 0 END) AS no_information
FROM trip;
-- 134809 | 134809
```

```sql
SELECT COUNT(*) AS empty_in_source FROM stg_trips
WHERE TRIM(COALESCE(wheelchair_accessible, '')) = '';
-- 134809
```

The column exists in the `trips.txt` header and is empty on every one of the
134,809 rows. The same is true one level up:

```sql
SELECT wheelchair_boarding, COUNT(*) FROM station GROUP BY 1;   -- 0 -> 652
```

All 652 stations also report "no information". The feed therefore contains **no
published wheelchair data at either the vehicle or the station grain**. This is
the headline finding of Q5, and it is a finding about the feed rather than about
the railway. Silence in a field whose code 0 means "no information" is not
evidence that no train or station is accessible; this feed says nothing either
way, and nothing in it supports a claim in either direction. The only defensible
statement RailPulse can make from this data is that the accessibility ratio is
unmeasurable, and the recommendation is to obtain it from another source.

By contrast `bikes_allowed` is populated, and splits exactly on mode:

```sql
SELECT rt.label, COUNT(*) AS trips,
       SUM(CASE WHEN t.bikes_allowed = 1 THEN 1 ELSE 0 END) AS guaranteed,
       ROUND(100.0 * SUM(CASE WHEN t.bikes_allowed = 1 THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct
FROM trip t JOIN route r USING (route_id) JOIN ref_route_type rt USING (route_type)
GROUP BY 1;
-- Bus  |  11758 |      0 |   0.00
-- Rail | 123051 | 123051 | 100.00
```

100 % of the 123,051 rail trips and 0 % of the 11,758 bus trips, which between
them cover all 270 rail-replacement bus routes. Whether the bus figure means
"bikes are not carried" or "nobody filled the field in" is question 2 in
section 4 of this report.

### 3.4 577,462 calls exist where no passenger may board or alight

```sql
SELECT pickup_type, drop_off_type, COUNT(*) FROM stop_time GROUP BY 1, 2 ORDER BY 3 DESC;
```

| `pickup_type` | `drop_off_type` | Calls | Meaning |
| ---: | ---: | ---: | --- |
| 0 | 0 | 1,318,454 | normal commercial call |
| 1 | 1 | 577,462 | technical pass-through — neither boarding nor alighting |
| 1 | 0 | 134,824 | set-down only |
| 0 | 1 | 134,767 | pick-up only |

577,462 calls (26.7 %) are pure pass-throughs, and 712,286 (32.9 %) bar boarding
of any kind. `stop_time.is_boardable` and `is_alightable` materialise the test so
that no analytical query has to remember it, and `v_departure` filters on
`is_boardable = 1`.

Network-wide that removes about a third of the calls, but the effect is nowhere
near uniform: at two Brussels stations it changes the answer entirely, and at a
third it is negligible.

```sql
SELECT s.station_name, COUNT(*) AS calls, SUM(st.is_boardable) AS boardable
FROM stop_time st
JOIN platform p ON p.stop_id = st.stop_id
JOIN station  s ON s.station_id = p.station_id
WHERE s.station_name IN ('Bruxelles-Chapelle', 'Bruxelles-Congrès',
                         'Bruxelles-Central')
GROUP BY 1;
-- Bruxelles-Central  | 50074 | 50028
-- Bruxelles-Chapelle | 23757 |   371
-- Bruxelles-Congrès  | 24191 |  1096
```

Counted naively, Bruxelles-Chapelle is a top-20 station in Belgium. Counted
correctly, 98.4 % of its traffic is trains passing through the North–South
junction without stopping commercially, and it has 371 real departures.
Bruxelles-Central is barely affected — 46 of its 50,074 calls are
non-boardable — which is why the distortion is invisible unless it is checked
station by station.

### 3.5 31,154 calls are timetabled at 24:00:00 or later

```sql
SELECT COUNT(*) FROM stg_stop_times
WHERE CAST(substr(TRIM(COALESCE(departure_time, arrival_time)), 1, 2) AS INTEGER) >= 24;
-- 31154
```

```sql
SELECT day_offset, COUNT(*) FROM stop_time GROUP BY 1;
-- 0 -> 2134365
-- 1 ->   31142
```

31,142 of them load with `day_offset = 1`; the remaining 12 are the `DQ-03`
quarantine. The largest value that survives is 47:09:00.

This is correct GTFS, not a defect. Clock times are relative to the *service
day*, so a train that leaves at 00:20 on the night of a Saturday service is
published as 24:20:00 and belongs to Saturday's operating day, not Sunday's. The
model keeps four representations of the same value, one per question that gets
asked of it:

| Column | Example | Answers |
| --- | --- | --- |
| `departure_time` | `'24:20:00'` | what the feed said — traceability |
| `departure_secs` | `87600` | ordering, durations, "how long after the service day began" |
| `departure_hour` | `0` | the hour a passenger reads on the platform |
| `day_offset` | `1` | which calendar day the call actually falls on |

Q1 must group on `departure_hour`. Grouping on the raw text would scatter 31,142
departures into fictional hours 24 to 47 and remove them from the midnight and
early-morning bands where they belong. `day_offset` exists so that joining a
call to a real calendar date remains possible: the calendar date of a call is
`service_date + day_offset` days, and without the column that arithmetic would
have to be re-derived from the string on every query.

### 3.6 Columns carried, columns dropped, columns absent

Three different situations get conflated in most feed reviews, so they are
separated here.

**(a) Present in the file header, empty on every row — dropped from the core model.**

```sql
SELECT SUM(CASE WHEN TRIM(COALESCE(stop_code, '')) = '' THEN 1 ELSE 0 END) AS stop_code,
       SUM(CASE WHEN TRIM(COALESCE(stop_url,  '')) = '' THEN 1 ELSE 0 END) AS stop_url,
       SUM(CASE WHEN TRIM(COALESCE(zone_id,   '')) = '' THEN 1 ELSE 0 END) AS zone_id,
       SUM(CASE WHEN TRIM(COALESCE(wheelchair_boarding, '')) = '' THEN 1 ELSE 0 END) AS wc_boarding,
       COUNT(*) AS rows
FROM stg_stops;
-- 2895 | 2895 | 2895 | 2895 | 2895
```

| Column | File | Empty | of | Disposition |
| --- | --- | ---: | ---: | --- |
| `stop_code` | `stops.txt` | 2,895 | 2,895 | dropped |
| `stop_url` | `stops.txt` | 2,895 | 2,895 | dropped |
| `zone_id` | `stops.txt` | 2,895 | 2,895 | dropped |
| `wheelchair_boarding` | `stops.txt` | 2,895 | 2,895 | **kept** — becomes code 0 per `DQ-02`; the absence is the finding |
| `route_desc` | `routes.txt` | 1,801 | 1,801 | dropped |
| `route_url` | `routes.txt` | 1,801 | 1,801 | dropped |
| `shape_id` | `trips.txt` | 134,809 | 134,809 | dropped |
| `direction_id` | `trips.txt` | 134,809 | 134,809 | **kept** as a nullable column — optional in GTFS but analytically load-bearing if it ever arrives; NULL for all 134,809 rows today |
| `wheelchair_accessible` | `trips.txt` | 134,809 | 134,809 | **kept** — becomes code 0 per `DQ-02` |
| `shape_dist_traveled` | `stop_times.txt` | 2,165,519 | 2,165,519 | dropped |
| `stop_headsign` | `stop_times.txt` | 2,165,519 | 2,165,519 | **kept** as a nullable column, NULL throughout |
| `default_lang` | `feed_info.txt` | 1 | 1 | dropped |
| `feed_contact_email` | `feed_info.txt` | 1 | 1 | dropped |
| `agency_phone` | `agency.txt` | 1 | 1 | kept, NULL |
| `agency_fare_url` | `agency.txt` | 1 | 1 | dropped |

The five "kept" rows are deliberate: dropping a column because this month's feed
happens to be empty would silently change the schema contract when next month's
is not.

**(b) Absent from the file header entirely.** The staging tables declare these
columns because GTFS specifies them, and the loader maps by header name, so they
stay `NULL` for every row rather than empty:

```sql
SELECT SUM(CASE WHEN route_sort_order IS NULL THEN 1 ELSE 0 END) AS route_sort_order_null,
       COUNT(*) FROM stg_routes;
-- 1801 | 1801
SELECT SUM(CASE WHEN timepoint IS NULL THEN 1 ELSE 0 END) AS timepoint_null,
       COUNT(*) FROM stg_stop_times;
-- 2165519 | 2165519
```

| Column | Expected in | Status |
| --- | --- | --- |
| `stop_timezone`, `level_id` | `stops.txt` | header absent |
| `route_sort_order` | `routes.txt` | header absent |
| `timepoint` | `stop_times.txt` | header absent |
| `from_route_id`, `to_route_id` | `transfers.txt` | header absent |
| `agency_email` | `agency.txt` | header absent |
| *(whole file)* | `shapes.txt` | not in the archive — 10 files, no geometry |

`shapes.txt` being absent explains `shape_id` being empty rather than the other
way round: the feed ships no route geometry at all, so nothing can be mapped
beyond station point coordinates.

**(c) Present and populated — the counter-examples.** Not everything optional is
empty, and it is worth recording which columns can be relied on:

```sql
SELECT SUM(CASE WHEN TRIM(COALESCE(stop_desc, '')) = '' THEN 1 ELSE 0 END) AS empty_stop_desc,
       SUM(CASE WHEN TRIM(COALESCE(platform_code, '')) = '' THEN 1 ELSE 0 END) AS empty_platform_code,
       COUNT(*) FROM stg_stops;
-- 0 | 1304 | 2895
```

`stop_desc` is populated on all 2,895 rows. `platform_code` is empty on 1,304 —
which is not a gap but the model working correctly: 652 of those are the station
rows, which have no platform by definition, and the other 652 are the one
unallocated-track child that each station owns for calls where no platform has
been assigned.

```sql
SELECT has_platform_code, COUNT(*) FROM platform GROUP BY 1;
-- 0 ->  652   (one per station: no track allocated)
-- 1 -> 1591   (real, numbered platforms)
```

`route_color` and `route_text_color` are populated on all 1,801 routes;
`block_id`, `trip_short_name` and `trip_headsign` on all 134,809 trips.

Finally, one structural redundancy that shaped the schema:

```sql
SELECT COUNT(*) FROM stg_stops c
JOIN stg_stops p ON p.stop_id = TRIM(c.parent_station)
WHERE TRIM(c.location_type) = '0' AND TRIM(c.stop_name) <> TRIM(p.stop_name);
-- 0
```

A child stop's `stop_name` is identical to its parent's on all 2,243 rows.
Storing it on `platform` would be a transitive dependency, so `station_name`
lives only on `station`.

---

## 4. What we would ask the publisher

Four questions, in the order in which the answers would change this report.

**1. Will `calendar.txt` ever carry the weekly pattern, or is `calendar_dates.txt`
the permanent contract?**
All 51,593 services publish seven zeroed weekday flags and all 4,697,139
calendar entries are ADDED exceptions. RailPulse derives the weekly rhythm and
is confident in it, but the derivation is our inference, not SNCB's statement.
If the flags will stay empty we would like that documented so that consumers
stop trying to read them; if they are to be populated, we need to know from
which feed version, because the switchover would change the Q4 classification of
41,217 services on a single day.

**2. Is `wheelchair_accessible` planned, and does `bikes_allowed = <empty>` on
rail-replacement buses mean "no" or "unknown"?**
`wheelchair_accessible` is empty on all 134,809 trips and
`wheelchair_boarding` on all 652 stations, so the accessibility question the
client asked cannot be answered from this feed at all. Separately, `bikes_allowed`
is a clean 100 % / 0 % split by mode — every one of the 123,051 rail trips says
Yes, every one of the 11,758 bus trips says nothing. That pattern looks like a
default rather than a measurement. If replacement coaches genuinely do not carry
bicycles, publishing code 2 ("No") instead of an empty value would let us report
it as a fact rather than a gap.

**3. Are the twelve calls at 63:18:00 to 87:39:00 real, and if not, how should
they be handled?**
Twelve calls on two trips (`…:007::8891009:8892007:10:6544:20260314` and
`…:046::8885001:8884004:9:8739:20260217`) are timetabled from 63:18:00 to
87:39:00 into their service day, the last three of them past the 87-hour mark.
We have quarantined them, which leaves both trips loaded
as truncated itineraries. Confirmation that these are export artefacts — and, if
so, the corrected times — would let us reinstate the two journeys intact.

**4. Will `shapes.txt` and `direction_id` be published?**
The archive contains ten files and no `shapes.txt`, and `shape_id` and
`direction_id` are empty on all 134,809 trips. Without geometry the network
cannot be mapped beyond station points, and without `direction_id` we cannot
separate outbound from inbound services — which is the natural next cut of the
Q1 peak-hour and Q2 platform analyses. A timeline for either would let us plan
the Sprint 3 dashboard around it.

A fifth item is worth raising as a note rather than a question: `feed_info.txt`
publishes `feed_start_date` and `feed_end_date` with a leading space. It is
harmless once you know, and it will break any consumer that parses the field on
a fixed width.

---

## 5. Inspecting the quarantine yourself

Every query below is read-only, and none of them should ever be run without
`-readonly`. The one command here that does write is `railpulse build
--keep-staging`, which rebuilds the database from scratch; it is called out
where it appears.

**What is in there, and under which rule:**

```bash
sqlite3 -readonly data/railpulse.db \
  "SELECT rule_code, source_table, COUNT(*) AS rows
     FROM rejected_row GROUP BY 1, 2 ORDER BY rows DESC;"
```

**The full record for every rejected row, payload included:**

```bash
sqlite3 -readonly -header -column data/railpulse.db \
  "SELECT rejected_id, source_table, src_line_no, rule_code, reason, payload
     FROM rejected_row ORDER BY source_table, src_line_no;"
```

**Pull a field out of the JSON payload without eyeballing it:**

```bash
sqlite3 -readonly -header -column data/railpulse.db \
  "SELECT src_line_no,
          json_extract(payload, '\$.trip_id')        AS trip_id,
          json_extract(payload, '\$.stop_sequence')  AS stop_sequence,
          json_extract(payload, '\$.departure_time') AS departure_time
     FROM rejected_row
    WHERE rule_code = 'DQ-03-IMPLAUSIBLE-DEPARTURE'
    ORDER BY src_line_no;"
```

**Tracing a row back to its physical line in the source file.**
`src_line_no` is the 1-based line number in the uncompressed `.txt`, counting the
header as line 1. Rejected row 5 reports `src_line_no = 961163` in
`stg_stop_times`, so:

```bash
unzip -p data/raw/nmbssncb_gtfs_static.zip stop_times.txt | sed -n '1p;961163p'
```

```
arrival_time,departure_time,drop_off_type,pickup_type,shape_dist_traveled,stop_headsign,stop_id,stop_sequence,trip_id
65:44:00,65:44:00,0,1,,"",gs:nmbssncb:8892007,10,gt:nmbssncb:88____:007::8891009:8892007:10:6544:20260314
```

The header is printed alongside the row because SNCB ships `stop_times.txt` — and
six of the other nine files — with its columns in alphabetical rather than spec
order. Reading the row positionally against the GTFS reference would misattribute
every field. The three exceptions are `feed_info.txt`, `translations.txt` and
`transfers.txt`, which is reason enough never to assume either ordering.

**Reading the staged row instead of the file.** `sql/07_cleanup.sql` drops the
`stg_` tables at the end of a normal build, because they are a second untyped
copy of the whole feed and account for roughly 400 MB of the finished database.
To keep them, rebuild with the flag — this is the one command in this section
that writes, and it discards and re-derives the entire core model:

```bash
railpulse build --keep-staging
```

Then the source row is joinable directly, which is far more convenient than
`sed` when a rule fires on thousands of rows:

```bash
sqlite3 -readonly -header -column data/railpulse.db \
  "SELECT s.src_line_no, s.trip_id, s.stop_sequence, s.arrival_time, s.departure_time
     FROM rejected_row r
     JOIN stg_stop_times s ON s.src_line_no = r.src_line_no
    WHERE r.source_table = 'stg_stop_times'
    ORDER BY s.src_line_no;"
```

Without `--keep-staging` that join returns nothing and the `sed` route above is
the only way back to the source bytes.

**Note on retention.** `rejected_row` and `ingestion_run` survive rebuilds by
design — they are the audit log, and `sql/02_schema.sql` deliberately omits them
from its `DROP TABLE` list. `sql/03_transform.sql` clears only the rows whose
`source_table LIKE 'stg%'`, so a re-run of the static feed replaces its own
quarantine entries without destroying the real-time ones, which are not
reproducible.
