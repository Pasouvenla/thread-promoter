"""Core logic for promoting a thread into a dedicated channel.

Sequence:
  1. preflight checks and target resolution
  2. channel creation, inheriting the parent channel permissions
  3. replay webhook creation
  4. header message, which doubles as the progress indicator
  5. chronological replay of the history
  6. pin restoration
  7. recovery prompt for whatever failed
  8. archive manifest write

The source thread is strictly read-only. Nothing is written to it, nothing is
locked, archived or deleted. What happens to the original afterwards is a server
administration decision, and keeping it untouched is what makes an unsatisfying
run repeatable.

Resuming reads the target channel rather than trusting the checkpoint. Every
replayed message carries a jump link naming its source, so the channel is its
own record of what was done. The checkpoint is an optimisation: losing it costs
one extra read, never a duplicated history.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
from dataclasses import dataclass, field

import aiohttp
import discord

from . import ratelimit
from .config import Config
from .renderers import (
    assemble,
    author_avatar,
    author_label,
    jump_line,
    poll_embed,
    reaction_line,
    reply_line,
    safe_webhook_username,
    slugify_channel_name,
    source_id_from_replay,
    system_line,
    timestamp_line,
)
from .state import Checkpoint, CheckpointStore, Manifest
from .views import FORCE, LEAVE, RecoveryView, failure_report

log = logging.getLogger("promoter")

# Worth another attempt: the far end wobbled, nothing is wrong with the code.
TRANSIENT = (aiohttp.ClientError, asyncio.TimeoutError, discord.DiscordServerError)

# Worth skipping one message and carrying on. Anything outside this set is a
# programming error and must reach the operator instead of being buried under a
# placeholder in a channel nobody is watching.
RECOVERABLE = (discord.HTTPException, aiohttp.ClientError, asyncio.TimeoutError, OSError)

RETRY_BACKOFF = (2.0, 5.0, 12.0)

# Messages between checkpoint writes. Saving after every single one rewrites
# the whole file each time: on a thirty thousand message thread the checkpoint
# grows past a megabyte and the run ends up writing tens of gigabytes for
# nothing, with one fsync per message. Batching is safe here precisely because
# the checkpoint is not the authority: a resume reads the channel, so losing a
# few entries costs one extra read and never a duplicate.
CHECKPOINT_EVERY = 25

# How far back to look in the target channel for the resume point. Generous
# enough to step over a header, a failure report and a recovery prompt.
RESUME_LOOKBACK = 200

REPLAYABLE_TYPES = (
    discord.MessageType.default,
    discord.MessageType.reply,
)

# Discord puts one of these at the head of a thread created from a message. It
# carries no content of its own, only a reference to the message it was started
# from, which the replay already fetches from the parent channel. Replaying it
# too produces an empty duplicate of the opening post.
SKIPPED_TYPES = (discord.MessageType.thread_starter_message,)


@dataclass
class Progress:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    recovered: int = 0
    attachments: int = 0
    attachments_lost: int = 0
    reactions: int = 0


@dataclass
class Payload:
    """Everything needed to emit one replayed message, at one fidelity level."""

    blocks: list[str] = field(default_factory=list)
    files: list[discord.File] = field(default_factory=list)
    embeds: list[discord.Embed] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.blocks and not self.files and not self.embeds


class PromotionError(RuntimeError):
    pass


class ThreadPromoter:
    def __init__(
        self,
        bot: discord.Client,
        config: Config,
        thread: discord.Thread,
        invoker: discord.Member,
        *,
        target_name: str | None = None,
        full_replay: bool = True,
        visible_to: discord.Role | None = None,
    ) -> None:
        self.bot = bot
        self.config = config
        self.thread = thread
        self.invoker = invoker
        self.target_name = target_name
        self.full_replay = full_replay
        self.visible_to = visible_to

        self.store = CheckpointStore(config.state_dir)
        self.checkpoint: Checkpoint = self.store.load(thread.id, thread.guild.id)
        self.manifest = Manifest(config.manifest_dir, thread.id)
        self.progress = Progress()

        self.channel: discord.TextChannel | None = None
        self.webhook: discord.Webhook | None = None
        self.author_labels: dict[int, str] = {}

        # A run lasts tens of minutes. Without a way out, the only way to stop
        # one is to kill the container mid-message.
        self.abort_requested = False
        self.rate_limits = ratelimit.install()
        # Messages already in the channel before this run started, so a resume
        # reports overall progress rather than restarting the count at zero.
        self.already_replayed = 0

    def request_abort(self) -> None:
        """Ask the replay to stop at the next message boundary.

        Deliberately not a task cancellation: stopping between two messages
        leaves a checkpoint that /promote-resume can pick up, whereas cancelling
        mid-send leaves a half-emitted message and no record of it.
        """
        self.abort_requested = True

    # ------------------------------------------------------------------ setup

    async def _fetch_target(self) -> discord.TextChannel | None:
        """Resolve the recorded target channel, or prove it is gone.

        Guild.get_channel reads a cache that does not hold channels the bot
        cannot view, and a private thread target is exactly that. Treating a
        cache miss as a deleted channel is what would replay an entire history
        into a second channel, so the question is asked of the API and only an
        explicit 404 counts as an answer.
        """
        channel_id = self.checkpoint.target_channel_id
        if not channel_id:
            return None
        try:
            channel = await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            log.info("Target channel %s is gone, checkpoint reset", channel_id)
            self.checkpoint.reset()
            self.store.save(self.checkpoint)
            return None
        except (discord.Forbidden, *TRANSIENT) as exc:
            # Cannot prove anything either way. Refusing to act beats starting
            # over on top of a channel that may be full of replayed history.
            raise PromotionError(
                f"The target channel {channel_id} could not be checked ({exc}). "
                "Refusing to start over blind: fix access, or delete the channel "
                "and run /promote-forget."
            ) from exc

        if not isinstance(channel, discord.TextChannel):
            raise PromotionError(
                f"The recorded target {channel_id} is not a text channel any more."
            )
        log.info("Resuming into existing channel %s", channel.id)
        return channel

    def _target_overwrites(self) -> dict:
        """Start closed, so a replay does not flood a whole server.

        Pouring thirty thousand messages into an open channel marks it unread
        for everyone, for hours. Messages go out silent, which suppresses the
        push notification but not the unread badge, so the only real answer is
        to keep the door shut while the replay runs.

        Inheriting the parent's overwrites happens at the end, when a human
        decides the channel is ready. Until then only the bot, whoever asked
        for the promotion, and one chosen role can see it.
        """
        guild = self.thread.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        me = guild.me
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True,
                manage_webhooks=True, embed_links=True, attach_files=True,
                add_reactions=True, read_message_history=True,
            )
        invoker = guild.get_member(self.invoker.id) or self.invoker
        overwrites[invoker] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True
        )
        if self.visible_to is not None:
            overwrites[self.visible_to] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )
        return overwrites

    async def _private_overwrites(self, overwrites: dict) -> dict:
        """Carry a private thread's audience over, without widening it.

        The bot is added explicitly: a channel it cannot see is a channel it
        cannot find again after a restart.
        """
        overwrites[self.thread.guild.default_role] = discord.PermissionOverwrite(
            view_channel=False
        )
        me = self.thread.guild.me
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_messages=True
            )
        for member in await self.thread.fetch_members():
            resolved = self.thread.guild.get_member(member.id)
            if resolved is not None:
                overwrites[resolved] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                )
        return overwrites

    async def _resolve_channel(self) -> discord.TextChannel:
        existing = await self._fetch_target()
        if existing is not None:
            return existing

        parent = self.thread.parent
        if parent is None:
            raise PromotionError("The parent channel of this thread cannot be reached.")

        name = slugify_channel_name(self.target_name or self.thread.name)
        overwrites = self._target_overwrites()
        if self.thread.is_private():
            # A private thread's participants are carried over so they keep
            # access once the channel is opened up.
            overwrites = await self._private_overwrites(overwrites)

        topic = (
            f"Promoted from the \"{self.thread.name}\" thread in #{parent.name} "
            f"on {discord.utils.utcnow():%Y-%m-%d}"
        )[:1024]

        channel = await self.thread.guild.create_text_channel(
            name=name,
            category=parent.category,
            topic=topic,
            overwrites=overwrites,
            reason=f"Thread {self.thread.id} promoted by {self.invoker}",
        )
        self.checkpoint.target_channel_id = channel.id
        self.store.save(self.checkpoint)
        return channel

    async def _resolve_webhook(self) -> discord.Webhook:
        if self.checkpoint.webhook_id and self.checkpoint.webhook_token:
            return discord.Webhook.partial(
                self.checkpoint.webhook_id,
                self.checkpoint.webhook_token,
                client=self.bot,
            )
        webhook = await self.channel.create_webhook(
            name="Thread replay",
            reason=f"Replaying history of thread {self.thread.id}",
        )
        self.checkpoint.webhook_id = webhook.id
        self.checkpoint.webhook_token = webhook.token
        self.store.save(self.checkpoint)
        return webhook

    async def _retire_webhook(self) -> None:
        """Close the door once the replay is done.

        The stored token can post as any replayed author for as long as it
        lives, and it lives in a file on disk.
        """
        if self.webhook is None or not self.checkpoint.webhook_id:
            return
        try:
            await self.webhook.delete(reason="Replay finished")
        except (discord.HTTPException, *TRANSIENT) as exc:
            self.manifest.warn(f"Replay webhook could not be deleted: {exc}")
            return
        self.checkpoint.webhook_id = None
        self.checkpoint.webhook_token = None
        self.webhook = None
        self.store.save(self.checkpoint)

    # ----------------------------------------------------------------- replay

    async def _reconcile_with_channel(self) -> int | None:
        """Ask the target channel what it already holds.

        Returns the id of the last source message that made it across. Also
        rebuilds the source-to-target map when the checkpoint has lost it, so
        replies keep pointing somewhere real.
        """
        last_source_id: int | None = None
        async for message in self.channel.history(
            limit=RESUME_LOOKBACK, oldest_first=False
        ):
            found = source_id_from_replay(message.content)
            if found is not None:
                last_source_id = found
                break

        if last_source_id is None:
            return None

        # Batched checkpoints mean the map can lag behind the channel after a
        # hard stop. Rebuild whenever the last replayed message is missing from
        # it, not only when it is empty, or replies would point nowhere.
        if not self.checkpoint.id_map or str(last_source_id) not in self.checkpoint.id_map:
            log.info("Rebuilding the id map from channel %s", self.channel.id)
            async for message in self.channel.history(limit=None, oldest_first=True):
                found = source_id_from_replay(message.content)
                if found is not None:
                    self.checkpoint.id_map.setdefault(str(found), message.id)

        return last_source_id

    async def _iter_source(self, after_id: int | None):
        """Walk the thread in order, streaming rather than buffering.

        A thread of several thousand messages held in memory as discord.py
        objects runs into hundreds of megabytes, in a container that declares
        no limit.
        """
        if after_id is None:
            # The message a thread was started from lives in the parent channel
            # and carries the same id as the thread, so it never shows up in the
            # thread history. A forum post has no such message: its opening post
            # is inside the thread already, and ForumChannel has no
            # fetch_message, hence the AttributeError.
            try:
                yield await self.thread.parent.fetch_message(self.thread.id)
            except (discord.NotFound, discord.Forbidden, AttributeError):
                log.info("No usable starter message for this thread.")

        after = discord.Object(id=after_id) if after_id else None
        async for message in self.thread.history(
            limit=None, after=after, oldest_first=True
        ):
            yield message

    async def _collect_reactions(self, message: discord.Message) -> list[dict]:
        collected: list[dict] = []
        for reaction in message.reactions:
            entry = {
                "emoji": str(reaction.emoji),
                "count": reaction.count,
                "users": [],
            }
            with contextlib.suppress(discord.HTTPException):
                entry["users"] = [str(user) async for user in reaction.users(limit=100)]
            collected.append(entry)
        return collected

    async def _build_files(
        self, message: discord.Message
    ) -> tuple[list[discord.File], list[str]]:
        files: list[discord.File] = []
        lost: list[str] = []
        limit = self.thread.guild.filesize_limit

        for attachment in message.attachments:
            if attachment.size > limit:
                lost.append(f"{attachment.filename} ({attachment.size / 1_048_576:.1f} MB)")
                self.progress.attachments_lost += 1
                continue
            try:
                # CDN URLs are signed (ex, is, hm) and short-lived, so the bytes
                # are read right away instead of storing the link.
                payload = await attachment.read()
            except (discord.HTTPException, *TRANSIENT) as exc:
                lost.append(f"{attachment.filename} (read failed: {exc})")
                self.progress.attachments_lost += 1
                continue

            if self.config.keep_attachment_copies:
                cache_dir = self.config.attachment_cache / str(self.thread.id)
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_dir.chmod(0o700)
                cached = cache_dir / f"{attachment.id}_{attachment.filename}"
                cached.write_bytes(payload)
                cached.chmod(0o600)

            files.append(
                discord.File(
                    io.BytesIO(payload),
                    filename=attachment.filename,
                    description=attachment.description,
                    spoiler=attachment.is_spoiler(),
                )
            )
            self.progress.attachments += 1

        # Stickers cannot travel through a webhook, so they are re-sent as images.
        for sticker in message.stickers:
            try:
                asset = await sticker.read()
            except (discord.HTTPException, discord.NotFound, ValueError, *TRANSIENT):
                lost.append(f"sticker {sticker.name}")
                continue
            files.append(discord.File(io.BytesIO(asset), filename=f"{sticker.name}.png"))

        return files, lost

    def _source_jump_url(self, source_id: int) -> str:
        return f"{self._source_url()}/{source_id}"

    def _new_jump_url(self, new_id: int) -> str:
        return (
            f"https://discord.com/channels/{self.thread.guild.id}/"
            f"{self.channel.id}/{new_id}"
        )

    def _annotations(
        self, message: discord.Message, reactions: list[dict], lost: list[str]
    ) -> tuple[list[str], list[str]]:
        prefix: list[str] = []
        referenced_id = message.reference.message_id if message.reference else None
        mapped = self.checkpoint.translated(referenced_id)
        line = reply_line(
            message,
            self._new_jump_url(mapped) if mapped else None,
            self.author_labels.get(referenced_id) if referenced_id else None,
            self._source_jump_url(referenced_id) if referenced_id else None,
        )
        if line:
            prefix.append(line)

        suffix: list[str] = []
        if lost:
            suffix.append("-# Attachment(s) not carried over: " + ", ".join(lost))
        reaction_summary = reaction_line(reactions)
        if reaction_summary:
            suffix.append(reaction_summary)
        suffix.append(timestamp_line(message))
        # Always last, always present: this is the resume anchor.
        suffix.append(jump_line(message))
        return prefix, suffix

    async def _compose(
        self,
        message: discord.Message,
        reactions: list[dict],
        *,
        with_attachments: bool = True,
        with_embeds: bool = True,
        max_content: int | None = None,
        note: str | None = None,
    ) -> Payload:
        """Build one emission payload at a given fidelity level."""
        files: list[discord.File] = []
        lost: list[str] = []
        if with_attachments and self.config.replay_attachments:
            files, lost = await self._build_files(message)
        elif message.attachments or message.stickers:
            lost = [a.filename for a in message.attachments] + [
                f"sticker {s.name}" for s in message.stickers
            ]

        embeds: list[discord.Embed] = []
        if with_embeds:
            embeds = [embed for embed in message.embeds if embed.type == "rich"][:10]
            poll = getattr(message, "poll", None)
            if poll is not None:
                built = poll_embed(poll)
                if built is not None:
                    embeds = [*embeds, built][:10]

        body = message.content or ""
        if max_content is not None and len(body) > max_content:
            body = body[:max_content] + "..."

        prefix, suffix = self._annotations(message, reactions, lost)
        if note:
            suffix.append(f"-# {note}")

        return Payload(assemble(body, prefix, suffix), files, embeds, lost)

    async def _send_with_retry(self, **kwargs) -> discord.WebhookMessage:
        """Absorb transient failures before declaring a message lost.

        discord.py already handles 429s; this covers gateway hiccups, upstream
        5xx and network drops, which are the bulk of what fails on a run
        measured in tens of minutes.
        """
        last: Exception | None = None
        for attempt, delay in enumerate((0.0, *RETRY_BACKOFF)):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.webhook.send(**kwargs)
            except (discord.DiscordServerError, *TRANSIENT) as exc:
                last = exc
                log.warning("Transient send failure (attempt %s): %s", attempt + 1, exc)
            except discord.HTTPException as exc:
                # 4xx other than rate limiting will not fix itself on retry.
                raise exc
        raise last if last else RuntimeError("send failed without an exception")

    async def _place_holder(self, message: discord.Message) -> int | None:
        """Keep the message's slot in the channel so ordering survives.

        A webhook message can be edited later, so the placeholder becomes the
        recovered message in place rather than being appended at the end. It
        carries the jump link like any other, so a resume counts it as done.
        """
        prefix, suffix = self._annotations(message, [], [])
        content = "\n".join(
            [*prefix, "-# Message could not be replayed, recovery pending.", *suffix]
        )[:2000]
        try:
            sent = await self.webhook.send(
                content=content,
                username=safe_webhook_username(author_label(message)),
                avatar_url=author_avatar(message),
                allowed_mentions=discord.AllowedMentions.none(),
                silent=True,
                wait=True,
            )
            return sent.id
        except (discord.HTTPException, *TRANSIENT) as exc:
            log.warning("Placeholder failed for %s: %s", message.id, exc)
            return None

    async def _replay_one(self, message: discord.Message, index: int) -> None:
        if message.type in SKIPPED_TYPES:
            self.progress.skipped += 1
            self.checkpoint.skip(message.id)
            return

        if message.type not in REPLAYABLE_TYPES:
            line = system_line(message)
            if line is None:
                self.progress.skipped += 1
                self.checkpoint.skip(message.id)
                return
            # A system line still carries the jump link, so it anchors a resume.
            await self._send_with_retry(
                content="\n".join([line, jump_line(message)]),
                username="Thread log",
                allowed_mentions=discord.AllowedMentions.none(),
                silent=True,
                wait=True,
            )
            self.progress.skipped += 1
            self.checkpoint.skip(message.id)
            return

        self.author_labels[message.id] = author_label(message)

        reactions = await self._collect_reactions(message)
        payload = await self._compose(message, reactions)
        if payload.lost:
            self.manifest.warn(f"Message {message.id}: {', '.join(payload.lost)}")

        if payload.is_empty():
            self.progress.skipped += 1
            self.checkpoint.skip(message.id)
            return

        new_id = await self._emit(message, payload)
        self.checkpoint.remember(message.id, new_id)
        self.progress.sent += 1

        if message.pinned:
            self.checkpoint.pinned_source_ids.append(message.id)

        if self.config.replay_reactions and reactions:
            await self._replay_reactions(new_id, reactions)

        self.manifest.add_message(
            self._manifest_entry(message, index, reactions, new_id=new_id, lost=payload.lost)
        )

    def _manifest_entry(
        self,
        message: discord.Message,
        index: int,
        reactions: list[dict],
        *,
        new_id: int | None = None,
        lost: list[str] | None = None,
    ) -> dict:
        return {
            "source_id": message.id,
            "new_id": new_id,
            "index": index,
            "author": str(message.author),
            "author_id": message.author.id,
            "author_display": author_label(message),
            "avatar_url": author_avatar(message),
            "created_at": message.created_at,
            "edited_at": message.edited_at,
            "pinned": message.pinned,
            "content": message.content,
            "attachments": [a.filename for a in message.attachments],
            "attachments_lost": lost or [],
            "reactions": reactions,
            "reply_to": message.reference.message_id if message.reference else None,
        }

    async def _emit(self, message: discord.Message, payload: Payload) -> int:
        username = safe_webhook_username(author_label(message))
        avatar = author_avatar(message)
        first_new_id: int | None = None

        # One list, one length. Deriving is_last from payload.blocks while
        # iterating over a fallback is how attachments used to vanish on a
        # message with no text.
        blocks = payload.blocks or [""]
        for index, block in enumerate(blocks):
            is_last = index == len(blocks) - 1
            sent = await self._send_with_retry(
                content=block or None,
                username=username,
                avatar_url=avatar,
                files=payload.files if is_last else [],
                embeds=payload.embeds if is_last else [],
                allowed_mentions=discord.AllowedMentions.none(),
                suppress_embeds=message.flags.suppress_embeds,
                silent=True,
                wait=True,
            )
            if first_new_id is None:
                first_new_id = sent.id
            if not is_last:
                await asyncio.sleep(self.config.message_delay)

        if first_new_id is None:
            raise RuntimeError("no message emitted")
        return first_new_id

    async def _replay_reactions(self, new_id: int, reactions: list[dict]) -> None:
        """A bot cannot react on someone else's behalf, so it reacts itself.

        Per-user detail is preserved in the subtext line and in the manifest.
        """
        partial = self.channel.get_partial_message(new_id)
        for entry in reactions:
            try:
                await partial.add_reaction(entry["emoji"])
                self.progress.reactions += 1
            except (discord.HTTPException, discord.NotFound, TypeError, *TRANSIENT):
                self.manifest.warn(
                    f"Reaction {entry['emoji']} could not be replayed on {new_id} "
                    "(external emoji not available to the bot)."
                )
            await asyncio.sleep(self.config.reaction_delay)

    async def _handle_failure(
        self, message: discord.Message, index: int, exc: Exception
    ) -> None:
        placeholder_id = await self._place_holder(message)
        entry = {
            "source_id": message.id,
            "index": index,
            "author": author_label(message),
            "created_at_epoch": int(message.created_at.timestamp()),
            "jump_url": message.jump_url,
            "placeholder_id": placeholder_id,
            "reason": f"{type(exc).__name__}: {exc}"[:200],
        }
        self.checkpoint.record_failure(entry)
        # The cursor still advances: the failure is tracked by id, so a resume
        # does not have to re-walk the whole history to find it again.
        if placeholder_id:
            self.checkpoint.remember(message.id, placeholder_id)
        else:
            self.checkpoint.skip(message.id)
        self.progress.failed += 1
        self.manifest.warn(f"Message {message.id} failed: {entry['reason']}")

    # --------------------------------------------------------------- recovery

    async def recover(self, policy: str) -> tuple[int, int]:
        """Apply one policy to every failed message. Returns (recovered, left)."""
        if policy == LEAVE or not self.checkpoint.failures:
            return 0, len(self.checkpoint.failures)

        ladder = [
            {"with_attachments": True, "with_embeds": True},
            {"with_attachments": False, "with_embeds": True,
             "note": "Attachments could not be carried over."},
            {"with_attachments": False, "with_embeds": False,
             "note": "Attachments and embeds could not be carried over."},
        ]
        if policy == FORCE:
            ladder.append(
                {"with_attachments": False, "with_embeds": False, "max_content": 900,
                 "note": "Truncated: the full message could not be replayed."}
            )

        recovered = 0
        remaining: list[dict] = []

        for entry in list(self.checkpoint.failures):
            try:
                message = await self.thread.fetch_message(entry["source_id"])
            except (discord.HTTPException, *TRANSIENT) as exc:
                entry["reason"] = f"source unreadable: {exc}"[:200]
                remaining.append(entry)
                continue

            reactions = await self._collect_reactions(message)
            landed = False

            for level in ladder:
                payload = await self._compose(message, reactions, **level)
                if payload.is_empty():
                    continue
                try:
                    landed = await self._apply(entry, message, payload)
                except (discord.HTTPException, *TRANSIENT) as exc:
                    entry["reason"] = f"{type(exc).__name__}: {exc}"[:200]
                    continue
                if landed:
                    break

            if landed:
                recovered += 1
                self.progress.recovered += 1
            elif policy == FORCE and entry.get("placeholder_id"):
                # Last resort: the placeholder already carries author, date and
                # a link to the original, so it stands as the stub.
                recovered += 1
                self.progress.recovered += 1
            else:
                remaining.append(entry)

            await asyncio.sleep(self.config.message_delay)

        self.checkpoint.failures = remaining
        self.checkpoint.recovery_choice = policy
        self.store.save(self.checkpoint)
        self.manifest.payload["recovery"] = {
            "policy": policy,
            "recovered": recovered,
            "remaining": [e["source_id"] for e in remaining],
        }
        self.manifest.flush()
        return recovered, len(remaining)

    async def _apply(
        self, entry: dict, message: discord.Message, payload: Payload
    ) -> bool:
        """Edit the placeholder in place, or emit at the end if it is gone."""
        placeholder_id = entry.get("placeholder_id")
        if placeholder_id:
            await self.webhook.edit_message(
                placeholder_id,
                content=payload.blocks[0] if payload.blocks else None,
                embeds=payload.embeds,
                attachments=payload.files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            # Overflow blocks cannot be inserted in place; they follow the
            # edited placeholder rather than being dropped.
            for block in payload.blocks[1:]:
                await self._send_with_retry(
                    content=block,
                    username=safe_webhook_username(author_label(message)),
                    avatar_url=author_avatar(message),
                    allowed_mentions=discord.AllowedMentions.none(),
                    silent=True,
                    wait=True,
                )
            self.checkpoint.remember(message.id, placeholder_id)
            return True

        new_id = await self._emit(message, payload)
        entry["placeholder_id"] = new_id
        self.checkpoint.remember(message.id, new_id)
        return True

    # -------------------------------------------------------------- verifying

    async def verify(self) -> dict:
        """Compare the target channel against the source thread.

        The manifest says what the bot believes it did. This counts what is
        actually there, which is the only thing worth reporting.
        """
        if self.channel is None:
            raise PromotionError("No target channel to verify against.")

        source_ids: set[int] = set()
        expected: set[int] = set()
        async for message in self._iter_source(None):
            source_ids.add(message.id)
            # Only what the replay would emit can be missing from the target.
            # Counting skipped types here reported a permanent phantom gap and
            # advised a resume that could never close it.
            if message.type in REPLAYABLE_TYPES:
                expected.add(message.id)

        replayed: set[int] = set()
        async for message in self.channel.history(limit=None, oldest_first=True):
            found = source_id_from_replay(message.content)
            if found is not None:
                replayed.add(found)

        missing = sorted(expected - replayed)
        return {
            "source_total": len(source_ids),
            "source_replayable": len(expected),
            "found_in_target": len(replayed & expected),
            "missing": missing,
            "pending_failures": len(self.checkpoint.failures),
            "unknown_in_target": len(replayed - source_ids),
        }

    # --------------------------------------------------------------- webhooks

    async def discover_webhooks(self) -> list[dict]:
        """Find the webhooks that actually published in this thread.

        A webhook has no idea it targets a thread: thread_id is a parameter of
        the call, not a property of the webhook, so there is no way to ask
        Discord which webhooks serve a thread. But every message names its
        emitter through webhook_id, so reading the thread answers the question
        from evidence rather than from guesswork.

        Webhooks that exist on the parent channel but never posted here are
        deliberately left out: migrating one would move an integration that has
        nothing to do with this thread.
        """
        counts: dict[int, int] = {}
        async for message in self._iter_source(None):
            if message.webhook_id:
                counts[message.webhook_id] = counts.get(message.webhook_id, 0) + 1
        if not counts:
            return []

        try:
            live = {hook.id: hook for hook in await self.thread.parent.webhooks()}
        except discord.Forbidden as exc:
            raise PromotionError(
                "Reading the webhooks of the parent channel needs the Manage "
                "Webhooks permission there."
            ) from exc

        found: list[dict] = []
        for webhook_id, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            hook = live.get(webhook_id)
            found.append(
                {
                    "id": webhook_id,
                    "webhook": hook,
                    "messages": count,
                    "name": hook.name if hook else None,
                    # The application that owns the webhook, when Discord tells
                    # us. This is what lets an operator recognise which of three
                    # identically named webhooks is the one they care about.
                    "owner": str(hook.user) if hook and hook.user else None,
                    "gone": hook is None,
                }
            )
        return found

    async def migrate_webhook(self, webhook: discord.Webhook) -> None:
        """Move a webhook to the promoted channel, keeping its URL.

        Editing channel_id preserves the id and the token, so the URL the
        emitting service holds keeps working and nothing has to be
        reconfigured on that side. With one exception, see the warning the
        command prints: a caller that passes thread_id explicitly still routes
        to a thread that no longer belongs to this channel.
        """
        if self.channel is None:
            raise PromotionError("No target channel to migrate a webhook into.")
        await webhook.edit(
            channel=self.channel,
            reason=f"Thread {self.thread.id} promoted to {self.channel.id}",
        )

    # ---------------------------------------------------------------- preview

    async def preview(self) -> dict:
        """Read the thread and describe what a replay would produce.

        Creates nothing: no channel, no webhook, not a single message. This is
        the only way to look at a migration before committing to it, and it
        exercises reading, composition and rendering without a single write.
        """
        oversized: list[str] = []
        limit = self.thread.guild.filesize_limit
        entries: list[dict] = []
        replayable = 0
        index = 0

        total = 0
        async for message in self._iter_source(None):
            total += 1
            if message.type not in REPLAYABLE_TYPES:
                # Same filter as the replay, so the preview shows what would
                # actually land rather than everything the thread contains.
                continue
            index += 1
            replayable += 1
            reactions = await self._collect_reactions(message)
            lost = [
                f"{a.filename} ({a.size / 1_048_576:.1f} MB)"
                for a in message.attachments
                if a.size > limit
            ]
            oversized.extend(lost)
            entries.append(self._manifest_entry(message, index, reactions, lost=lost))

        self.manifest.set_header(
            thread={
                "id": self.thread.id,
                "name": self.thread.name,
                "parent": getattr(self.thread.parent, "name", None),
                "created_at": self._thread_created_at(),
                "url": self._source_url(),
            },
            target={},
        )
        for entry in entries:
            self.manifest.add_message(entry)
        if oversized:
            self.manifest.warn(
                f"{len(oversized)} attachment(s) exceed the guild upload limit: "
                + ", ".join(oversized)
            )

        estimate = replayable * self.config.message_delay
        return {
            "total": total,
            "replayable": replayable,
            "skipped": total - replayable,
            "attachments": sum(len(m.get("attachments") or []) for m in entries),
            "oversized": len(oversized),
            "estimated_seconds": estimate,
            "manifest": self.manifest.payload,
        }

    # ----------------------------------------------------------- presentation

    def _source_url(self) -> str:
        return f"https://discord.com/channels/{self.thread.guild.id}/{self.thread.id}"

    def _thread_created_at(self):
        """Thread.created_at is None for threads made before 9 January 2022.

        The snowflake carries its own creation time, so there is always an
        answer and no reason for a historical thread to fail here.
        """
        return self.thread.created_at or discord.utils.snowflake_time(self.thread.id)

    def _header_embed(self, status: str) -> discord.Embed:
        parent = self.thread.parent
        description = (
            f"History replayed from [the original thread]({self._source_url()}) "
            f"in {parent.mention}.\n"
            f"Everything below is a webhook copy: usernames and avatars are "
            f"those of the original authors, and the real timestamp appears "
            f"as subtext under each message."
            if self.full_replay
            else
            f"This channel continues [the original thread]({self._source_url()}) "
            f"from {parent.mention}. The history was left where it is."
        )
        embed = discord.Embed(
            title=f"Promoted thread: {self.thread.name}",
            description=description,
        )
        embed.add_field(
            name="Created",
            value=f"<t:{int(self._thread_created_at().timestamp())}:D>",
        )
        embed.add_field(name="Promoted by", value=self.invoker.mention)
        if self.full_replay:
            qui = self.visible_to.mention if self.visible_to is not None else "nobody else yet"
            embed.add_field(
                name="Visible to",
                value=f"{self.invoker.mention} and {qui}. Open it to the server "
                      f"from the channel permissions once the replay is done.",
                inline=False,
            )
        embed.add_field(name="Status", value=status, inline=False)
        embed.set_footer(text=f"Source thread: {self.thread.id}")
        return embed

    async def _write_header(self) -> None:
        """Single service message, living in the target channel.

        Progress is reported by editing it rather than by posting into the
        source thread, which stays read-only.
        """
        message = await self.channel.send(
            embed=self._header_embed("Replay starting."),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.checkpoint.header_message_id = message.id
        self.store.save(self.checkpoint)

    async def update_status(self, status: str, force: bool = False) -> None:
        # Called from a finally block: nothing raised here may be allowed to
        # replace the exception that got us there, or to end the migration.
        try:
            if self.checkpoint.header_message_id is None or self.channel is None:
                return
            if not force and self.progress.sent % max(self.config.progress_every, 1):
                return
            await self.channel.get_partial_message(
                self.checkpoint.header_message_id
            ).edit(embed=self._header_embed(status))
        except Exception:
            log.debug("Status update skipped", exc_info=True)

    def _expected_total(self) -> int | None:
        """How many messages the thread holds, as Discord counts them.

        Free: it comes with the thread object, no extra pass over the history.
        Approximate, since it excludes deleted messages and Discord stops
        counting precisely past a point, which is why the progress line says
        "about".
        """
        if self.checkpoint.source_total:
            return self.checkpoint.source_total
        compte = getattr(self.thread, "message_count", None)
        return compte or None

    def status_line(self, prefix: str) -> str:
        fait = self.progress.sent + self.progress.skipped + self.already_replayed
        total = self._expected_total()
        if total:
            pct = min(100, round(fait * 100 / total))
            line = f"{prefix} {fait} of about {total} message(s), {pct}%"
        else:
            line = f"{prefix} {fait} message(s) replayed"
        if self.progress.attachments:
            line += f", {self.progress.attachments} attachment(s)"
        if self.progress.reactions:
            line += f", {self.progress.reactions} reaction(s)"
        line += "."
        if self.progress.skipped:
            line += f" {self.progress.skipped} skipped."
        if self.progress.failed:
            line += f" {self.progress.failed} failed."
        if self.progress.recovered:
            line += f" {self.progress.recovered} recovered."
        summary = self.rate_limits.summary()
        if summary != "no rate limiting":
            line += f" Throttling: {summary}."
        return line

    async def prompt_recovery(self) -> None:
        """Ask once, for the whole set, and apply the answer to all of it."""
        if not self.checkpoint.failures:
            return

        async def apply(interaction: discord.Interaction, policy: str) -> None:
            await self.update_status(
                self.status_line(f"Recovery in progress ({policy}):"), force=True
            )
            recovered, left = await self.recover(policy)
            summary = f"Recovery finished: {recovered} restored, {left} still missing."
            # The channel, not the interaction: a recovery pass runs for tens of
            # minutes and the interaction token dies after fifteen.
            try:
                await self.channel.send(
                    summary, allowed_mentions=discord.AllowedMentions.none()
                )
            except (discord.HTTPException, *TRANSIENT):
                log.warning("Recovery summary could not be posted")
            await self.update_status(self.status_line("Replay complete:"), force=True)
            if not left:
                await self._retire_webhook()

        await self.channel.send(
            embed=failure_report(
                self.checkpoint.failures, self.channel.id, self.thread.guild.id
            ),
            view=RecoveryView(self.invoker.id, apply),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ----------------------------------------------------------- orchestration

    async def _pinned_source_ids(self) -> list[int]:
        """Which source messages are pinned, asked of the source itself.

        Everything else about resuming was made independent of the checkpoint,
        because the channel is the record. Pins were the exception: they were
        accumulated while replaying and vanished with the file. The thread
        knows its own pins, so it is asked, oldest first so the target ends up
        in the same order.

        Falls back to whatever the checkpoint holds if the source cannot be
        read, which is still better than nothing.
        """
        try:
            return [message.id async for message in self.thread.pins(oldest_first=True)]
        except (discord.HTTPException, *TRANSIENT) as exc:
            log.warning("Pins unreadable from the source (%s), using the checkpoint", exc)
            return list(self.checkpoint.pinned_source_ids)

    async def _restore_pins(self) -> None:
        for source_id in await self._pinned_source_ids():
            new_id = self.checkpoint.translated(source_id)
            if not new_id:
                continue
            try:
                await self.channel.get_partial_message(new_id).pin(
                    reason="Restoring pins from the original thread"
                )
            except (discord.HTTPException, *TRANSIENT) as exc:
                # 50 pins per channel is the ceiling, and this is where a large
                # thread meets it.
                self.manifest.warn(f"Pin not restored for {new_id}: {exc}")
            await asyncio.sleep(1.0)

    async def prepare(self) -> discord.TextChannel:
        self.channel = await self._resolve_channel()
        self.webhook = await self._resolve_webhook()
        return self.channel

    async def prepare_for_recovery(self) -> discord.TextChannel:
        """Attach to a previous run without ever creating anything new.

        Recovery must not silently spawn a second channel: if the first one is
        gone, the right answer is a fresh /promote, not a half-migration.
        """
        if not self.checkpoint.target_channel_id:
            raise PromotionError("No previous run is recorded for this thread.")
        channel = await self._fetch_target()
        if channel is None:
            raise PromotionError(
                "The channel created by the previous run no longer exists. "
                "Run /promote to start over."
            )
        self.channel = channel
        self.webhook = await self._resolve_webhook()
        return channel

    async def attach(self) -> discord.TextChannel:
        """Attach to an existing run for a read-only operation."""
        return await self.prepare_for_recovery()

    async def run(self) -> discord.TextChannel:
        await self.prepare()

        if not self.full_replay:
            if self.checkpoint.header_message_id is None:
                await self._write_header()
            await self.update_status("Channel created, history not replayed.", force=True)
            self.checkpoint.done = True
            self.store.save(self.checkpoint)
            await self._retire_webhook()
            return self.channel

        # The channel is the record of what was replayed, not the checkpoint.
        resume_after = await self._reconcile_with_channel()
        if resume_after is not None:
            log.info("Resuming after source message %s", resume_after)
            self.already_replayed = len(self.checkpoint.id_map)
        self.checkpoint.last_source_id = resume_after
        if not self.checkpoint.source_total:
            self.checkpoint.source_total = getattr(self.thread, "message_count", None)

        self.manifest.set_header(
            thread={
                "id": self.thread.id,
                "name": self.thread.name,
                "parent": getattr(self.thread.parent, "name", None),
                "created_at": self._thread_created_at(),
                "url": self._source_url(),
            },
            target={"id": self.channel.id, "name": self.channel.name},
        )

        if self.checkpoint.header_message_id is None:
            await self._write_header()

        index = 0
        aborted = False
        depuis_sauvegarde = 0
        async for message in self._iter_source(resume_after):
            if self.abort_requested:
                aborted = True
                break
            index += 1
            try:
                await self._replay_one(message, index)
                depuis_sauvegarde += 1
            except RECOVERABLE as exc:
                log.warning("Failure on message %s: %s", message.id, exc)
                await self._handle_failure(message, index, exc)
                # A failure is worth persisting straight away: it is what the
                # recovery pass works from, and it is rare enough to afford.
                depuis_sauvegarde = CHECKPOINT_EVERY
            finally:
                if depuis_sauvegarde >= CHECKPOINT_EVERY:
                    self.store.save(self.checkpoint)
                    depuis_sauvegarde = 0
                await self.update_status(self.status_line("Replay in progress:"))
            await asyncio.sleep(self.config.message_delay)

        if aborted:
            self.store.save(self.checkpoint)
            self.manifest.payload["rate_limits"] = self.rate_limits.as_dict()
            self.manifest.flush()
            await self.update_status(
                self.status_line("Replay stopped on request:"), force=True
            )
            log.info("Replay of thread %s aborted on request", self.thread.id)
            return self.channel

        await self.update_status(self.status_line("Replay in progress:"), force=True)
        await self._restore_pins()

        self.checkpoint.done = True
        self.store.save(self.checkpoint)
        self.manifest.payload["rate_limits"] = self.rate_limits.as_dict()
        self.manifest.flush()

        if self.checkpoint.failures:
            await self.update_status(
                self.status_line("Replay complete, awaiting a recovery choice:"),
                force=True,
            )
            await self.prompt_recovery()
        else:
            await self.update_status(self.status_line("Replay complete:"), force=True)
            await self._retire_webhook()
        return self.channel
