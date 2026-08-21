"""Restores what a webhook cannot reproduce natively.

Each helper turns a lost property of the original message into a discreet
visual equivalent built on Discord markdown (`-# ` renders as grey subtext,
`>` as a quote) rather than on embeds, which would distort the layout.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

import discord

CONTENT_LIMIT = 2000
USERNAME_LIMIT = 80
CHANNEL_NAME_LIMIT = 100

# Below this, a body chunk is too small to be worth the annotations riding
# along with it, and the suffix goes into a message of its own instead.
MIN_BODY_BUDGET = 500

# Discord rejects webhook usernames containing these sequences.
FORBIDDEN_USERNAME_PARTS = ("discord", "clyde", "everyone", "here")

# The jump link is how a replayed message names its source. A resume reads it
# back out of the target channel, so the wording here is load bearing: change
# it and JUMP_PATTERN must follow.
JUMP_LABEL = "original message"
JUMP_PATTERN = re.compile(
    r"\[" + re.escape(JUMP_LABEL) + r"\]\(https://discord\.com/channels/\d+/\d+/(\d+)\)"
)

SYSTEM_LABELS = {
    discord.MessageType.pins_add: "pinned a message",
    discord.MessageType.recipient_add: "added a participant",
    discord.MessageType.recipient_remove: "removed a participant",
    discord.MessageType.channel_name_change: "renamed the thread",
}


def slugify_channel_name(raw: str, fallback: str = "promoted-thread") -> str:
    """Discord normalises channel names anyway, but better to own the result."""
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    slug = re.sub(r"[^a-z0-9\-_]+", "-", lowered)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return (slug or fallback)[:CHANNEL_NAME_LIMIT]


def safe_webhook_username(display_name: str) -> str:
    name = display_name.strip() or "User"
    lowered = name.lower()
    for part in FORBIDDEN_USERNAME_PARTS:
        if part in lowered:
            # A zero-width space is enough to clear the API rejection while
            # keeping the original casing and readability of the name.
            name = re.sub(
                re.escape(part),
                lambda match: match.group(0)[0] + "​" + match.group(0)[1:],
                name,
                flags=re.I,
            )
            lowered = name.lower()
    return name[:USERNAME_LIMIT]


def author_avatar(message: discord.Message) -> str | None:
    asset = getattr(message.author, "display_avatar", None)
    return asset.url if asset else None


def author_label(message: discord.Message) -> str:
    author = message.author
    return getattr(author, "display_name", None) or author.name


def timestamp_line(message: discord.Message) -> str:
    epoch = int(message.created_at.timestamp())
    line = f"-# <t:{epoch}:f>"
    if message.edited_at is not None:
        line += " (edited)"
    return line


def jump_line(message: discord.Message) -> str:
    return f"-# [{JUMP_LABEL}]({message.jump_url})"


def source_id_from_replay(content: str | None) -> int | None:
    """Recover the id of the source message from a replayed one.

    This is what makes a resume able to read its own output instead of
    trusting a cursor stored on disk.
    """
    if not content:
        return None
    match = JUMP_PATTERN.search(content)
    return int(match.group(1)) if match else None


def reply_line(
    message: discord.Message,
    resolved_url: str | None,
    resolved_author: str | None,
) -> str | None:
    """Webhooks cannot emit native replies, and replies never cross channels.

    So the link is rebuilt towards the already-replayed copy in the new channel.
    """
    if message.reference is None:
        return None

    target = resolved_author or "a message"
    if resolved_url:
        # Markdown link rather than a bare URL: Discord turns a raw message link
        # into what looks like a channel mention, which reads as if the reply
        # pointed at a channel instead of a message.
        return f"-# Replying to [**{target}**]({resolved_url})"

    referenced = message.reference.resolved
    if isinstance(referenced, discord.Message):
        extract = (referenced.content or "").replace("\n", " ").strip()
        if extract:
            extract = extract[:120] + ("..." if len(extract) > 120 else "")
            return f"-# Replying to **{referenced.author.display_name}**: {extract}"
        return f"-# Replying to **{referenced.author.display_name}**"
    return "-# Replying to a message outside this thread"


def reaction_line(reactions: list[dict]) -> str | None:
    if not reactions:
        return None
    chunks = []
    for entry in reactions:
        emoji = entry["emoji"]
        count = entry["count"]
        chunks.append(f"{emoji} x{count}" if count > 1 else f"{emoji}")
    return "-# Original reactions: " + "  ".join(chunks)


def system_line(message: discord.Message) -> str | None:
    label = SYSTEM_LABELS.get(message.type)
    if label is None:
        return None
    return f"-# {message.author.display_name} {label}"


def poll_embed(poll: object) -> discord.Embed | None:
    """A poll cannot be replayed as such, so its outcome is frozen instead."""
    question = getattr(getattr(poll, "question", None), "text", None)
    answers = getattr(poll, "answers", None)
    if question is None or answers is None:
        return None
    embed = discord.Embed(title=f"Poll: {question}")
    for answer in answers:
        text = getattr(getattr(answer, "media", None), "text", None) or "Answer"
        votes = getattr(answer, "vote_count", 0)
        embed.add_field(name=text, value=f"{votes} vote(s)", inline=False)
    embed.set_footer(text="Closed poll, restored from the original thread")
    return embed


def _fence_language(text: str, initial: str | None = None) -> str | None:
    """Language tag of the code fence left open at the end of the text.

    None means no fence is open. An empty string means an open fence with no
    language, which is not the same thing and must not be collapsed into None.
    """
    state = initial
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            state = None if state is not None else stripped[3:].strip()
    return state


def split_content(content: str, limit: int = CONTENT_LIMIT) -> list[str]:
    """Split without breaking words, and without cutting a code fence in half.

    A fence severed by a split breaks the rendering of both halves, so an open
    fence is closed before the cut and reopened after it with the same language
    tag.
    """
    if len(content) <= limit:
        return [content] if content else []

    parts: list[str] = []
    remaining = content
    open_language: str | None = None

    while True:
        reopen = f"```{open_language}\n" if open_language is not None else ""
        if len(reopen) + len(remaining) <= limit:
            parts.append(reopen + remaining)
            break

        # Room for the fence we may have to close at the end of this chunk.
        budget = limit - len(reopen) - 4
        window = remaining[:budget]
        if not window:
            parts.append(reopen + remaining)
            break

        cut = window.rfind("\n")
        if cut < len(window) // 2:
            cut = window.rfind(" ")
        if cut < len(window) // 2:
            cut = len(window)

        chunk = remaining[:cut]
        remaining = remaining[cut:].lstrip("\n")
        open_language = _fence_language(chunk, open_language)
        close = "\n```" if open_language is not None else ""
        parts.append(reopen + chunk + close)

        if not remaining:
            break

    return [part for part in parts if part]


def assemble(body: str, prefix_lines: Iterable[str], suffix_lines: Iterable[str]) -> list[str]:
    """Combine body and annotations while honouring the 2000 character cap.

    When the annotations are large enough to leave no usable room for the body,
    the suffix moves into a message of its own rather than overrunning the
    limit, which the API would refuse outright.
    """
    prefix = "\n".join(line for line in prefix_lines if line)
    suffix = "\n".join(line for line in suffix_lines if line)

    budget = CONTENT_LIMIT - len(prefix) - len(suffix) - 4
    detach_suffix = budget < MIN_BODY_BUDGET
    if detach_suffix:
        budget = CONTENT_LIMIT - len(prefix) - 2

    chunks = split_content(body or "", max(budget, 1)) or [""]
    assembled: list[str] = []
    for index, chunk in enumerate(chunks):
        pieces = []
        if index == 0 and prefix:
            pieces.append(prefix)
        if chunk:
            pieces.append(chunk)
        if not detach_suffix and index == len(chunks) - 1 and suffix:
            pieces.append(suffix)
        assembled.append("\n".join(pieces).strip())

    if detach_suffix and suffix:
        assembled.extend(split_content(suffix))

    return [item for item in assembled if item]
