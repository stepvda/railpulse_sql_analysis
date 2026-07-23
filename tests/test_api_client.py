"""The HTTP client's retry and rate-limit logic.

These are pure-function tests — nothing here touches the network. The point is
the edge cases, because the failure mode of a retry loop is that it works
perfectly against a healthy server and falls over the first time something
upstream misbehaves, which is exactly when you need it.

Each test below corresponds to a defect that was found and fixed by inspection
rather than by a crash in production, and is pinned here so it stays fixed.
"""

from __future__ import annotations

import time

import pytest

from railpulse.api_client import (
    MAX_SERVER_BACKOFF_SECONDS,
    BelgianMobilityClient,
    RateLimiter,
    _parse_retry_after,
)


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, "", "   "])
def test_absent_header_returns_none(value):
    assert _parse_retry_after(value) is None


@pytest.mark.parametrize(
    "value, expected",
    [("0", 0.0), ("1", 1.0), ("5", 5.0), ("120", 120.0)],
)
def test_delta_seconds_are_parsed(value, expected):
    assert _parse_retry_after(value) == expected


def test_zero_is_distinguishable_from_absent():
    """`Retry-After: 0` means "retry immediately" and must not be confused
    with "no instruction given".

    The original code wrote `_parse_retry_after(h) or fallback`, and because
    0.0 is falsy that turned an instruction to retry NOW into a 60-second
    sleep. The caller now tests `is None` instead.
    """
    assert _parse_retry_after("0") == 0.0
    assert _parse_retry_after("0") is not None
    assert _parse_retry_after(None) is None


@pytest.mark.parametrize(
    "value",
    ["garbage", "-5", "12.5", "\x00", "Retry later please", "Mon, 99 Xxx 9999"],
)
def test_malformed_header_returns_none_and_never_raises(value):
    """A broken intermediary must not be able to crash an ingestion run.

    `email.utils.parsedate_to_datetime` RAISES ValueError on unparseable input
    in Python 3.10+ (older versions returned None), so this needs an explicit
    guard, not an assumption.
    """
    assert _parse_retry_after(value) is None


def test_http_date_in_the_past_clamps_to_zero():
    assert _parse_retry_after("Mon, 01 Jan 2020 00:00:00 GMT") == 0.0


def test_absurd_backoff_is_capped():
    """We honour the server's wishes, but not to the point of hanging.

    A misconfigured proxy answering with a date months out would otherwise
    park the job indefinitely.
    """
    assert _parse_retry_after("99999999") == MAX_SERVER_BACKOFF_SECONDS
    assert _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") == MAX_SERVER_BACKOFF_SECONDS


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_first_call_does_not_wait():
    assert RateLimiter(10.0).wait() == 0.0


def test_subsequent_call_waits_the_minimum_interval():
    limiter = RateLimiter(0.15)
    limiter.wait()
    started = time.perf_counter()
    slept = limiter.wait()
    elapsed = time.perf_counter() - started
    assert slept > 0
    assert elapsed >= 0.13          # small tolerance for timer granularity


def test_no_wait_when_enough_time_has_already_passed():
    limiter = RateLimiter(0.01)
    limiter.wait()
    time.sleep(0.05)
    assert limiter.wait() == 0.0


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------
def test_key_is_masked_in_the_auth_description():
    """describe_auth() is printed by every job. It must never print the key."""
    key = "0123456789abcdef0123456789abcdef"
    description = BelgianMobilityClient(api_key=key).describe_auth()
    assert key not in description
    assert "0123" in description and "cdef" in description   # enough to identify


def test_anonymous_client_says_so_loudly():
    description = BelgianMobilityClient(api_key="").describe_auth()
    assert "ANONYMOUS" in description
    assert "100 requests/day" in description


def test_key_is_sent_as_the_azure_subscription_header():
    from railpulse import config

    client = BelgianMobilityClient(api_key="abc")
    assert client.session.headers[config.API_KEY_HEADER] == "abc"
    assert client.is_authenticated


def test_no_auth_header_when_no_key():
    from railpulse import config

    client = BelgianMobilityClient(api_key="")
    assert config.API_KEY_HEADER not in client.session.headers
    assert not client.is_authenticated


# ---------------------------------------------------------------------------
# Retryable status classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_transient_statuses_are_retried(status):
    assert status in BelgianMobilityClient.RETRYABLE_STATUSES


@pytest.mark.parametrize("status", [200, 304, 400, 401, 403, 404])
def test_permanent_statuses_are_not_retried(status):
    """Retrying a 401 or a 404 is not resilience, it is abuse: the request is
    wrong and repeating it will not make it right."""
    assert status not in BelgianMobilityClient.RETRYABLE_STATUSES


# ---------------------------------------------------------------------------
# max_retries floor (a mis-set env var must not silently no-op every fetch)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("requested", [0, -1, -100])
def test_max_retries_floors_at_one(requested):
    """max_retries=0 would make _request skip its loop and fetch nothing."""
    assert BelgianMobilityClient(api_key="", max_retries=requested).max_retries == 1


def test_max_retries_passes_through_when_positive():
    assert BelgianMobilityClient(api_key="", max_retries=7).max_retries == 7
