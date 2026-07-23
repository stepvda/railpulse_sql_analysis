# RailPulse — Operational Performance Report

**Prepared for:** SNCB/NMBS, ahead of winter scheduling
**Prepared by:** RailPulse (urban mobility consulting)
**Data:** NMBS/SNCB GTFS Static, feed version `2026-07-20`, covering the
timetable from **2025-12-20 to 2026-12-12**
**Attribution:** NMBS/SNCB – Open Data – 2026-07-20 · licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

Every figure in this report is produced by a query in [`sql/analysis/`](../sql/analysis/).
The verbatim output of all 47 queries — with the SQL that produced each one — is
in [`analysis_results.md`](analysis_results.md), and the full result sets are in
[`output/*.csv`](../output/). Nothing here was computed in Python or a spreadsheet.

---

## Executive summary

| # | Question | Answer |
|---|---|---|
| 1 | Busiest hour on the network | **17:00–17:59**, 950,651 annual departures (6.51% of the network day). 07:00 is a close second at 938,525. |
| 2 | Busiest platforms at Bruxelles-Central | **Platforms 4, 3 and 2**, carrying 63,426 / 62,276 / 56,874 annual departures. |
| 3 | Busiest morning destinations | **Anvers-Central** (41,972 annual morning trips), **Louvain** (27,516), **Charleroi-Central** (21,328). |
| 4 | Service frequency mix | **45.24%** High Frequency, **34.65%** Medium, **20.11%** Low/Special — but the 45% carries **86.21%** of all operating days. |
| 5 | Amenity availability | **91.28%** of trips guarantee bike storage. The gap is entirely modal: **100%** of rail, **0%** of the 270 rail-replacement bus routes. `wheelchair_accessible` is **unpopulated for all 134,809 trips**. |

### The three things worth acting on

1. **Bruxelles-Central is the network's structural bottleneck, and it is not
   close.** It handles more annual departures than Bruxelles-Midi (311,324 vs
   283,415) across **6 platforms instead of 21**. That is 8,113 timetabled calls
   per platform against Midi's 2,085 — a **3.9× pressure differential**. On a
   typical operating day its busiest platform turns over **11.2 trains in its
   peak hour, one every 5.4 minutes**. Any winter resilience plan that treats
   the three Brussels stations as interchangeable is mis-specified.

2. **The evening peak is real and the timetable file hides it.** Counting rows
   in the timetable says the network peaks at 10:00. Counting departures that
   actually happen says 17:00. The difference is that off-peak and seasonal
   services are numerous but rare, while commuter services are few and run
   ~250 days a year. Hour 17 sits at rank 10 on the naive measure and rank 1 on
   the real one. **Capacity decisions taken from an unweighted timetable count
   would invest in the wrong hour of the day.**

3. **The accessibility data cannot support an accessibility statement.** Not
   because the network performs badly, but because the field is empty:
   `wheelchair_accessible` carries no value for any of the 134,809 trips, and
   `wheelchair_boarding` carries none for any of the 652 stations. This is a
   publishing gap, and it is cheap to fix relative to what it blocks.

---

## Method, in one section

Three decisions shape every number above. They are stated here rather than
buried, because a reader who disagrees with them should be able to find them.

**Departures are counted, not timetable rows.** This feed describes 358
individual dates, and each of its 134,809 trips carries its own calendar. A
trip that runs once and a trip that runs 250 times are one row each. Every
headline figure therefore weights each scheduled call by the number of days its
service actually operates ([`v_trip_service_days`](../sql/05_views.sql)). The
unweighted figure is reported alongside wherever the two disagree — and they
disagree materially in Q1, Q2 and Q3.

**Only boardable calls count.** 577,000 of the 2.17 million calls in this feed
are technical pass-throughs (`pickup_type = 1 AND drop_off_type = 1`): the train
serves the platform but no passenger may use it. Including them would inflate
every station in this report by roughly a quarter.

**"No information" is not "no".** GTFS uses code `0` for both "unstated" and,
carelessly read, "absent". Q5 counts only explicit guarantees (code `1`) and
reports silence as its own category.

Full reasoning: [`decisions.md`](decisions.md). Data caveats:
[`data_quality.md`](data_quality.md).

---

## Q1 — The peak hour problem

> *What hour of the day experiences the highest volume of scheduled train
> departures across the entire network?*

