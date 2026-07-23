"""A polite HTTP client for the Belgian Mobility Open Data API.

The brief is explicit: *"Be mindful of the request limits so you don't get
blocked! Read the documentation for the usage of the API to make sure you're
compliant."* This module is that compliance, in code:

* **Client-side throttle.** The portal publishes 10 requests/minute and 100
  requests/day for anonymous callers. A process-wide gate keeps at least
  ``60 / MAX_REQUESTS_PER_MINUTE`` seconds between calls, so the limit is never
  approached rather than being discovered by getting a 429.
* **Honour ``Retry-After``.** On 429 or 503 the server's own backoff is used
  when it supplies one, and exponential backoff with a cap when it does not.
* **Retry only what is retryable.** 4xx other than 429 means the request is
  wrong; retrying it is just abuse.
* **Conditional GET.** The static feed is 26 MB and is regenerated once a day.
  ``If-Modified-Since`` turns a same-day re-run into a 304 and no download.
* **Attribution + identification.** A descriptive ``User-Agent`` so the
  operator can see who is calling, and the CC BY 4.0 attribution string is
  recorded alongside the data it applies to.

This is the *only* place in the project where the network is touched.
"""

from __future__ import annotations

import email.utils
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from . import config


class RateLimiter:
    """Minimum-interval gate shared by every request this process makes."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = min_interval_seconds
        self._last_call: float | None = None

    def wait(self) -> float:
        """Block until the next call is allowed. Returns the seconds slept."""
        if self._last_call is None:
            self._last_call = time.monotonic()
            return 0.0
        elapsed = time.monotonic() - self._last_call
        sleep_for = self.min_interval - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            sleep_for = 0.0
        self._last_call = time.monotonic()
        return sleep_for


@dataclass
class FetchResult:
    """Outcome of one API call, recorded into ``ingestion_run``."""

    url: str
    status_code: int
    bytes_downloaded: int
    last_modified: str | None
    path: Path | None = None
    payload: Any | None = None
    not_modified: bool = False


class BelgianMobilityClient:
    """Thin, rate-limited wrapper over the two feeds this project consumes."""

    #: Statuses worth retrying: transient server-side or throttling conditions.
    RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str | None = None,
        *,
        max_requests_per_minute: int | None = None,
        max_retries: int | None = None,
        timeout: int | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else config.API_KEY).strip()
        self.max_retries = max_retries or config.MAX_RETRIES
        self.timeout = timeout or config.REQUEST_TIMEOUT_SECONDS
        rpm = max_requests_per_minute or config.MAX_REQUESTS_PER_MINUTE
        self.limiter = RateLimiter(60.0 / max(rpm, 1))

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
        })
        if self.api_key:
            self.session.headers[config.API_KEY_HEADER] = self.api_key

    # -- introspection ----------------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        """True when a subscription key is in play (higher published quotas)."""
        return bool(self.api_key)

    def describe_auth(self) -> str:
        """Human-readable auth state — printed by every job at start-up."""
        if self.is_authenticated:
            masked = f"{self.api_key[:4]}…{self.api_key[-4:]}"
            return f"subscription key {masked} (Standard tier)"
        return (
            "ANONYMOUS — no BMC_API_KEY set. The feeds still answer, but you are "
            "capped at 100 requests/day and 10/minute. See .env.example."
        )

    # -- core request loop -------------------------------------------------
    def _request(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """GET *url* with throttling, bounded retries and honest backoff."""
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            slept = self.limiter.wait()
            if slept:
                print(f"    rate-limit gate: waited {slept:0.1f}s")
            try:
                response = self.session.get(
                    url, headers=headers, timeout=self.timeout, stream=stream
                )
            except requests.RequestException as exc:
                last_error = exc
                # Only sleep if another attempt will actually follow. Sleeping
                # after the final failure just delays the exception by up to a
                # minute for no benefit.
                if attempt < self.max_retries:
                    backoff = min(2 ** attempt, 60)
                    print(f"    attempt {attempt}/{self.max_retries} failed "
                          f"({exc}); retrying in {backoff}s")
                    time.sleep(backoff)
                else:
                    print(f"    attempt {attempt}/{self.max_retries} failed ({exc})")
                continue

            if response.status_code not in self.RETRYABLE_STATUSES:
                # Includes 2xx, 304, and non-retryable 4xx — let the caller
                # decide. raise_for_status() is applied by the public methods.
                return response

            # Retryable. Prefer the server's own Retry-After over our guess.
            # `is None` rather than a truthiness test on purpose: "Retry-After: 0"
            # is a valid instruction meaning "retry immediately", and `or` would
            # silently discard it in favour of exponential backoff.
            server_backoff = _parse_retry_after(response.headers.get("Retry-After"))
            backoff = (server_backoff if server_backoff is not None
                       else min(2 ** attempt, 60))
            print(f"    HTTP {response.status_code} from API "
                  f"(attempt {attempt}/{self.max_retries}); backing off {backoff:0.0f}s")
            last_error = requests.HTTPError(
                f"HTTP {response.status_code}", response=response
            )
            if attempt < self.max_retries:
                time.sleep(backoff)

        raise RuntimeError(
            f"Giving up on {url} after {self.max_retries} attempts"
        ) from last_error

    # -- public API --------------------------------------------------------
    def download_gtfs_static(
        self,
        destination: Path,
        *,
        if_modified_since: str | None = None,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> FetchResult:
        """Download the GTFS Static zip to *destination*.

        Passing *if_modified_since* (an RFC-1123 date, normally the
        ``Last-Modified`` recorded by the previous run) lets the server answer
        304 and skip a 26 MB transfer. The feed is rebuilt once a day, so
        re-running the pipeline in the afternoon should not re-download it.
        """
        url = config.GTFS_STATIC_URL
        headers = {"If-Modified-Since": if_modified_since} if if_modified_since else None

        response = self._request(url, headers=headers, stream=True)

        if response.status_code == 304:
            response.close()
            print(f"  ✓ {url} -> 304 Not Modified (using cached {destination.name})")
            return FetchResult(url, 304, 0, if_modified_since,
                               path=destination, not_modified=True)

        response.raise_for_status()

        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header and total_header.isdigit() else None
        destination.parent.mkdir(parents=True, exist_ok=True)

        # Write to a sibling .part file and rename on success, so an interrupted
        # download can never be mistaken for a complete feed on the next run.
        temp_path = destination.with_suffix(destination.suffix + ".part")
        written = 0
        with temp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                handle.write(chunk)
                written += len(chunk)
                if progress:
                    progress(written, total)
        temp_path.replace(destination)

        last_modified = response.headers.get("Last-Modified")
        print(f"  ✓ {url} -> {written:,} bytes "
              f"(upstream Last-Modified: {last_modified})")
        return FetchResult(url, response.status_code, written, last_modified,
                           path=destination)

    def fetch_realtime(self, feed: str) -> FetchResult:
        """Fetch one GTFS-Realtime feed and return its decoded JSON payload.

        ``feed`` is ``'trip-update'`` or ``'alert'``.

        Worth knowing: although the portal documents these feeds as Protocol
        Buffers, the gateway actually serves the **JSON** encoding of the same
        GTFS-RT message (``Content-Type: application/json``). That is why this
        project needs no ``gtfs-realtime-bindings`` dependency — which keeps the
        pipeline inside the "requests + sqlite3 only" constraint.
        """
        urls = {
            "trip-update": config.GTFS_RT_TRIP_UPDATE_URL,
            "alert": config.GTFS_RT_ALERT_URL,
        }
        if feed not in urls:
            raise ValueError(f"unknown realtime feed {feed!r}; expected {sorted(urls)}")

        url = urls[feed]
        response = self._request(url)
        response.raise_for_status()
        payload = response.json()

        entity_count = len(payload.get("entity", []))
        print(f"  ✓ {feed}: {entity_count} entities, "
              f"{len(response.content):,} bytes")
        return FetchResult(
            url, response.status_code, len(response.content),
            response.headers.get("Last-Modified"), payload=payload,
        )


#: Never sleep longer than this on a server's instruction. A misconfigured
#: proxy that answers `Retry-After: <a date three months out>` would otherwise
#: park the ingestion job until the heat death of the sprint.
MAX_SERVER_BACKOFF_SECONDS = 300.0


def _parse_retry_after(value: str | None) -> float | None:
    """Interpret a ``Retry-After`` header, or return None if it is unusable.

    The header comes in two RFC-9110 forms — delta-seconds (``120``) or an
    HTTP-date (``Wed, 21 Oct 2026 07:28:00 GMT``) — and this has to survive
    both, plus whatever a broken intermediary invents.

    Two things worth knowing about the implementation:

    * ``email.utils.parsedate_to_datetime`` **raises** ``ValueError`` on
      unparseable input in Python 3.10+ (it returned ``None`` in older
      versions). Letting that propagate would turn a cosmetically malformed
      header into a crashed ingestion run, so it is caught.
    * The result is capped at :data:`MAX_SERVER_BACKOFF_SECONDS`. We honour the
      server's wishes, but not to the point of hanging indefinitely.

    Returns ``None`` when there is no usable instruction, which the caller
    distinguishes from a legitimate ``0`` ("retry immediately").
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None

    if value.isdigit():
        return min(float(value), MAX_SERVER_BACKOFF_SECONDS)

    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None

    delta = parsed.timestamp() - time.time()
    return min(max(delta, 0.0), MAX_SERVER_BACKOFF_SECONDS)
