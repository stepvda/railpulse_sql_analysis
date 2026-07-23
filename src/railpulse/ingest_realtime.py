"""GTFS-Realtime JSON  ->  ``rt_*`` tables.

This is the "Live Stream Integration" nice-to-have: an append-only poller that,
run on a timer, turns the operator's 30-second live feed into a delay history
the static timetable can be measured against.

DESIGN NOTES
------------
*Append-only.* A snapshot is never updated or replaced. Punctuality analysis
needs to know what was predicted *at the time*, not just the final outcome, and
an immutable log is also the only honest audit trail.

*Idempotent.* ``rt_snapshot`` carries ``UNIQUE (feed, feed_timestamp_epoch)``.
The upstream feed rebuilds every ~30 s; if the poller fires more often than
that, or a cron run overlaps a manual one, the identical payload comes back
with the same header timestamp and the insert is skipped rather than
double-counting every delay in it.

*Shredding, not parsing.* Like the static loader, this module does not
interpret values. It walks the JSON and writes each leaf into the column that
matches it. Whether a delay of 3 600 s is plausible, whether a SKIPPED stop
counts as late — those are questions for SQL.

*Why there is no protobuf dependency.* The portal documents these feeds as
Protocol Buffers, but the gateway actually serves the JSON encoding of the same
GTFS-RT message. See docs/api_and_compliance.md.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import config
from .api_client import BelgianMobilityClient, FetchResult
from .db import connect, transaction

FEEDS = ("trip-update", "alert")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_to_utc(epoch: Any) -> str | None:
    """Format a GTFS-RT POSIX timestamp as ISO-8601 UTC, or None."""
    try:
        value = int(epoch)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _gtfs_date_to_iso(value: Any) -> str | None:
    """'20260723' -> '2026-07-23'. Anything else passes through untouched."""
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# snapshot bookkeeping
# ---------------------------------------------------------------------------
def _open_snapshot(
    conn: sqlite3.Connection, feed: str, result: FetchResult
) -> int | None:
    """Insert the snapshot header. Returns None when it is a duplicate."""
    payload = result.payload or {}
    header = payload.get("header", {})
    feed_epoch = _int_or_none(header.get("timestamp"))
    entities = payload.get("entity", []) or []

    try:
        cursor = conn.execute(
            "INSERT INTO rt_snapshot (feed, fetched_at_utc, feed_timestamp_epoch, "
            "                         feed_timestamp_utc, entity_count, "
            "                         bytes_downloaded, source_url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (feed, _utc_now(), feed_epoch, _epoch_to_utc(feed_epoch),
             len(entities), result.bytes_downloaded, result.url),
        )
    except sqlite3.IntegrityError:
        # UNIQUE (feed, feed_timestamp_epoch): the operator has not rebuilt the
        # feed since our last poll. Nothing new to record.
        print(f"    {feed}: unchanged upstream "
              f"(feed timestamp {feed_epoch}); snapshot skipped")
        return None
    return int(cursor.lastrowid)


# ---------------------------------------------------------------------------
# trip updates
# ---------------------------------------------------------------------------
def _shred_trip_updates(
    snapshot_id: int, entities: Iterable[dict]
) -> tuple[list[Sequence], list[Sequence]]:
    """Flatten tripUpdate entities into (trip rows, stop-time rows)."""
    trip_rows: list[Sequence] = []
    stu_rows: list[Sequence] = []

    for entity in entities:
        update = entity.get("tripUpdate")
        if not update:
            continue
        entity_id = entity.get("id")
        trip = update.get("trip", {}) or {}
        vehicle = update.get("vehicle", {}) or {}

        trip_rows.append((
            snapshot_id,
            entity_id,
            trip.get("tripId"),
            trip.get("routeId"),
            _gtfs_date_to_iso(trip.get("startDate")),
            trip.get("startTime"),
            _int_or_none(trip.get("scheduleRelationship")),
            vehicle.get("id"),
            _int_or_none(update.get("timestamp")),
        ))

        seen_sequences: set[int] = set()
        for position, stu in enumerate(update.get("stopTimeUpdate", []) or []):
            arrival = stu.get("arrival") or {}
            departure = stu.get("departure") or {}
            # stopSequence is optional in GTFS-RT. It is part of this table's
            # primary key, so fall back to the position in the array rather
            # than dropping the row; a collision would lose an observation.
            sequence = _int_or_none(stu.get("stopSequence"))
            if sequence is None or sequence in seen_sequences:
                sequence = -(position + 1)
            seen_sequences.add(sequence)

            stu_rows.append((
                snapshot_id,
                entity_id,
                sequence,
                stu.get("stopId"),
                _int_or_none(arrival.get("time")),
                _int_or_none(arrival.get("delay")),
                _int_or_none(departure.get("time")),
                _int_or_none(departure.get("delay")),
                _int_or_none(stu.get("scheduleRelationship")),
            ))

    return trip_rows, stu_rows


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------
def _shred_alerts(
    snapshot_id: int, entities: Iterable[dict]
) -> tuple[list[Sequence], list[Sequence], list[Sequence], list[Sequence]]:
    """Flatten alert entities into (alert, text, informed-entity, period) rows."""
    alerts: list[Sequence] = []
    texts: list[Sequence] = []
    informed: list[Sequence] = []
    periods: list[Sequence] = []

    for entity in entities:
        alert = entity.get("alert")
        if not alert:
            continue
        entity_id = entity.get("id")

        alerts.append((
            snapshot_id,
            entity_id,
            _int_or_none(alert.get("cause")),
            _int_or_none(alert.get("effect")),
            alert.get("url", {}).get("translation", [{}])[0].get("text")
            if isinstance(alert.get("url"), dict) else alert.get("url"),
        ))

        # headerText / descriptionText are TranslatedString: one row per
        # language, which is why rt_alert_text exists instead of four columns.
        for field_name, key in (("header", "headerText"),
                                ("description", "descriptionText")):
            translated = alert.get(key) or {}
            seen_languages: set[str] = set()
            for translation in translated.get("translation", []) or []:
                language = translation.get("language")
                text = translation.get("text")
                if not language or not text or language in seen_languages:
                    continue
                seen_languages.add(language)
                texts.append((snapshot_id, entity_id, field_name, language, text))

        for index, informed_entity in enumerate(alert.get("informedEntity", []) or []):
            informed.append((
                snapshot_id, entity_id, index,
                informed_entity.get("agencyId"),
                informed_entity.get("routeId"),
                informed_entity.get("stopId"),
                (informed_entity.get("trip") or {}).get("tripId"),
            ))

        for index, period in enumerate(alert.get("activePeriod", []) or []):
            periods.append((
                snapshot_id, entity_id, index,
                _int_or_none(period.get("start")),
                _int_or_none(period.get("end")),
            ))

    return alerts, texts, informed, periods


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------
def ingest_feed(
    conn: sqlite3.Connection,
    client: BelgianMobilityClient,
    feed: str,
) -> dict[str, int]:
    """Poll one feed once and append it. Returns rows written per table."""
    result = client.fetch_realtime(feed)
    payload = result.payload or {}
    entities = payload.get("entity", []) or []
    written: dict[str, int] = {}

    with transaction(conn):
        snapshot_id = _open_snapshot(conn, feed, result)
        if snapshot_id is None:
            return written

        if feed == "trip-update":
            trip_rows, stu_rows = _shred_trip_updates(snapshot_id, entities)
            conn.executemany(
                "INSERT INTO rt_trip_update (snapshot_id, rt_entity_id, trip_id, "
                " route_id, start_date, start_time, schedule_relationship, "
                " vehicle_id, update_timestamp_epoch) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                trip_rows,
            )
            conn.executemany(
                "INSERT INTO rt_stop_time_update (snapshot_id, rt_entity_id, "
                " stop_sequence, stop_id, arrival_epoch, arrival_delay_s, "
                " departure_epoch, departure_delay_s, schedule_relationship) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                stu_rows,
            )
            written = {"rt_trip_update": len(trip_rows),
                       "rt_stop_time_update": len(stu_rows)}
        else:
            alerts, texts, informed, periods = _shred_alerts(snapshot_id, entities)
            conn.executemany(
                "INSERT INTO rt_alert (snapshot_id, rt_entity_id, cause, effect, url) "
                "VALUES (?, ?, ?, ?, ?)", alerts)
            conn.executemany(
                "INSERT INTO rt_alert_text (snapshot_id, rt_entity_id, field_name, "
                " language, text) VALUES (?, ?, ?, ?, ?)", texts)
            conn.executemany(
                "INSERT INTO rt_alert_informed_entity (snapshot_id, rt_entity_id, "
                " entity_seq, agency_id, route_id, stop_id, trip_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", informed)
            conn.executemany(
                "INSERT INTO rt_alert_active_period (snapshot_id, rt_entity_id, "
                " period_seq, start_epoch, end_epoch) VALUES (?, ?, ?, ?, ?)",
                periods)
            written = {"rt_alert": len(alerts), "rt_alert_text": len(texts),
                       "rt_alert_informed_entity": len(informed),
                       "rt_alert_active_period": len(periods)}

    for table, count in written.items():
        print(f"    + {table:<26} {count:>6,} rows")
    return written


def poll_once(feeds: Sequence[str] = FEEDS) -> dict[str, int]:
    """One poll of each requested feed. This is what the cron entry calls."""
    conn = connect()
    client = BelgianMobilityClient()
    print(f"[realtime] {_utc_now()}  auth: {client.describe_auth()}")
    totals: dict[str, int] = {}
    try:
        for feed in feeds:
            for table, count in ingest_feed(conn, client, feed).items():
                totals[table] = totals.get(table, 0) + count
    finally:
        conn.close()
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="railpulse poll",
        description="Append one GTFS-Realtime snapshot to the database.",
    )
    parser.add_argument("--feed", choices=[*FEEDS, "all"], default="all",
                        help="which feed to poll (default: all)")
    args = parser.parse_args(argv)

    feeds = FEEDS if args.feed == "all" else (args.feed,)
    poll_once(feeds)
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