### Answer: 17:00–17:59

**950,651 annual departures**, 6.51% of the network's daily total.

| Rank | Hour | Annual departures | % of day | Timetable rows | Naive rank |
|---|---|---|---|---|---|
| 1 | 17:00 | 950,651 | 6.51% | 76,736 | 10 |
| 2 | 07:00 | 938,525 | 6.43% | 76,473 | 12 |
| 3 | 16:00 | 919,919 | 6.30% | 77,203 | 9 |
| 4 | 18:00 | 892,678 | 6.11% | 76,475 | 11 |
| 5 | 08:00 | 875,355 | 5.99% | 85,053 | 6 |

### Why the naive answer (10:00) is wrong

The unweighted count ranks 10:00 first with 94,323 timetable rows. But a call in
hour 10 runs on an average of **8.8 days**, while a call in hour 07 runs on
**12.3**. The midday hours are full of services that exist in the timetable and
seldom in reality.

| Hour | Annualised rank | Naive rank | Movement | Avg days per call |
|---|---|---|---|---|
| 17:00 | 1 | 10 | **+9** | 12.4 |
| 07:00 | 2 | 12 | **+10** | 12.3 |
| 16:00 | 3 | 9 | +6 | 11.9 |
| 10:00 | 8 | 1 | **−7** | 8.8 |
| 11:00 | 12 | 2 | **−10** | 9.0 |

The network has the twin-peaked profile of a commuter railway. The unweighted
count flattens it into a midday plateau that does not exist.

**Recommendation.** Use annualised departures for capacity planning. If a
timetable-row count must be used (it is far cheaper to compute), restrict it to
a single representative service date rather than the whole feed.

📄 [`q1_peak_hour.sql`](../sql/analysis/q1_peak_hour.sql) ·
📊 [`q1_annualised_departures_by_hour.csv`](../output/q1_annualised_departures_by_hour.csv)

---

## Q2 — Platform bottlenecks at Bruxelles-Central

> *Identify the top 3 busiest platforms in Brussels-Central.*

### Answer: platforms 4, 3 and 2

Station `gs:nmbssncb:S8813003`, six numbered platforms.

| Rank | Platform | Annual departures | % of station | Timetable rows | Routes | Destinations |
|---|---|---|---|---|---|---|
| 1 | **Platform 4** | 63,426 | 20.4% | 10,515 | 163 | 34 |
| 2 | **Platform 3** | 62,276 | 20.0% | 11,982 | 179 | 44 |
| 3 | **Platform 2** | 56,874 | 18.3% | 7,471 | 116 | 28 |
| 4 | Platform 1 | 52,561 | 16.9% | 6,781 | 108 | 28 |
| 5 | Platform 5 | 40,639 | 13.1% | 6,191 | 92 | 27 |
| 6 | Platform 6 | 35,548 | 11.4% | 5,740 | 89 | 26 |

**The top three are robust; their internal order is not.** Ranked by raw
timetable rows, platform 3 leads platform 4 (11,982 vs 10,515). Platform 4's
services simply run on more days. The same three platforms top both rankings, so
the answer to the question as asked is stable — but a report claiming "platform
4 is the single busiest" should say which measure it used.

