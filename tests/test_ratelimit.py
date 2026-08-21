"""The counter reads discord.py's log records, so the markers it looks for are
pinned against the installed library rather than trusted."""

from __future__ import annotations

import inspect
import logging

import discord.http
import pytest

from app.ratelimit import (
    BUCKET_MARKER,
    GLOBAL_MARKER,
    PREEMPTIVE_MARKER,
    RateLimitCounter,
    RateLimitStats,
)


@pytest.mark.parametrize("marker", [BUCKET_MARKER, GLOBAL_MARKER, PREEMPTIVE_MARKER])
def test_the_markers_still_exist_in_discord_py(marker):
    """Fails loudly on an upgrade that reworks these messages.

    Without this the counter would quietly report zero forever.
    """
    source = inspect.getsource(discord.http)
    assert marker in source, f"discord.py no longer logs {marker!r}"


@pytest.fixture
def counter():
    stats = RateLimitStats()
    handler = RateLimitCounter(stats)
    logger = logging.getLogger("test.ratelimit")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    yield logger, stats
    logger.removeHandler(handler)


def test_a_bucket_rate_limit_is_counted(counter):
    logger, stats = counter
    logger.warning(
        "We are being rate limited. %s %s responded with 429. Retrying in %.2f seconds.",
        "POST", "/channels/1", 3.5,
    )
    assert stats.bucket_hits == 1
    assert stats.seconds_waited == pytest.approx(3.5)


def test_a_global_rate_limit_is_counted_apart(counter):
    logger, stats = counter
    logger.warning("Global rate limit has been hit. Retrying in %.2f seconds.", 10.0)
    assert stats.global_hits == 1
    assert stats.bucket_hits == 1
    assert stats.seconds_waited == pytest.approx(10.0)


def test_a_preemptive_wait_is_counted(counter):
    logger, stats = counter
    logger.warning("A rate limit bucket (%s) has been exhausted. Pre-emptively rate limiting...", "abc")
    assert stats.preemptive_waits == 1


def test_unrelated_warnings_are_ignored(counter):
    logger, stats = counter
    logger.warning("Something else entirely happened")
    assert stats.as_dict() == {
        "bucket_hits": 0, "global_hits": 0, "preemptive_waits": 0, "seconds_waited": 0.0
    }


def test_a_quiet_run_says_so():
    assert RateLimitStats().summary() == "no rate limiting"


def test_a_throttled_run_is_readable():
    summary = RateLimitStats(bucket_hits=12, global_hits=2, seconds_waited=61.4).summary()
    assert "12 rate limit(s)" in summary and "2 global" in summary and "61s" in summary


def test_a_broken_record_does_not_raise():
    """A record whose formatting fails must not take a migration down with it."""
    stats = RateLimitStats()
    handler = RateLimitCounter(stats)
    record = logging.LogRecord(
        "discord.http", logging.WARNING, __file__, 1,
        "We are being rate limited %s %s", ("only-one-arg",), None,
    )
    handler.emit(record)
    assert stats.bucket_hits == 0
