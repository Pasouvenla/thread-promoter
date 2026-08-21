"""The healthcheck runs every 60 seconds forever, so it must never be noisy
and never be wrong about a live bot."""

from __future__ import annotations

import time

import pytest

from app.healthcheck import is_healthy


@pytest.fixture
def beat(tmp_path):
    return tmp_path / "heartbeat"


def test_a_fresh_heartbeat_is_healthy(beat):
    beat.write_text(f"{time.time()} 0.042")
    assert is_healthy(beat)


def test_a_stale_heartbeat_is_not(beat):
    beat.write_text(f"{time.time() - 500} 0.042")
    assert not is_healthy(beat)


def test_a_missing_file_is_not_healthy_and_does_not_raise(beat):
    assert not is_healthy(beat)


@pytest.mark.parametrize("content", ["", "corrupted", "   ", "\x00"])
def test_an_unreadable_heartbeat_is_not_healthy_and_does_not_raise(beat, content):
    beat.write_text(content)
    assert not is_healthy(beat)


def test_a_heartbeat_from_the_future_is_rejected(beat):
    """A clock jump backwards must not make a dead bot look alive forever."""
    beat.write_text(f"{time.time() + 10_000} 0.042")
    assert not is_healthy(beat)


def test_the_window_covers_more_than_one_missed_beat():
    from app.bot import HEARTBEAT_INTERVAL
    from app.healthcheck import MAX_AGE

    assert MAX_AGE > HEARTBEAT_INTERVAL * 2, "one slow write would flap the container"
