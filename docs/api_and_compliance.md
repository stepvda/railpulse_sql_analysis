# API access, rate limits and licence compliance

The challenge brief carries one explicit warning:

> Be mindful of the request limits so you don't get blocked! Read the
> documentation for the usage of the API to make sure your compliant.

(quoted verbatim from `project-instructions/01-sql-rail-db/README.md`, typo and
all)

This document is the evidence that the documentation was read. It records what
the source actually publishes, what key was obtained and how, the arithmetic
that shows this project fits inside the published quota with room to spare, and
the licence obligations that come with the data.

Everything stated here was confirmed either against the portal's own pages on
2026-07-23 or against files in this repository. Where a detail could not be
confirmed it is marked as such rather than filled in with a plausible guess —
see the [verification log](#8-verification-log) at the end.

---

## 1. The data source

The data comes from the **Belgian Mobility Open Data Portal**, run by the
Belgian Mobility Company (BMC), at <https://data.belgianmobility.io>. It is a
single gateway in front of the four public transport operators that between
them cover the whole country:

| Operator | Network | Region |
| --- | --- | --- |
| NMBS-SNCB | National railway | Belgium |
| De Lijn | Bus and tram | Flanders |
| LETEC (TEC) | Bus | Wallonia |
| STIB-MIVB | Metro, tram and bus | Brussels |

This matters for the schema: RailPulse ingests **NMBS-SNCB only**, but because
all four operators publish the same GTFS shape through the same gateway, the
core model in `sql/02_schema.sql` is operator-agnostic. Pointing
`RAILPULSE_OPERATOR` at another slug is intended to be the only change needed to
load a different network. That has not been tried, and the other three slugs are
guesses — `config.py`'s own comment writes the Walloon operator as `tec` where
the table below guesses `letec`, which is exactly the kind of detail that would
have to be confirmed against the portal before the claim is worth anything.

### 1.1 Endpoint table

The gateway host is `api-management-discovery-production.azure-api.net`. GTFS
paths follow the pattern `/api/gtfs/feed/{operator}/{static|rt/<feed>}`.

**GTFS Static** — ZIP, regenerated daily.

| Operator | URL | Status |
| --- | --- | --- |
| NMBS-SNCB | `https://api-management-discovery-production.azure-api.net/api/gtfs/feed/nmbssncb/static` | Verified — this is the feed this project loads |
| De Lijn | `.../api/gtfs/feed/delijn/static` | Slug inferred from the path pattern; not called by this project |
| LETEC | `.../api/gtfs/feed/letec/static` | Slug inferred; not called |
| STIB-MIVB | `.../api/gtfs/feed/stibmivb/static` | Slug inferred; not called |

**GTFS Realtime** — documented as Protocol Buffers, served as JSON (see
[section 5](#5-the-gtfs-rt-format-surprise)), refreshed every 30 seconds.

| Operator | Feeds published | URL |
| --- | --- | --- |
| NMBS-SNCB | 2 — trip updates, service alerts | `.../api/gtfs/feed/nmbssncb/rt/trip-update`<br>`.../api/gtfs/feed/nmbssncb/rt/alert` (both verified) |
| De Lijn | 2 — trip updates, service alerts | Same two paths with `delijn` (inferred) |
| LETEC | 3 — trip updates, service alerts, vehicle positions | Same two paths with `letec` (inferred). **The vehicle-position path is not published on the public data page and has not been called from here, so it is deliberately left blank rather than guessed.** |
| STIB-MIVB | none | STIB-MIVB publishes no GTFS-RT card at all; its live data is the JSON dataset API below |

**STIB-MIVB JSON datasets** — the only endpoints the portal prints in full, so
these five are quoted verbatim:

| Dataset | URL | Cadence |
| --- | --- | --- |
| Waiting Time | `https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/rt/WaitingTimes` | Real-time |
| Stop Details | `https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/static/StopDetails` | Real-time |
| Stops By Line | `https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/static/stopsByLine` | Real-time |
| Travellers Information | `https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/rt/TravellersInformation` | Real-time |
| Vehicle Positions | `https://api-management-discovery-production.azure-api.net/api/datasets/stibmivb/rt/VehiclePositions` | 15 seconds |

**NeTEx EPIP** — all four operators publish a daily NeTEx EPIP export (XML).
The portal exposes these behind JavaScript-driven Download buttons and does not
print the target URLs, so no URL is given here. NeTEx is a richer European
exchange format than GTFS; it is listed for completeness only, as RailPulse has
no use for it.

STIB-MIVB additionally publishes a weekly ShapeFile, INSPIRE Roads and INSPIRE
Rails. Not used here.

### 1.2 Update cadence

| Product | Cadence (as published) | What that means for us |
| --- | --- | --- |
| GTFS Static | Daily | One download per day is the maximum useful rate. A second run the same day should be answered 304 — see [3.2](#32-conditional-get) |
| GTFS Realtime | 30 seconds | Polling faster than 30 s cannot return new information |
| NeTEx EPIP | Daily | Not used |
| STIB JSON | Real-time / 15 s | Not used |

### 1.3 What this project actually downloads

| Artefact | Size | Requests |
| --- | --- | --- |
| `data/raw/nmbssncb_gtfs_static.zip` | 26 283 845 bytes (26.3 MB) | 1 |
| One real-time poll (`data/raw/sample_trip_update.json` + `sample_alert.json`) | 187 880 + 55 663 = 243 543 bytes | 2 |

That single 26 MB static request yields the entire national timetable: 652
stations, 1 801 routes, 134 809 trips and 2 165 507 timetabled calls covering
2025-12-20 to 2026-12-12. The efficiency of that trade is the whole argument of
[section 7](#7-a-note-on-irail).

---

## 2. Registering and getting a subscription key

The portal is Microsoft Azure API Management. Documentation and sign-up live on
the developer portal, at a different host from the gateway itself:

* Developer portal: <https://api-management-opendata-production.developer.azure-api.net>
* Sign up: `.../signup` · Sign in: `.../signin`

The API list page (`.../apis`) states plainly: *"As a guest (unregistered) user,
you can browse the documentation of all our APIs, but to use them you will need
a valid subscription key."* In practice the GTFS endpoints do answer without one, at
the anonymous quota — but that is not the intended mode and should not be
relied on.

### 2.1 The flow that was actually performed for this project

1. **Sign up** at `.../signup` with an email address and password, and confirm
   the address from the activation mail.
2. Sign in, then open the **Profile** menu.
3. Go to **Products** and select **Standard**.
4. Give the subscription a name (any label; it is only a handle for the key).
5. Press **Subscribe**.
6. The **Subscriptions** table on the Profile page then shows the subscription
   with a **Primary key** and a **Secondary key**, both revealed by a "Show"
   toggle and both immediately valid.

Two keys are issued so that a key can be rotated without an outage: put the
secondary key into service, regenerate the primary, then swap back.

**No key, primary or secondary, appears anywhere in this repository.** The key
is read from the environment variable `BMC_API_KEY`, which `src/railpulse/config.py`
loads from a `.env` that `.gitignore` excludes on its second line. `.env.example`
is the template kept in the repo and ships with the field empty. The portal's
terms are explicit that a key is
*"personal and non-transferable"* and *"may only be used by the registered User
to whom it was assigned"*, so sharing one in a public repository is both a
security mistake and a terms violation.

### 2.2 Automating steps 2-6

`.env.example` reserves `BMC_EMAIL` and `BMC_PASSWORD` for a
`scripts/setup_api_key.py` that would drive the same browser flow with
Playwright for an account that already exists and print the resulting primary
key. **That script is not in the repository as of 2026-07-23** — only the
environment variables and the commented-out `playwright>=1.44` line in
`requirements.txt` exist. The subscription for this project was created by hand
in a browser, which is entirely equivalent; the automation is a convenience
that was never needed, and nothing in the pipeline depends on it.

### 2.3 Tiers

| Tier | Registration required | Daily quota | Per-minute quota | Intended users (portal's wording) |
| --- | --- | --- | --- | --- |
| Anonymous | No | 100 requests | 10 | General public, testing |
| Standard | Yes | 12 000 requests | 500 | Developers, NGOs, startups |
| Gold | Yes, plus an SLA | On request | On request | High-volume platforms |

The terms head that column *"Usage Quota (indicative)"*, so the numbers are the
published intent rather than a contractual guarantee. The arithmetic below is
therefore sized to sit well under them, not to graze them.

Standard needs only a sign-up form; the terms name no fee for it, and reserve
the right to charge only at the top tier — *"BMC reserves the right to charge
fees for Gold tier access where high-volume use causes substantial
infrastructure costs."* Gold is not self-service: the terms say users *"start
with Standard, can request Gold via service agreement"*, and separately that
*"The Anonymous and Standard Access Tiers are not covered by any Service Level
Agreement."* No dedicated route for a Gold request is printed anywhere on the
portal — `/en/contact.html` gives only the general address
**opendata@belgianmobilitycompany.be**, which is the obvious starting point but
is not stated to be the Gold channel.

RailPulse needs Standard. Section 3 shows why it does not come close to needing
Gold.

---

## 3. Rate limits and how this project complies

All of the mechanisms below live in `src/railpulse/api_client.py`, which is the
only module in the project that touches the network. Compliance is enforced in
one place rather than trusted to every caller.

### 3.1 The controls, and the line of code each corresponds to

| Control | Where | What it does |
| --- | --- | --- |
| Minimum-interval gate | `RateLimiter.wait()`, constructed as `RateLimiter(60.0 / max(rpm, 1))` | A process-wide gate that sleeps until at least `60 / MAX_REQUESTS_PER_MINUTE` seconds have passed since the previous call. With the default `RAILPULSE_MAX_RPM=10` that is 6.0 s, so the per-minute ceiling is approached rather than discovered by collecting a 429. The gate is per *process*, so two concurrent pollers would not see each other — which is one more reason to run a single scheduled job rather than several. |
| Anonymous-safe default | `config.MAX_REQUESTS_PER_MINUTE = 10` | The client throttles itself to the *anonymous* limit even when a Standard key is present. It is therefore safe if the key is missing, expired or mistyped. |
| Bounded retry | `_request()`, `max_retries = 4` | Four attempts, then `RuntimeError`. A client that retries forever is indistinguishable from an attack. |
| `Retry-After` honoured | `_parse_retry_after()` | When the server says how long to wait, that value wins over our own backoff. Both delta-seconds and HTTP-date forms are handled. |
| Retry only what is retryable | `RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}` | Any other 4xx means the request itself is wrong. Repeating it would not fix it, and would burn quota. |
| Exponential backoff with a cap | `min(2 ** attempt, 60)` | Used when no `Retry-After` is supplied: 2 s, 4 s, 8 s, capped at 60 s. |
| Conditional GET | `download_gtfs_static(if_modified_since=...)` | See 3.2. |
| Descriptive `User-Agent` | `config.USER_AGENT` | `RailPulse/1.0 (BeCode data-engineering exercise; contact via GitHub issues)`. An operator seeing unusual traffic can identify who is making it and why. |
| Atomic download | `.part` file, renamed on success | An interrupted transfer can never be mistaken for a complete feed, which would otherwise cause a needless re-download or, worse, a silently truncated load. |
| Idempotent real-time writes | `rt_snapshot UNIQUE (feed, feed_timestamp_epoch)` | An overlapping or too-frequent poll returns a payload whose header timestamp has not moved; it is skipped rather than double-counted. |

### 3.2 Conditional GET

The static feed is 26 283 845 bytes and is regenerated once a day. Re-running
the pipeline the same afternoon has nothing new to fetch.

`ingest_static.fetch_static_feed()` records the upstream `Last-Modified` header
(`data/raw/nmbssncb_gtfs_static.last_modified`, currently
`Thu, 23 Jul 2026 03:05:09 GMT`) and sends it back as `If-Modified-Since` on the
next run. If the server answers **304 Not Modified**, `download_gtfs_static()`
returns `not_modified=True` without writing, and the cached zip is reused.

Only one static download has been made from this machine, so a 304 from this
gateway has not actually been observed — the client-side half is implemented
and the header is being sent, but whether the gateway honours
`If-Modified-Since` is an expectation, not a measurement.

A 304 would still count as one request against the quota — a 304 is not free —
but it costs a few hundred bytes of headers instead of 26.3 MB of payload, and
it makes a rebuild loop cheap enough that there is no temptation to work around
the rate limit.

### 3.3 The arithmetic

**A full build costs exactly one request.** `railpulse build`
(`python -m railpulse.build`) calls `fetch_static_feed()`, which calls
`download_gtfs_static()` once; everything after that — unzip, stage, transform,
index, view creation — is local. There is no per-station, per-route or
per-trip fetching anywhere in the pipeline. `railpulse build --offline` costs
none at all, and is how a rebuild against the zip already on disk should be run.

**Real-time polling costs two requests per poll** (`trip-update` and `alert`),
separated by the 6-second gate.

| Poll interval | Polls/day | RT requests/day | Plus one static | % of anonymous quota (100) | % of Standard quota (12 000) | Bandwidth/day |
| --- | --- | --- | --- | --- | --- | --- |
| every 5 min | 288 | 576 | 577 | 577% — blocked | 4.8% | 70.1 MB |
| **every 15 min** | 96 | 192 | **193** | 193% — blocked | **1.6%** | 23.4 MB |
| every 30 min | 48 | 96 | 97 | 97% | 0.8% | 11.7 MB |
| **every 45 min** | 32 | 64 | **65** | **65%** | 0.5% | 7.8 MB |
| every 60 min | 24 | 48 | 49 | 49% | 0.4% | 5.8 MB |

Bandwidth is `polls × 243 543 bytes`, using the two captured sample payloads as
the per-poll size.

**Recommended interval — the two rows in bold: every 15 minutes with a Standard
key, every 45 minutes without one.**

* With Standard, 15-minute polling costs 193 of 12 000 daily requests (1.6%)
  and matches the Sprint 2 brief's "every 15 to 30 minutes" timer trigger. It
  is the finest resolution worth having without paying real infrastructure cost
  to someone else.
* Without a key, 30-minute polling is arithmetically inside the 100/day ceiling
  at 97 requests — but only just. A single failed fetch can consume up to four
  attempts, so three spare requests is not enough headroom. 45 minutes leaves
  35 requests of slack, which absorbs a bad-network day and still yields 32
  snapshots.
* Polling faster than 30 seconds is pointless in any tier: the feed itself only
  refreshes every 30 seconds, and `rt_snapshot`'s uniqueness constraint would
  discard the duplicates anyway.

`scripts/poll_realtime.sh` ships with a `*/15 * * * *` crontab line. That is the
Standard-key recommendation, and an anonymous caller must change it, because
15-minute polling is 192 real-time requests a day against a ceiling of 100.

Note that the script's own header comment puts 15-minute polling at "96
requests/day … just inside the anonymous ceiling". That arithmetic counts polls,
not requests: each poll is two requests, so the true figure is 192. The comment
in the script is wrong; this table is right.

**Cron cannot express a 45-minute cadence with a step field.** `*/45` in the
minute column expands to minutes {0, 45} within each hour, so it fires twice an
hour — 48 runs a day, exactly the same as `*/30`, not the 32 the table gives for
a true 45-minute interval. A real 45-minute cadence needs the three-hour cycle
written out:

```cron
0,45 0,3,6,9,12,15,18,21 * * *
30   1,4,7,10,13,16,19,22 * * *
15   2,5,8,11,14,17,20,23 * * *
```

That is 32 runs a day and 65 requests including the static build. If three cron
lines are more machinery than the job deserves, the two honest single-line
alternatives are `0 * * * *` (hourly, 49 requests/day) or the launchd job in
[3.4](#34-launchd-job-macos), whose `StartInterval` takes a plain number of
seconds and therefore expresses 45 minutes directly.

### 3.4 launchd job (macOS)

`scripts/poll_realtime.sh` documents the cron form and points here for the
plist. On macOS launchd is the better fit for two specific reasons: a
`StartInterval` is a plain number of seconds, so it can express the 45-minute
anonymous cadence that cron cannot (see 3.3), and launchd runs a job that fell
due while the machine was asleep, where cron simply skips it. Both survive a
reboot — that is not the difference.

Create the log directory first (`mkdir -p data/logs`): launchd opens
`StandardOutPath` when it spawns the job, before `poll_realtime.sh` gets to run
its own `mkdir -p`, so on a fresh checkout the first run's output is otherwise
lost. Then save the file as
`~/Library/LaunchAgents/io.railpulse.poll.plist`, replacing the paths, and
`launchctl load ~/Library/LaunchAgents/io.railpulse.poll.plist`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>io.railpulse.poll</string>

    <key>ProgramArguments</key>
    <array>
        <string>/full/path/to/railpulse_sql_analysis/scripts/poll_realtime.sh</string>
    </array>

    <!-- 2700 s = 45 min, the anonymous-safe cadence: 32 polls = 64 real-time
         requests a day, 65 counting the daily static build, against a ceiling
         of 100. Cron cannot express this interval; see 3.3.
         Set 900 (15 min) once BMC_API_KEY holds a Standard key. -->
    <key>StartInterval</key>
    <integer>2700</integer>

    <!-- Run once at load so a fresh install produces a snapshot immediately
         instead of waiting out the first interval. -->
    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/full/path/to/railpulse_sql_analysis/data/logs/launchd.out.log</string>
    <key>StandardErrorPath</key>
    <string>/full/path/to/railpulse_sql_analysis/data/logs/launchd.err.log</string>
</dict>
</plist>
```

launchd does not read a login shell profile, so `BMC_API_KEY` must reach the
process another way. It does not need to be exported: `config.py` calls
`load_dotenv(PROJECT_ROOT / ".env")` with `PROJECT_ROOT` derived from the
package's own `__file__`, so `.env` is found by path regardless of the working
directory or the environment launchd supplies. The `cd "$PROJECT_ROOT"` in
`poll_realtime.sh` is belt-and-braces for the log path, not what makes the key
resolve.

---

## 4. Authentication mechanics

The key travels in an HTTP header, never in the query string — a query string
ends up in server logs, browser history and shell history:

```
Ocp-Apim-Subscription-Key: <your key>
```

That header name is Azure API Management's convention, not a BMC invention;
`config.API_KEY_HEADER` holds it and `BelgianMobilityClient.__init__` attaches
it to the `requests.Session` once, so no individual call site can forget it.

### 4.1 What the failure modes look like

| Status | Meaning | Typical Azure APIM body | What the client does |
| --- | --- | --- | --- |
| **401 Unauthorized** | Key missing, mistyped, revoked, or not subscribed to the product that fronts this API | `{"statusCode": 401, "message": "Access denied due to missing subscription key. Make sure to include subscription key when making requests to an API."}` or `"... due to invalid subscription key."` | **Not retried.** 401 is not in `RETRYABLE_STATUSES`; the request is wrong and repeating it is abuse. `raise_for_status()` surfaces it. |
| **429 Too Many Requests** | Per-minute or per-day quota exceeded | `{"statusCode": 429, "message": "Rate limit is exceeded. Try again in NN seconds."}`, normally with a `Retry-After` header | Four attempts in all — three retries — waiting the server's `Retry-After` when present, otherwise 2/4/8 s capped at 60 s, then `RuntimeError`. |
| **304 Not Modified** | `If-Modified-Since` matched | empty | Treated as success; the cached zip is reused. |

The exact 401 and 429 message strings above are the standard Azure API
Management responses. This project has not deliberately triggered either
against the live gateway — it has never been rate-limited — so the wording is
quoted from the Azure APIM convention rather than from an observed response.
The portal's own terms confirm the status code: *"Bandwidth and rate limits may
be applied at any time to ensure system stability. If limits are exceeded, HTTP
429 (Too Many Requests) errors may be returned."*

### 4.2 Anonymous access

The GTFS endpoints answer without any `Ocp-Apim-Subscription-Key` header at
all — verified during this project — but at 100 requests/day and 10/minute
instead of 12 000 and 500. `BelgianMobilityClient.describe_auth()` prints which
mode a run is in at start-up, so an unauthenticated run is visible in the log
rather than discovered later as an unexplained 429:

```
ANONYMOUS — no BMC_API_KEY set. The feeds still answer, but you are
capped at 100 requests/day and 10/minute. See .env.example.
```

Because the client throttles to the anonymous per-minute limit regardless, an
accidentally anonymous run degrades in quota rather than breaking.

---

## 5. The GTFS-RT format surprise

The portal lists the real-time products with **Format: Protocol Buffers**. The
gateway actually returns the **JSON encoding of the same GTFS-Realtime
message**. Both sample payloads in `data/raw/` parse with `json.loads()` and
contain no protobuf framing, and every live poll has been decoded with
`response.json()` without a single failure. The response `Content-Type` is not
recorded anywhere in the repository, so the evidence here is the payload itself,
not the header.

This is the single most consequential discovery in the ingestion layer. Had the
feed been protobuf, decoding it would have required the
`gtfs-realtime-bindings` package (and transitively `protobuf`). Because it is
JSON, `BelgianMobilityClient.fetch_realtime()` decodes it with `response.json()`
from `requests` and nothing else, and `src/railpulse/ingest_realtime.py` shreds
the resulting dict straight into the `rt_` tables — which keeps the project
inside the brief's "Python must
*only* be used for the network `requests` and executing raw SQL via `sqlite3`"
constraint without an argument about what counts as a data-frame engine.

### 5.1 A trip update, trimmed

From `data/raw/sample_trip_update.json`, captured 2026-07-23 07:59:17 UTC. The
real payload is 187 880 bytes: 113 entities, 1 482 stop-time updates. The first
entity is shown, with 2 of its 16 stop-time updates, in the order the payload
gives them. Values are verbatim; nothing has been rewritten, only omitted.

```json
{
  "header": {
    "gtfsRealtimeVersion": "1.0",
    "incrementality": 0,
    "timestamp": 1784793557
  },
  "entity": [
    {
      "id": "rt:nmbssncb:88____:007::8200100:8814001:16:1028:20260807",
      "tripUpdate": {
        "trip": {
          "tripId": "gt:nmbssncb:88____:007::8200100:8814001:16:1028:20260807",
          "startTime": "07:11:00",
          "startDate": "20260723",
          "scheduleRelationship": 0
        },
        "stopTimeUpdate": [
          {
            "stopId": "gs:nmbssncb:8200100",
            "scheduleRelationship": 2,
            "stopSequence": 1
          },
          {
            "arrival":   { "time": 1784784600, "delay": 60 },
            "departure": { "time": 1784784960, "delay": 0  },
            "stopId": "gs:nmbssncb:8866001_3",
            "scheduleRelationship": 0,
            "stopSequence": 3
          }
        ],
        "timestamp": { "low": 1784793545, "high": 0, "unsigned": true }
      }
    }
  ]
}
```

Note the first stop-time update: `stopId`, `scheduleRelationship` and
`stopSequence` only, no times. Exactly 957 of the 1 482 stop updates in the
sample carry those three keys and nothing else, and all 957 are the ones with
`scheduleRelationship: 2` — the two sets coincide exactly. At *stop* level,
`scheduleRelationship: 2` is `NO_DATA` — "no real-time data is given for this
stop" — not a cancellation. The same integer on the *trip* descriptor means
something else entirely; the two enumerations are separate in the
[GTFS-RT reference](https://gtfs.org/documentation/realtime/reference/). Reading
a 2 as "cancelled" would report roughly two thirds of the network as cancelled.

Only 524 stop updates carry a prediction (`scheduleRelationship: 0`), all 524 of
them with at least one time, and one is `1` (`SKIPPED`). Across the 946 delay
values present, observed delays run from 0 to 1 080 seconds (18 minutes).

### 5.2 A service alert, trimmed

From `data/raw/sample_alert.json`, captured 2026-07-23 07:59:36 UTC — 55 663
bytes, 21 entities. The first entity is shown. Every text block in the real
payload carries all four languages; `nl` and `de` are dropped from `headerText`
here, and `fr`, `nl` and `de` from `descriptionText` and `url`, purely to keep
the excerpt readable.

```json
{
  "header": {
    "gtfsRealtimeVersion": "2.0",
    "timestamp": 1784793576
  },
  "entity": [
    {
      "id": "rs:nmbssncb:1c07417c",
      "alert": {
        "informedEntity": [ { "agencyId": "nmbssncb" } ],
        "activePeriod": [],
        "cause": 1,
        "effect": 3,
        "headerText": {
          "translation": [
            { "language": "fr", "text": "Rivage - Aywaille : Retards et suppressions" },
            { "language": "en", "text": "Rivage - Aywaille: Delays and cancellations" }
          ]
        },
        "descriptionText": {
          "translation": [
            { "language": "en", "text": "Delays and cancellations are possible. Disruption for an undetermined amount of time. We are awaiting information by a technical team. ... Cause: defective train" }
          ]
        },
        "url": {
          "translation": [
            { "language": "en", "text": "http://www.belgianrail.be/jp/nmbs-realtime/help.exe/en?tpl=showmap_external&..." }
          ]
        }
      }
    }
  ]
}
```

Alerts are multilingual (fr, nl, de, en), which is why `rt_alert_text` is a
child table keyed by language rather than four columns on `rt_alert`. In this
sample every one of the three text blocks carries all four languages — 63
translations per language across 21 alerts — with no block partially
translated.

### 5.3 Quirks of this particular JSON encoding

These are the traps a parser has to survive, all confirmed against the two
captured payloads:

| Quirk | Detail |
| --- | --- |
| camelCase, not snake_case | The protobuf field `trip_update` appears as `tripUpdate`, `stop_time_update` as `stopTimeUpdate`, `gtfs_realtime_version` as `gtfsRealtimeVersion`. A parser written from the GTFS-RT `.proto` field names finds nothing. |
| 64-bit integers as objects | `tripUpdate.timestamp` is `{"low": …, "high": …, "unsigned": true}` — the protobuf.js Long representation — in **all 113** entities. `header.timestamp` and every `arrival.time` / `departure.time` in the same payload are plain integers. The encoding is inconsistent within a single response, so `int(x)` is not safe without a type check. |
| Different `gtfsRealtimeVersion` per feed | The trip-update feed declares `"1.0"`, the alert feed `"2.0"`. |
| `incrementality` only on trip updates | Present as `0` (FULL_DATASET) on the trip-update header, absent from the alert header. Each poll is a complete snapshot, not a delta — which is what makes append-only snapshot storage correct. |
| Enums stay bare integers | `cause`, `effect` and `scheduleRelationship` arrive as numbers with no labels. `sql/06_realtime.sql` seeds `ref_alert_cause` (12 rows), `ref_alert_effect` (11) and `ref_schedule_relationship` (7) so a query can join for a label instead of hard-coding one — but see the caveat below on which `scheduleRelationship` those 7 rows describe. |
| Empty `activePeriod` | All 21 alerts in the sample carry `"activePeriod": []`, i.e. active until further notice. `rt_alert_active_period` therefore stays empty for this sample; it is not a loader bug. |
| Network-wide `informedEntity` | All 21 alerts scope to `{"agencyId": "nmbssncb"}` only — no route, trip or stop is named. Attaching an alert to a specific train from this feed alone is not possible; the affected line is stated in prose inside `headerText`. |

### 5.4 An open defect: the two `scheduleRelationship` enumerations are conflated

Section 5.1 is the theory. The loader does not yet follow it, and the gap is
worth stating rather than glossing over.

`ref_schedule_relationship` as seeded in `sql/06_realtime.sql` holds the
**trip-level** enumeration: `0 SCHEDULED, 1 ADDED, 2 UNSCHEDULED, 3 CANCELED,
5 REPLACEMENT, 6 DUPLICATED, 7 DELETED`. `rt_stop_time_update.schedule_relationship`
carries **stop-level** codes but has a foreign key to that same table, so a
stop-level `2` — `NO_DATA` — resolves to the label `UNSCHEDULED`. The FK is
satisfied; the label is wrong.

`v_rt_departure_performance` then reads the same code as a cancellation:
`CASE WHEN o.schedule_relationship = 2 THEN 1 ELSE 0 END AS is_skipped`. Against
the seven snapshots currently stored that marks **1 107 of 1 787 rows**
(62%) as skipped, when the feed is only saying it has no live data for those
calls. This is precisely the misreading section 5.1 warns about, and it is
currently in the repository.

The fix belongs in `sql/06_realtime.sql`, not here: either a second reference
table for the stop-level enumeration, or a `NO_DATA` verdict distinct from
`SKIPPED`. Until it lands, treat `is_skipped` from that view as unreliable —
the raw stop-level code in `rt_stop_time_update` is the honest source, and its
distribution over the stored snapshots is 2 453 SCHEDULED / 4 SKIPPED /
4 178 NO_DATA.

---

## 6. Licence and attribution

The datasets are published under the **Creative Commons Attribution 4.0
International (CC BY 4.0)** licence. That permits copying, redistribution,
adaptation and commercial use, on one condition: attribution.

The portal prescribes the exact form:

> Source: [PTO Name] – Open Data – [Date of dataset update]

and, for modified data:

> Contains data originally published by [PTO Name], modified by [User Name].

### 6.1 The exact strings this project must display

The dataset loaded here is the NMBS-SNCB GTFS Static feed whose `feed_info.txt`
declares `feed_version = 2026-07-20`. That publisher-declared version is the
"date of dataset update":

```
Source: NMBS-SNCB – Open Data – 2026-07-20
```

Because RailPulse normalises, cleans and quarantines rows rather than
redistributing the feed verbatim, the modified-data form applies to any output
derived from it:

```
Contains data originally published by NMBS-SNCB, modified by RailPulse.
```

The separator is an en dash (`–`), as printed by the portal, not a hyphen.

`config.ATTRIBUTION_TEMPLATE` does not currently render that string. It holds
`"NMBS/SNCB – Open Data – {feed_date}"`, which differs from the portal's
prescribed form in two ways: no `Source: ` prefix, and `NMBS/SNCB` where the
portal names the operator `NMBS-SNCB`. Neither is fatal — the licence asks for
attribution, not for a byte-exact template — but the two should be reconciled,
and the portal's form is the one to move towards.

### 6.2 Where the attribution belongs

| Location | Form | State on 2026-07-23 |
| --- | --- | --- |
| `src/railpulse/config.py` | `ATTRIBUTION_TEMPLATE`, so the date is rendered from the loaded feed rather than typed by hand and left to go stale | Present, wording not yet aligned (above) |
| `feed_info` table | `feed_publisher_name`, `feed_publisher_url` and `feed_version` are loaded from the feed, so the database itself carries the provenance of every row | Present — one row, `feed_version` `2026-07-20`. Note `feed_publisher_name` is the slug `nmbssncb`, not a display name, so only the *date* can be rendered from this table; the operator name still has to come from the template |
| `ingestion_run` table | `source_url`, `source_last_modified`, `http_status` and `bytes_downloaded` per run | Present — columns exist; the one recorded run was an `--offline` rebuild, so its HTTP columns are `NULL` |
| `README.md` | Both strings, in a Data & licence section | **Not yet written.** The file does not exist at the time of writing |
| Streamlit dashboard | Footer, on every page | **Not yet built.** `dashboard/` is empty; this is a Sprint 3 obligation, recorded here so it is not forgotten |

Rendering the date from `feed_info.feed_version` rather than hard-coding it is
the point: an attribution that names the wrong version of the dataset is not
attribution, it is a stale string.

Two smaller notes:

* The portal's FAQ separately asks users to *"credit 'Belgian Mobility Company'
  and include a link to this portal when possible"*. That is a softer request
  than the licence condition, and it is honoured by linking
  <https://data.belgianmobility.io> alongside the attribution line.
* The GTFS feed's own `feed_publisher_url` is `http://www.belgiantrain.be/`,
  and `feed_lang` is `fr` — which is why every station name in this database is
  French, with nl/de/en held in `text_translation`.

---

## 7. A note on iRail

The mission text points at the **iRail API** (<https://api.irail.be>), and it is
worth being explicit about why this project does not use it.

iRail is a long-running, community-run open-source project that scrapes and
re-serves SNCB data. Its liveboard endpoint —
`GET /liveboard/{?id,station,date,time,arrdep,lang,format,alerts}`, in practice
`/liveboard/?station=...&format=json` — returns the departure board for one
station: *"Liveboards provides real-time informations on arriving and departing
trains for every Belgian station."* It is genuinely good, and it is free.

### 7.1 Its documented limits

From <https://docs.irail.be>:

* **Rate limit:** *"You can make up to 3 requests per second per source IP
  address. Every IP address also has 5 burst requests, meaning you can either
  have 8 requests in 1 second or 15 requests in 3 seconds"*.
* **User-Agent:** the docs ask for a header identifying the application, in the
  form `<application name>/<application version> (<website>; <mail>)`.
* **Caching:** the docs describe support for conditional requests —
  `If-None-Match` on the request, `ETag` and `Cache-Control` on the response —
  to minimise data transfer. Honouring them is presented as good practice
  rather than as an enforced rule.
* **Licence:** the documentation pages consulted do not state a licence for the
  API. That is a genuine gap in what could be confirmed, not an omission here —
  anyone planning to redistribute iRail output should establish the terms
  before doing so.

### 7.2 Why the official portal wins for this project

The two APIs answer different questions.

| | BMC GTFS Static | iRail liveboard |
| --- | --- | --- |
| Grain | The complete national timetable | One station, one moment |
| Requests for a full network snapshot | **1** | **652** — one per station in this feed |
| Horizon | 2025-12-20 → 2026-12-12 (358 service dates) | The next few departures |
| Contains service calendars, platform allocations, bikes/wheelchair flags, headsigns | Yes | Partially, and not for future dates |
| Cost of Q1 (peak hour across the year) | One download, then SQL | Impossible — the data does not exist in the response |

Sweeping all 652 stations through iRail costs 652 requests, which at its own
3 requests/second takes roughly 218 seconds and produces one instant's worth of
departure boards. The same 652 stations arrive in a single 26 MB GTFS download,
with a full year of scheduled service attached. For questions Q1 to Q5 — peak
hour, platform bottlenecks, morning destinations, service frequency,
accessibility — the timetable *is* the answer, and the liveboard cannot supply
it at any polling rate.

The official portal is also the operator's own publication rather than a
re-serving of it, which matters for a licence and attribution statement.

### 7.3 When iRail would be the right call

* **Ad-hoc, single-station queries** — one departure board, no infrastructure.
  652 stations of GTFS is absurd overkill to answer "when is the next train to
  Ghent".
* **No subscription key available.** iRail needs no registration.
* **Cross-checking a delay figure** from an independent source before trusting
  a GTFS-RT reading.
* **Composed journeys.** iRail's `connections` endpoint does route planning;
  GTFS gives you the raw timetable and leaves the planning to you.

For Sprint 2, when the Azure Function begins polling live delays on a timer,
the GTFS-RT trip-update feed remains the right source: two requests capture the
entire network, where iRail would need one request per station per poll.

---

## 8. Verification log

Everything above, and how it was confirmed. Portal pages were read on
2026-07-23.

| Claim | Confirmed by |
| --- | --- |
| Four operators: De Lijn, STIB-MIVB, LETEC, NMBS-SNCB | Developer portal home — *"looking for the open-data of the Belgian Public Transport Operators (LETEC, De Lijn, NMBS-SNCB, STIB-MIVB)"*, and one card per operator on `/en/data.html` |
| Cadence: static daily, GTFS-RT 30 s, NeTEx daily, STIB JSON real-time / 15 s | Dataset cards on `/en/data.html`; Vehicle Positions is the only STIB card at 15 s, the other four read "Real-time" |
| NMBS-SNCB publishes 2 RT feeds (TU, SA); De Lijn 2 (TU, SA); LETEC 3 (TU, SA, VP); STIB-MIVB none | Feed lists printed on the RT cards, `/en/data.html` |
| Five STIB JSON dataset URLs | Printed verbatim on `/en/data.html` |
| GTFS-RT documented as "Protocol Buffers" | Format field on all three RT cards |
| Quotas: anonymous 100/day + 10/min; Standard 12 000/day + 500/min; Gold *"Upon request"*, its registration cell reading "yes, plus SLA"; quota column headed *"Usage Quota (indicative)"* | `/en/terms.html` tier table |
| *"BMC reserves the right to charge fees for Gold tier access…"*; *"Users start with Standard, can request Gold via service agreement"*; *"The Anonymous and Standard Access Tiers are not covered by any Service Level Agreement"* | `/en/terms.html` |
| Keys are *"personal and non-transferable"* | `/en/terms.html` |
| HTTP 429 on limit breach | `/en/terms.html` |
| CC BY 4.0 and both attribution strings | `/en/terms.html` |
| FAQ asks for credit to *"Belgian Mobility Company"* and a portal link *"when possible"* | `/en/faq.html` |
| `opendata@belgianmobilitycompany.be`, and that no Gold-specific contact route is printed | `/en/contact.html` |
| Guest access: *"to use them you will need a valid subscription key"* | Developer portal `/apis` |
| Signup / signin URLs | Links on `/en/data.html` |
| Static zip = 26 283 845 bytes; upstream `Last-Modified` `Thu, 23 Jul 2026 03:05:09 GMT` | `data/raw/nmbssncb_gtfs_static.zip`, `…​.last_modified` |
| Sample payloads 187 880 and 55 663 bytes; 113 and 21 entities; 1 482 stop-time updates (957 × NO_DATA, 524 × SCHEDULED, 1 × SKIPPED); the 957 NO_DATA rows are exactly the 957 that carry no time; 946 delay values, range 0–1 080 s; all 113 `tripUpdate.timestamp` values are Long objects and 0 `time` fields are; header timestamps 1784793557 / 1784793576 = 07:59:17 / 07:59:36 UTC; all 21 alerts have `activePeriod: []` and an agency-only `informedEntity`; 63 translations per language | Parsed from `data/raw/sample_trip_update.json` and `sample_alert.json` |
| 652 stations, 1 801 routes, 134 809 trips, 2 165 507 stop times, 358 distinct service dates, feed window 2025-12-20 → 2026-12-12, `feed_version` 2026-07-20, `feed_lang` fr, `feed_publisher_url` `http://www.belgiantrain.be/` | `SELECT` against `data/railpulse.db` |
| `ref_schedule_relationship` holds the trip-level enumeration; `v_rt_departure_performance` marks 1 107 of 1 787 rows `is_skipped`; stored stop-level codes are 2 453 / 4 / 4 178 for 0 / 1 / 2 | `sql/06_realtime.sql` lines 192-199 and 271-273, plus `SELECT` against the seven stored `rt_snapshot` rows |
| A build costs one request | `src/railpulse/build.py` → `fetch_static_feed()` → one `download_gtfs_static()` call; `api_client.py` is the only module that imports `requests` |
| iRail limits, User-Agent form, caching headers, liveboard endpoint and description | docs.irail.be |
| Stop-level `scheduleRelationship`: 0 SCHEDULED, 1 SKIPPED, 2 NO_DATA, 3 UNSCHEDULED | gtfs.org GTFS-Realtime reference |
| **Not confirmed:** operator slugs `delijn`, `letec`, `stibmivb` in GTFS paths; the LETEC vehicle-position path; NeTEx EPIP URLs; a licence statement for the iRail API; the literal wording of a live 401 or 429 from this gateway; that this gateway honours `If-Modified-Since` (no 304 has been observed); that Gold is requested at the general contact address | Stated as unconfirmed above rather than guessed |
