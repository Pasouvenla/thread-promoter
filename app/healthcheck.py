"""Container healthcheck: is the gateway still alive?

A bot exposes no port to probe. What it does expose is progress: the heartbeat
file is refreshed while the gateway connection is up, so a stale file means
connected but stuck, which is the failure nobody notices from outside.

Exit 0 healthy, exit 1 anything else. Never a traceback: a crash here would
land in the container logs on every probe.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .bot import HEARTBEAT_INTERVAL, HEARTBEAT_PATH

# Three missed beats. Tight enough to catch a wedged gateway, loose enough to
# ride out one slow write.
MAX_AGE = HEARTBEAT_INTERVAL * 3


def is_healthy(path: Path = HEARTBEAT_PATH, max_age: float = MAX_AGE) -> bool:
    try:
        stamp = float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return 0 <= time.time() - stamp < max_age


def main() -> int:
    return 0 if is_healthy() else 1


if __name__ == "__main__":
    sys.exit(main())