A further **1,348 calls (2.69% of the station's traffic)** carry no platform
allocation at all in the feed. They are excluded from the ranking above rather
than being silently assigned.

### Where the bottleneck actually bites

| Platform | Busiest hour | Calls in that hour | Share of platform's day |
|---|---|---|---|
| Platform 3 | 09:00 | 791 | 6.6% |
| Platform 4 | 10:00 | 722 | 6.9% |
| Platform 2 | 22:00 | 487 | 6.5% |
| Platform 1 | 14:00 | 409 | 6.0% |

No platform concentrates more than 6.9% of its day into a single hour, which
means Bruxelles-Central's problem is **sustained load, not peak spikes**. The
station runs near capacity for most of the operating day, which is a harder
problem than a peak: there is no trough in which to absorb a disruption.

📄 [`q2_platform_bottlenecks.sql`](../sql/analysis/q2_platform_bottlenecks.sql)

---

## Q3 — Busiest morning destinations

> *Find the top 3 most frequent terminal destinations for all morning trips that
> depart before 12:00:00.*

"Morning trip" is applied to the **trip's origin**, not to each of its calls — a
service leaving Ostende at 06:04 is one morning trip, not eight.

### Answer: Anvers-Central, Louvain, Charleroi-Central

| Rank | Destination | Annual morning trips | Distinct services | Avg days per service |
|---|---|---|---|---|
| 1 | **Anvers-Central** | 41,972 | 3,930 | 10.7 |
| 2 | **Louvain** | 27,516 | 2,505 | 11.0 |
| 3 | **Charleroi-Central** | 21,328 | 2,505 | 8.5 |
| 4 | Bruxelles-Midi | 16,806 | 3,150 | 5.3 |
| 5 | Brussels Airport-Zaventem | 16,320 | 2,210 | 7.4 |

Bruxelles-Midi is 2nd by number of distinct services but only 4th by trips that
actually run — its morning services average 5.3 operating days each, half the
rate of Anvers-Central's. Anvers-Central leads on **both** measures, so it is the
one destination that can be stated without qualification.

### Which destinations are genuinely morning-driven

Ranking by volume finds destinations that are busy all day. Ranking by *morning
share* finds the commuter flows:

| Destination | Morning share | Annual morning trips |
|---|---|---|
| **Schaerbeek** | 65.8% | 6,727 |
| Eupen | 54.7% | 3,124 |
| Blankenberge | 51.7% | 3,704 |
| Wavre | 49.9% | 2,773 |

Schaerbeek is the network's most morning-skewed destination by a wide margin —
two thirds of its arrivals originate before noon. It is a depot and
maintenance hub as well as a passenger station, which is consistent with a
strong morning positioning flow.

📄 [`q3_morning_destinations.sql`](../sql/analysis/q3_morning_destinations.sql)

---

## Q4 — Service frequency

> *Classify each active service ID into a weekly frequency category. 5+ days →
> High; 2–4 → Medium; 1 or irregular → Low/Special. Show the percentage in each.*

### A data problem had to be solved first

`calendar.txt` is the file that should answer this. In this feed **all seven
weekday flags are `0` for all 51,593 services** — the entire operating pattern
is expressed through `calendar_dates.txt` (4,697,139 explicit dates, every one
`exception_type = 1`). Running the textbook `monday + tuesday + … >= 5` query
here classifies 100% of the network as "Low Frequency/Special".

The weekly rhythm is therefore **derived**: for each service, the modal number
of operating days across the weeks in which it runs at all
([`v_service_frequency`](../sql/05_views.sql)).

### Answer

| Class | Services | % of services | Operating days | % of operating days |
|---|---|---|---|---|
| **High Frequency** (5+ d/wk) | 23,340 | **45.24%** | 4,049,433 | **86.21%** |
| **Medium Frequency** (2–4 d/wk) | 17,877 | **34.65%** | 553,302 | 11.78% |
| **Low Frequency/Special** (1 d/wk) | 10,376 | **20.11%** | 94,404 | 2.01% |

**The two percentage columns tell different stories, and the second is the
important one.** 45% of service calendars produce 86% of all operating days.
The 20% classified Low/Special account for 2% of the railway. A winter
timetable reduction that targets "the 20% of low-frequency services" would
remove one fiftieth of actual service — and a reduction that touches the High
Frequency tier is nine times more consequential per service than it looks.

Underlying distribution: the two spikes are at **5 days** (16,541 services —
Monday-to-Friday commuter) and **7 days** (6,420 — daily), with a third at
**2 days** (14,215 — weekend-only).

### How much the definition matters

"Operates 5 days a week" has more than one defensible reading. All three were
computed:

| Definition | High | Medium | Low/Special |
|---|---|---|---|
| **A. Modal days per active week** (used) | 45.24% | 34.65% | 20.11% |
| B. Distinct weekdays ever touched | 62.26% | 31.52% | 6.22% |
| C. Busiest single week | 52.10% | 36.38% | 11.52% |

The headline "High Frequency" share moves by **17 percentage points** depending
on the definition. Definition A was chosen because it describes a service's
*normal* week and is unmoved by one unusual one; B over-counts a service that
ran Monday-to-Friday exactly once. This spread is published rather than hidden,
because a single number here would imply a precision the question does not have.

📄 [`q4_service_frequency.sql`](../sql/analysis/q4_service_frequency.sql)

---

## Q5 — The accessibility audit

> *Calculate the exact ratio and percentage of scheduled trips per route that
> explicitly guarantee wheelchair accessibility or bicycle storage. Which routes
> score the lowest?*

### Answer

| Amenity | Ratio | % guaranteed | Explicitly refused | No information |
|---|---|---|---|---|
| Bicycle storage | 123,051 / 134,809 | **91.28%** | 0 | 11,758 |
| Wheelchair accessibility | 0 / 134,809 | **0.00%** | 0 | **134,809** |

### Finding 1 — the bicycle gap is a mode gap, not a route gap

| Mode | Routes | Trips | Bikes guaranteed | % |
|---|---|---|---|---|
| Rail (`route_type` 2) | 1,531 | 123,051 | 123,051 | **100.00%** |
| Bus (`route_type` 3) | 270 | 11,758 | 0 | **0.00%** |

There is nothing in between. Every single rail trip guarantees bike storage;
not one rail-replacement bus trip does. All **270** zero-scoring routes are
replacement buses.

This changes the recommendation completely. A scattered set of underperforming
routes would call for route-by-route investigation. A perfect modal split calls
for **one procurement decision**: bicycle capacity in the replacement-bus
contract. Ranked by passenger exposure, the corridors to address first are:

| Priority | Route | Annual trips |
|---|---|---|
| 1 | BUS Luxembourg (LU) — Arlon | 4,989 |
| 2 | BUS Bruxelles-Midi — Nivelles | 2,822 |
| 3 | BUS Hasselt — Kiewit | 1,824 |
| 4 | BUS Ottignies — Fleurus | 1,432 |
| 5 | BUS Bruges — Gand-Saint-Pierre | 1,246 |

### Finding 2 — wheelchair accessibility cannot be assessed from this feed

`trips.wheelchair_accessible` is empty for **all 134,809 trips**.
`stops.wheelchair_boarding` is empty for **all 652 stations**. Both fields exist
in the feed; neither carries a value.

This is stated as a **data gap, not a performance finding**. It would be wrong
to report 0% wheelchair accessibility: GTFS code `0` means "no information", and
SNCB demonstrably operates assisted-boarding services that this feed does not
describe. The correct conclusion is that **no automated journey planner,
accessibility app or regulator using this feed can answer a wheelchair question
about SNCB at all** — which is a more serious problem than a low score, and a
cheaper one to fix.

**Recommendation.** Populate `wheelchair_accessible` at trip level and
`wheelchair_boarding` at station level. Both are single-digit-cardinality fields
already modelled in the GTFS the operator publishes.

📄 [`q5_accessibility_audit.sql`](../sql/analysis/q5_accessibility_audit.sql)

---

## Nice-to-have: the hub leaderboard

### Structural comparison

| Rank | Station | Annual departures | Platforms | Calls per platform | Routes | Destinations |
|---|---|---|---|---|---|---|
| 1 | Bruxelles-Central | 311,324 | **6** | **8,113** | 337 | 101 |
| 2 | Bruxelles-Nord | 309,765 | 13 | 3,712 | 350 | 102 |
| 3 | Bruxelles-Midi | 283,415 | 21 | 2,085 | 354 | 103 |
| 4 | Gand-Saint-Pierre | 149,864 | 11 | 1,713 | 206 | 58 |
| 5 | Anvers-Central | 131,812 | 16 | 825 | 125 | 61 |

The three Brussels stations carry near-identical annual volumes over platform
counts that differ by a factor of 3.5. **Bruxelles-Central absorbs the highest
load on the fewest platforms in the country.**

On a composite of connectivity, platform headroom and load smoothness,
**Bruxelles-Midi ranks first (74.7)** and Bruxelles-Central last of the five
(43.7) — it matches Midi on connectivity (95.6 vs 100) while scoring 0 on
headroom.

### Punctuality (live data)

Real-time GTFS-RT trip updates are polled and appended by
[`scripts/poll_realtime.sh`](../scripts/poll_realtime.sh). On-time is defined as
a departure delay under 120 seconds; cancellations are counted separately rather
than folded in as punctual.

The observation window captured for this report is **12 trip-update snapshots
and 10 alert snapshots over roughly nine minutes** (2026-07-23 09:17–09:26 UTC):
19,723 raw stop-time updates, resolving to 1,758 distinct observed calls, of
which **563 carry an actual delay reading**. That is a demonstration of the
mechanism, not a settled ranking — read it accordingly.

| Rank | Station | Observed | On time | Avg delay | Worst |
|---|---|---|---|---|---|
| 1 | Bruxelles-Midi | 11 | 90.9% | 44 s | 120 s |
| 2 | Bruxelles-Central | 17 | 82.4% | 60 s | 240 s |
| 3 | Gand-Saint-Pierre | 7 | 71.4% | 94 s | 480 s |
| 4 | Bruxelles-Nord | 17 | 70.6% | 64 s | 240 s |
| 5 | Anvers-Central | 11 | 63.6% | 76 s | 180 s |

Delay distribution across all 626 observed non-cancelled departures:
**69.9% on time** (<2 min), 17.9% delayed 2–5 minutes, 2.1% delayed 5–15
minutes, 0.6% beyond 15 minutes, and 9.5% carrying no delay reading at all.

**Do not draw conclusions from this table yet.** Seven to seventeen observations
per station is an anecdote: an earlier run of the same pipeline, over a
different nine-minute window, ranked Bruxelles-Central *last* at 64.7% rather
than second at 82.4%. The station ordering is not stable at this sample size and
saying otherwise would be the exact error this report criticises elsewhere.

What the exercise *does* establish is that the mechanism works end to end:
**100% of observed real-time trips resolved against the static timetable**, so
the join between live operations and the Sprint 1 model is sound, and the
delay/cancellation split is being captured correctly. Leave
`scripts/poll_realtime.sh` running on a 15-minute cron for a week and this table
becomes a real punctuality leaderboard — that accumulation is Sprint 2's job.

📄 [`q6_network_leaderboard.sql`](../sql/analysis/q6_network_leaderboard.sql)

---

## Nice-to-have: index optimisation

Measured on the full 2,165,507-row fact table, best of three runs after a
warm-up. Reproduce with `make benchmark`.

**Cost of a SARGable violation** — same answer, computed two ways:

| Query | Indexed / materialised | Function-wrapped | Penalty |
|---|---|---|---|
| Q1 hourly histogram | 0.08 s | 9.04 s | **~100×** |
| Q2 single-platform lookup | <0.01 s | 0.20 s | **~570×** |
| Q4 weekday counts (4.7M rows) | 1.43 s | 2.92 s | ~2× |

**Value of each index** — timed, then with the index dropped, then restored:

| Query | With index | Without | Speed-up |
|---|---|---|---|
| Q1 hourly histogram | 0.10 s | 0.36 s | 3.7× |
| Q2 platform counts at one station | 0.004 s | 5.95 s | **1,564×** |
| Q5 amenity ratio per route | 0.011 s | 0.038 s | 3.6× |

Q1 and Q4 get faster for **different reasons**, and the distinction matters. Q1
is an index effect: `ix_stop_time_boardable_hour` turns a scan plus a temporary
B-tree into a covering seek. Q4 is not — both plans scan `service_date` either
way, because no index is defined on `day_of_week`. Its 2× is purely the cost of
*not* calling `strftime()` 4,697,139 times, and no query plan shows it. Plans
describe access paths; only a stopwatch describes per-row work.

📄 [`q7_index_optimisation.sql`](../sql/analysis/q7_index_optimisation.sql)

---

## What we would ask the publisher

1. **Populate `wheelchair_accessible` and `wheelchair_boarding.`** Currently
   empty across the entire feed, blocking every accessibility use case.
2. **Populate the `calendar.txt` weekday flags,** or state in the feed
   documentation that they are intentionally unused. Every consumer currently
   has to reverse-engineer the weekly pattern from 4.7 million exception rows.
3. **Confirm the 12 calls published at 48:00:00 or later** (up to `87:16:00`).
   These are quarantined here as implausible; if they are intentional, the
   semantics should be documented.
4. **Set `bikes_allowed` explicitly on replacement-bus trips** — `0` currently
   means "no information", which is indistinguishable from an unset field. If
   the answer is genuinely no, `2` says so.

---

## Reproducing this report

```bash
make setup          # install requests + python-dotenv
cp .env.example .env && $EDITOR .env    # add your BMC_API_KEY
make all            # fetch -> build -> verify -> analyse   (~3 minutes)
make dashboard      # interactive version
```

Every table above regenerates from
[`docs/analysis_results.md`](analysis_results.md) and
[`output/*.csv`](../output/).
