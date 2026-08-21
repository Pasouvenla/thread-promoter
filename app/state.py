"""Resume checkpoint and archive manifest.

The checkpoint lets an interrupted migration restart (container restart,
network outage, cascading 429s) without re-posting what already went through.
It is a cache, not the source of truth: the target channel itself holds the
record of what was replayed, through the jump link carried by every message.
Losing or corrupting this file costs one extra read, never a duplicated
history.

The manifest keeps, outside Discord, everything the replay cannot restore:
per-user reaction detail, oversized attachments, third-party bot components.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

log = logging.getLogger("state")

# Files under /data hold conversation content and a live webhook token.
FILE_MODE = 0o600


@dataclass
class Checkpoint:
    thread_id: int
    guild_id: int
    target_channel_id: int | None = None
    webhook_id: int | None = None
    webhook_token: str | None = None
    header_message_id: int | None = None
    last_source_id: int | None = None
    source_total: int | None = None
    id_map: dict[str, int] = field(default_factory=dict)
    skipped_source_ids: list[int] = field(default_factory=list)
    pinned_source_ids: list[int] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    recovery_choice: str | None = None
    done: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        """Build from stored JSON, tolerating schema drift in both directions.

        An unknown key must not brick the command: the checkpoint is written
        after every message, so a file from an older or newer build is an
        ordinary thing to meet after an upgrade.
        """
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            log.warning("Ignoring unknown checkpoint fields: %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in data.items() if k in known})

    def reset(self) -> None:
        """Forget everything but the identity of the run.

        Only ever called once the target channel is proven gone, never on a
        cache miss or a transport error.
        """
        self.target_channel_id = None
        self.webhook_id = None
        self.webhook_token = None
        self.header_message_id = None
        self.last_source_id = None
        self.source_total = None
        self.id_map.clear()
        self.skipped_source_ids.clear()
        self.pinned_source_ids.clear()
        self.failures.clear()
        self.recovery_choice = None
        self.done = False

    def record_failure(self, entry: dict[str, Any]) -> None:
        self.failures = [
            f for f in self.failures if f.get("source_id") != entry["source_id"]
        ]
        self.failures.append(entry)

    def remember(self, source_id: int, new_id: int) -> None:
        self.id_map[str(source_id)] = new_id
        self.last_source_id = source_id

    def skip(self, source_id: int) -> None:
        """Record a message that was deliberately not emitted.

        Kept apart from id_map so that a lookup can never hand out a zero
        dressed up as a message id.
        """
        self.skipped_source_ids.append(source_id)
        self.last_source_id = source_id

    def translated(self, source_id: int | None) -> int | None:
        if source_id is None:
            return None
        return self.id_map.get(str(source_id))


class CheckpointStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, thread_id: int) -> Path:
        return self.directory / f"{thread_id}.json"

    def load(self, thread_id: int, guild_id: int) -> Checkpoint:
        path = self._path(thread_id)
        if not path.exists():
            return Checkpoint(thread_id=thread_id, guild_id=guild_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Checkpoint.from_dict(data)
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as exc:
            # Move it aside rather than delete: it is the only trace of what a
            # previous run did, and it may still be readable by hand.
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            quarantine = path.with_name(f"{thread_id}.corrupt-{stamp}.json")
            try:
                path.rename(quarantine)
                log.error("Unreadable checkpoint moved to %s (%s)", quarantine.name, exc)
            except OSError:
                log.exception("Unreadable checkpoint could not be moved aside")
            return Checkpoint(thread_id=thread_id, guild_id=guild_id)

    def save(self, checkpoint: Checkpoint) -> None:
        # Write, flush to disk, then rename. The rename alone is atomic against
        # a process crash but not against a host losing power, which is exactly
        # when a half-written checkpoint would matter most.
        path = self._path(checkpoint.thread_id)
        tmp = path.with_suffix(".tmp")
        payload = json.dumps(asdict(checkpoint), ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        tmp.replace(path)

    def discard(self, thread_id: int) -> None:
        self._path(thread_id).unlink(missing_ok=True)


class Manifest:
    """JSON archive of the migration, written as it goes.

    Cumulative across runs. A resume that finds nothing left to replay used to
    flush an empty payload over the archive, destroying the one record of what
    the migration could not carry over. The archive outlives any single run, so
    it is loaded before it is added to.
    """

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"thread": {}, "target_channel": {}, "messages": [], "warnings": []}

    def __init__(self, directory: Path, thread_id: int) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / f"{thread_id}.json"
        self.payload: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Unreadable manifest %s, starting a new one (%s)", self.path, exc)
            return self._empty()
        payload = self._empty()
        payload.update(loaded)
        return payload

    def set_header(self, thread: dict[str, Any], target: dict[str, Any]) -> None:
        if thread:
            self.payload["thread"] = thread
        if target:
            self.payload["target_channel"] = target

    def add_message(self, entry: dict[str, Any]) -> None:
        """Add or replace, then keep the archive in chronological order.

        Ordering is by source id rather than by index: a snowflake encodes its
        own creation time, while index restarts at one on every resume.
        """
        source_id = entry.get("source_id")
        messages = [m for m in self.payload["messages"] if m.get("source_id") != source_id]
        messages.append(entry)
        messages.sort(key=lambda m: m.get("source_id") or 0)
        self.payload["messages"] = messages

    def warn(self, message: str) -> None:
        if message not in self.payload["warnings"]:
            self.payload["warnings"].append(message)

    def flush(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        os.chmod(tmp, FILE_MODE)
        tmp.replace(self.path)
