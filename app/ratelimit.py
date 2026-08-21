"""Count the rate limits discord.py absorbs on our behalf.

The library handles 429s internally and says nothing to the caller, so a run
that spent twenty minutes sleeping looks exactly like one that did not. That
missing number is the difference between choosing a pacing value and guessing
one.

There is no public hook for this, so the counter reads the log records
discord.http emits. The two markers below have been stable across the 2.x line;
if a future version reworks them the counter goes quiet rather than wrong, and
the test pins them against the installed library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

BUCKET_MARKER = "We are being rate limited"
GLOBAL_MARKER = "Global rate limit has been hit"
PREEMPTIVE_MARKER = "has been exhausted"


@dataclass
class RateLimitStats:
    bucket_hits: int = 0
    global_hits: int = 0
    preemptive_waits: int = 0
    seconds_waited: float = 0.0

    def summary(self) -> str:
        if not (self.bucket_hits or self.global_hits or self.preemptive_waits):
            return "no rate limiting"
        parts = [f"{self.bucket_hits} rate limit(s)"]
        if self.global_hits:
            parts.append(f"{self.global_hits} global")
        if self.preemptive_waits:
            parts.append(f"{self.preemptive_waits} pre-emptive wait(s)")
        if self.seconds_waited:
            parts.append(f"{self.seconds_waited:.0f}s waiting")
        return ", ".join(parts)

    def as_dict(self) -> dict:
        return {
            "bucket_hits": self.bucket_hits,
            "global_hits": self.global_hits,
            "preemptive_waits": self.preemptive_waits,
            "seconds_waited": round(self.seconds_waited, 1),
        }


class RateLimitCounter(logging.Handler):
    def __init__(self, stats: RateLimitStats | None = None) -> None:
        super().__init__(level=logging.WARNING)
        self.stats = stats or RateLimitStats()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not kill a run
            return
        if GLOBAL_MARKER in message:
            self.stats.global_hits += 1
            self.stats.bucket_hits += 1
        elif BUCKET_MARKER in message:
            self.stats.bucket_hits += 1
        elif PREEMPTIVE_MARKER in message:
            self.stats.preemptive_waits += 1
        else:
            return
        self.stats.seconds_waited += _seconds_from(record)


def _seconds_from(record: logging.LogRecord) -> float:
    """Pull the retry delay out of the record's own arguments.

    discord.py passes it as a positional float, so this reads the value rather
    than the rendered sentence.
    """
    args = record.args if isinstance(record.args, tuple) else ()
    for value in reversed(args):
        if isinstance(value, float):
            return value
    return 0.0


_counter: RateLimitCounter | None = None


def install() -> RateLimitStats:
    """Attach the counter to discord.http. Idempotent."""
    global _counter
    if _counter is None:
        _counter = RateLimitCounter()
        logging.getLogger("discord.http").addHandler(_counter)
    return _counter.stats


def snapshot() -> RateLimitStats:
    return install()
