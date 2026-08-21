"""Doubles for the discord.py surface the promoter actually touches.

Deliberately hand written rather than MagicMock: the point of these tests is to
notice when a call the real API would reject slips in, and a mock that answers
everything notices nothing.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import discord
import pytest

from app.config import Config


class ThreadWriteAttempt(AssertionError):
    """Raised when anything tries to write to the source thread."""


class FakeAsset:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeAuthor:
    def __init__(self, author_id: int, name: str) -> None:
        self.id = author_id
        self.name = name
        self.display_name = name
        self.display_avatar = FakeAsset(f"https://cdn.example/{author_id}.png")

    def __str__(self) -> str:
        return f"{self.name}#0001"


class FakeFlags:
    suppress_embeds = False


class FakeReference:
    def __init__(self, message_id: int, resolved=None) -> None:
        self.message_id = message_id
        self.resolved = resolved


class FakeReaction:
    def __init__(self, emoji: str, count: int, users: list[str] | None = None) -> None:
        self.emoji = emoji
        self.count = count
        self._users = users or []

    def users(self, limit: int = 100):
        async def gen():
            for user in self._users[:limit]:
                yield user

        return gen()


class FakeAttachment:
    def __init__(self, attachment_id: int, filename: str, size: int, payload: bytes = b"x") -> None:
        self.id = attachment_id
        self.filename = filename
        self.size = size
        self.description = None
        self.payload = payload
        self.fail = False

    def is_spoiler(self) -> bool:
        return False

    async def read(self) -> bytes:
        if self.fail:
            raise discord.HTTPException(_Response(500), "read failed")
        return self.payload


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test"


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        author: FakeAuthor,
        content: str = "",
        *,
        created_at: dt.datetime | None = None,
        message_type: discord.MessageType = discord.MessageType.default,
        attachments: list[FakeAttachment] | None = None,
        reactions: list[FakeReaction] | None = None,
        reference: FakeReference | None = None,
        pinned: bool = False,
        guild_id: int = 500,
        channel_id: int = 900,
    ) -> None:
        self.id = message_id
        self.author = author
        self.content = content
        self.created_at = created_at or dt.datetime(2024, 5, 1, 12, 0, tzinfo=dt.UTC)
        self.edited_at = None
        self.type = message_type
        self.attachments = attachments or []
        self.stickers = []
        self.embeds = []
        self.reactions = reactions or []
        self.reference = reference
        self.pinned = pinned
        self.flags = FakeFlags()
        self.jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


class FakeSent:
    def __init__(self, message_id: int) -> None:
        self.id = message_id


class FakeWebhook:
    def __init__(self, channel: FakeChannel | None = None) -> None:
        self.channel = channel
        self.id = 7000
        self.token = "webhook-token"
        self.sent: list[dict] = []
        self.edited: list[tuple[int, dict]] = []
        self.deleted = False
        self.next_id = 10_000
        self.fail_on_source: set[int] = set()
        self.failures_left = 0

    async def send(self, **kwargs) -> FakeSent:
        if self.failures_left:
            self.failures_left -= 1
            raise discord.HTTPException(_Response(400), "forced failure")
        self.next_id += 1
        self.sent.append(kwargs)
        # Mirror the emission into the channel: resuming reads the channel back,
        # so a double that does not record what it sent tests nothing.
        if self.channel is not None:
            echo = FakeMessage(self.next_id, FakeAuthor(1, "webhook"), kwargs.get("content") or "")
            self.channel.messages.append(echo)
        return FakeSent(self.next_id)

    async def edit_message(self, message_id: int, **kwargs) -> None:
        self.edited.append((message_id, kwargs))

    async def delete(self, reason: str | None = None) -> None:
        self.deleted = True


class FakePartialMessage:
    def __init__(self, channel: FakeChannel, message_id: int) -> None:
        self.channel = channel
        self.id = message_id

    async def add_reaction(self, emoji) -> None:
        self.channel.reactions.append((self.id, str(emoji)))

    async def pin(self, reason: str | None = None) -> None:
        self.channel.pinned.append(self.id)

    async def edit(self, **kwargs) -> None:
        self.channel.edits.append((self.id, kwargs))


class FakeChannel(discord.TextChannel):
    """Subclasses the real type on purpose.

    The promoter checks isinstance before writing into a recorded target, and a
    double that sidesteps that check would let a broken resolution pass.
    discord.TextChannel.__init__ needs a connection state, so it is skipped;
    every method the promoter calls is overridden below.
    """

    def __init__(self, channel_id: int = 900, name: str = "promoted") -> None:
        # mention is a read-only property on the real type, derived from the id.
        self.id = channel_id
        self.name = name
        self.messages: list[FakeMessage] = []
        self.plain_sends: list[dict] = []
        self.reactions: list[tuple[int, str]] = []
        self.pinned: list[int] = []
        self.edits: list[tuple[int, dict]] = []
        self.webhook = FakeWebhook(self)
        self.next_id = 20_000

    async def send(self, content: str | None = None, **kwargs) -> FakeSent:
        self.next_id += 1
        self.plain_sends.append({"content": content, **kwargs})
        self.messages.append(FakeMessage(self.next_id, FakeAuthor(1, "bot"), content or ""))
        return FakeSent(self.next_id)

    async def create_webhook(self, name: str, reason: str | None = None) -> FakeWebhook:
        return self.webhook

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return FakePartialMessage(self, message_id)

    def history(self, limit=None, oldest_first: bool = True):
        ordered = self.messages if oldest_first else list(reversed(self.messages))
        if limit is not None:
            ordered = ordered[:limit]

        async def gen():
            for message in ordered:
                yield message

        return gen()


class FakeGuild:
    def __init__(self, guild_id: int = 500) -> None:
        self.id = guild_id
        self.filesize_limit = 10 * 1024 * 1024
        self.default_role = "everyone"
        self.me = FakeAuthor(1, "promoter-bot")
        self.created_channels: list[dict] = []
        self.channel = FakeChannel()

    async def create_text_channel(self, **kwargs) -> FakeChannel:
        self.created_channels.append(kwargs)
        self.channel.name = kwargs.get("name", "promoted")
        return self.channel

    def get_channel(self, channel_id: int):
        """Always a miss, like a cache that never saw a channel it cannot view.

        If the promoter ever resolves its target from here again, the tests
        that assert no second channel is created will fail.
        """
        return None

    def get_member(self, member_id: int):
        return FakeAuthor(member_id, f"member{member_id}")


class FakeParent:
    def __init__(self, guild: FakeGuild) -> None:
        self.name = "general"
        self.mention = "<#42>"
        self.category = None
        self.overwrites = {}
        self.guild = guild
        self.starter: FakeMessage | None = None

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if self.starter is None:
            raise discord.NotFound(_Response(404), "no starter")
        return self.starter


class FakeThread:
    """The source. Every write method is a tripwire."""

    def __init__(self, guild: FakeGuild, thread_id: int = 800) -> None:
        self.id = thread_id
        self.name = "A long discussion"
        self.guild = guild
        self.parent = FakeParent(guild)
        self.created_at = dt.datetime(2023, 1, 1, tzinfo=dt.UTC)
        self.messages: list[FakeMessage] = []
        self._private = False

    def is_private(self) -> bool:
        return self._private

    async def fetch_members(self):
        return []

    async def fetch_message(self, message_id: int) -> FakeMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise discord.NotFound(_Response(404), "unknown message")

    def history(self, limit=None, after=None, oldest_first: bool = True):
        selected = [m for m in self.messages if after is None or m.id > after.id]
        if not oldest_first:
            selected = list(reversed(selected))

        async def gen():
            for message in selected:
                yield message

        return gen()

    # Tripwires. The source thread is strictly read-only.
    async def send(self, *args, **kwargs):
        raise ThreadWriteAttempt("Thread.send was called on the source thread")

    async def edit(self, *args, **kwargs):
        raise ThreadWriteAttempt("Thread.edit was called on the source thread")

    async def delete(self, *args, **kwargs):
        raise ThreadWriteAttempt("Thread.delete was called on the source thread")

    async def purge(self, *args, **kwargs):
        raise ThreadWriteAttempt("Thread.purge was called on the source thread")


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.guild = guild
        self.missing_channels: set[int] = set()
        self.unreachable = False

    async def fetch_channel(self, channel_id: int):
        if self.unreachable:
            raise discord.DiscordServerError(_Response(503), "gateway down")
        if channel_id in self.missing_channels:
            raise discord.NotFound(_Response(404), "unknown channel")
        return self.guild.channel


@pytest.fixture
def config(tmp_path: Path, monkeypatch) -> Config:
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("PROMOTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PROMOTER_MANIFEST_DIR", str(tmp_path / "manifests"))
    monkeypatch.setenv("PROMOTER_ATTACHMENT_CACHE", str(tmp_path / "attachments"))
    monkeypatch.setenv("PROMOTER_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("PROMOTER_MESSAGE_DELAY", "0.25")
    monkeypatch.setenv("PROMOTER_REACTION_DELAY", "0.10")
    return Config.load()


@pytest.fixture
def guild() -> FakeGuild:
    return FakeGuild()


@pytest.fixture
def thread(guild: FakeGuild) -> FakeThread:
    return FakeThread(guild)


@pytest.fixture
def bot(guild: FakeGuild) -> FakeBot:
    return FakeBot(guild)


@pytest.fixture
def invoker() -> FakeAuthor:
    author = FakeAuthor(99, "operator")
    author.mention = "<@99>"
    return author


@pytest.fixture(autouse=True)
def partial_webhook(monkeypatch, guild):
    """Rebuild a stored webhook without touching the network.

    A resume that did not finish cleanly still holds a webhook id and token, and
    reattaches through Webhook.partial. Letting the real one through would need
    a connection state; skipping it would leave the resume path untested. This
    records the call instead.
    """
    calls: list[tuple] = []

    def fake_partial(webhook_id, token, *, client=None, session=None, bot_token=None):
        calls.append((webhook_id, token, client))
        return guild.channel.webhook

    monkeypatch.setattr(discord.Webhook, "partial", staticmethod(fake_partial))
    guild.partial_calls = calls
    return calls


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Pacing is correctness, not speed, and it is asserted separately."""
    import asyncio as _asyncio

    async def instant(_seconds):
        return None

    monkeypatch.setattr(_asyncio, "sleep", instant)
