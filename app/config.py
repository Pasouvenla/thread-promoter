"""Centralised configuration, driven entirely by environment variables.

The switch surface is deliberately narrow. Every option here is a real trade
off between cost and fidelity; presentation details are fixed in code, because
each extra combination is a code path nobody will ever exercise.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("config")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float, minimum: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s is not a number (%r), falling back to %s", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s below the %s floor (%s), clamping", name, minimum, value)
        return minimum
    return value


def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s is not an integer (%r), falling back to %s", name, raw, default)
        return default
    if value < minimum:
        log.warning("%s below the %s floor (%s), clamping", name, minimum, value)
        return minimum
    return value


@dataclass(frozen=True)
class Config:
    # repr=False: a traceback capturing locals, or any log call formatting the
    # config, would otherwise write the bot token to the logs, and from there
    # to wherever the logs are shipped.
    token: str = field(
        default_factory=lambda: os.getenv("DISCORD_TOKEN", ""), repr=False
    )
    guild_ids: tuple[int, ...] = ()

    # Emission pacing. The observed per-channel ceiling for webhook sends is
    # around 30 messages per minute, and that bucket is shared with reactions
    # and pins. 2.0 s leaves comfortable headroom. The floor is non-zero
    # because a zero delay walks straight into a global rate limit.
    message_delay: float = field(
        default_factory=lambda: _env_float("PROMOTER_MESSAGE_DELAY", 2.0, 0.25)
    )
    reaction_delay: float = field(
        default_factory=lambda: _env_float("PROMOTER_REACTION_DELAY", 0.30, 0.10)
    )

    # Off by default. A bot cannot react on someone else's behalf, so replayed
    # reactions all end up attributed to the bot: fidelity in appearance only,
    # paid for with one API call per reaction out of the same bucket as the
    # messages. Who reacted with what is preserved as text under each message
    # and in full in the manifest, which is honest about what it is.
    replay_reactions: bool = field(
        default_factory=lambda: _env_bool("PROMOTER_REPLAY_REACTIONS", False)
    )
    replay_attachments: bool = field(
        default_factory=lambda: _env_bool("PROMOTER_REPLAY_ATTACHMENTS", True)
    )

    # Persistence
    state_dir: Path = field(
        default_factory=lambda: Path(os.getenv("PROMOTER_STATE_DIR", "/data/state"))
    )
    manifest_dir: Path = field(
        default_factory=lambda: Path(os.getenv("PROMOTER_MANIFEST_DIR", "/data/manifests"))
    )
    attachment_cache: Path = field(
        default_factory=lambda: Path(os.getenv("PROMOTER_ATTACHMENT_CACHE", "/data/attachments"))
    )
    export_dir: Path = field(
        default_factory=lambda: Path(os.getenv("PROMOTER_EXPORT_DIR", "/data/exports"))
    )
    keep_attachment_copies: bool = field(
        default_factory=lambda: _env_bool("PROMOTER_KEEP_ATTACHMENT_COPIES", True)
    )

    progress_every: int = field(
        default_factory=lambda: _env_int("PROMOTER_PROGRESS_EVERY", 25, 1)
    )

    @classmethod
    def load(cls) -> Config:
        raw_guilds = os.getenv("DISCORD_GUILD_IDS", "").replace(";", ",")
        parts = [part.strip() for part in raw_guilds.split(",") if part.strip()]
        for part in parts:
            if not part.isdigit():
                log.warning("Ignoring non-numeric guild id %r", part)
        guilds = tuple(int(part) for part in parts if part.isdigit())

        cfg = cls(guild_ids=guilds)
        if not cfg.token:
            raise RuntimeError("DISCORD_TOKEN is missing from the environment.")

        # 0700: these directories hold conversation content, a live webhook
        # token and the attachment bytes. Nothing else on the host has any
        # business reading them.
        for directory in (
            cfg.state_dir,
            cfg.manifest_dir,
            cfg.attachment_cache,
            cfg.export_dir,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
                directory.chmod(0o700)
            except OSError as exc:
                # The usual cause is a host directory owned by someone else,
                # which is invisible on a Docker Desktop bind mount and obvious
                # on a Linux host. Say what to do rather than what broke.
                raise RuntimeError(
                    f"{directory} is not usable by uid {os.getuid()}: {exc}. "
                    f"The data volume must belong to the container user, "
                    f"for instance chown -R 1000:1000 on the host directory."
                ) from exc
        return cfg
