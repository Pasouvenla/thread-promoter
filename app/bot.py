"""Entry point: Discord bot exposing the thread promotion commands."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .export import export_manifest, render_manifest
from .promoter import PromotionError, ThreadPromoter
from .views import WebhookMigrationView, webhook_report
from .state import CheckpointStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
)
log = logging.getLogger("bot")

# Touched by a background loop while the gateway is up, read by the container
# healthcheck. A bot that stays connected and stops making progress is the
# failure mode worth catching, and it is invisible from the outside otherwise.
HEARTBEAT_PATH = Path(os.getenv("PROMOTER_HEARTBEAT", "/tmp/heartbeat"))
HEARTBEAT_INTERVAL = 30.0

REQUIRED_BOT_PERMISSIONS = discord.Permissions(
    view_channel=True,
    read_message_history=True,
    send_messages=True,
    manage_channels=True,
    manage_webhooks=True,
    manage_messages=True,
    add_reactions=True,
    attach_files=True,
    embed_links=True,
)

# discord.py exposes some permissions under their legacy alias when iterating
# (view_channel comes back as read_messages), so the labels are mapped back to
# what the Discord interface actually calls them.
PERMISSION_LABELS = {
    "read_messages": "View Channel",
    "read_message_history": "Read Message History",
    "send_messages": "Send Messages",
    "manage_channels": "Manage Channels",
    "manage_webhooks": "Manage Webhooks",
    "manage_messages": "Manage Messages",
    "add_reactions": "Add Reactions",
    "attach_files": "Attach Files",
    "embed_links": "Embed Links",
}


class PromoterBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        # Privileged intent, mandatory: without it message.content comes back
        # empty and the replay only carries attachments.
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.running: set[int] = set()
        self.tasks: set[asyncio.Task] = set()
        # Live promoters, so a run can be asked to stop from a command.
        self.active: dict[int, ThreadPromoter] = {}

    async def setup_hook(self) -> None:
        if self.config.guild_ids:
            for guild_id in self.config.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("Commands synced to guild %s", guild_id)
        else:
            await self.tree.sync()
            log.info("Commands synced globally (slow propagation).")
        self.spawn(self._heartbeat())

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)

    async def _heartbeat(self) -> None:
        while not self.is_closed():
            if self.is_ready():
                try:
                    HEARTBEAT_PATH.write_text(f"{time.time()} {self.latency:.3f}\n")
                except OSError:
                    log.warning("Heartbeat could not be written to %s", HEARTBEAT_PATH)
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    def spawn(self, coro) -> None:
        # Keep a strong reference: a bare create_task can be garbage collected
        # mid-run, which would silently abort a migration.
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def claim(self, thread_id: int) -> bool:
        """Reserve a thread, atomically with respect to the event loop.

        Testing membership and adding must not be separated by an await: the
        round trip to Discord in between is long enough for a second /promote
        to slip through and start its own channel.
        """
        if thread_id in self.running:
            return False
        self.running.add(thread_id)
        return True


def format_duration(seconds: float) -> str:
    """Readable at both ends of the range, and never optimistic.

    A five-message thread used to round to "0 minute(s)", which says nothing
    about whether the run is instant or merely short. Rounding up rather than
    to nearest, because someone told "2 minutes" who waits two and a half
    minutes was misled, while the reverse costs nothing.
    """
    if seconds < 60:
        return f"{math.ceil(seconds)} second(s)"
    if seconds < 3600:
        return f"{math.ceil(seconds / 60)} minute(s)"
    hours, minutes = divmod(math.ceil(seconds / 60), 60)
    return f"{hours}h{minutes:02d}"


def _preflight(interaction: discord.Interaction) -> discord.Thread:
    channel = interaction.channel
    if not isinstance(channel, discord.Thread):
        raise PromotionError("This command must be run from inside a thread.")
    if channel.parent is None:
        raise PromotionError("The parent channel of this thread cannot be reached.")

    me = interaction.guild.me
    if me is None:
        raise PromotionError("Bot member object unavailable, cannot check permissions.")

    granted = channel.permissions_for(me)
    missing = [
        PERMISSION_LABELS.get(name, name)
        for name, value in REQUIRED_BOT_PERMISSIONS
        if value and not getattr(granted, name)
    ]
    if missing:
        raise PromotionError("Missing bot permissions: " + ", ".join(missing))
    return channel


async def _launch(
    bot: PromoterBot,
    interaction: discord.Interaction,
    *,
    name: str | None,
    full_replay: bool,
    visible_to: discord.Role | None = None,
) -> None:
    try:
        thread = _preflight(interaction)
    except PromotionError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    if not bot.claim(thread.id):
        await interaction.response.send_message(
            "A promotion is already running on this thread.", ephemeral=True
        )
        return

    try:
        await interaction.response.send_message(
            "Promotion started. Progress is reported in the new channel, which "
            "starts closed so the replay does not flood the server: open it "
            "from the channel permissions once you are happy with it. On a "
            "large thread the run takes hours because of Discord rate limits, "
            "and `/promote-abort` stops it cleanly. The source thread is left "
            "untouched.",
            ephemeral=True,
        )
    except discord.HTTPException:
        bot.running.discard(thread.id)
        raise

    async def worker() -> None:
        promoter: ThreadPromoter | None = None
        try:
            # Inside the try: loading a corrupted checkpoint used to raise here
            # and leave the thread marked as running forever.
            promoter = ThreadPromoter(
                bot,
                bot.config,
                thread,
                interaction.user,
                target_name=name,
                full_replay=full_replay,
                visible_to=visible_to,
            )
            bot.active[thread.id] = promoter
            channel = await promoter.run()
            log.info("Thread %s promoted to %s", thread.id, channel.id)
        except Exception:
            log.exception("Promotion of thread %s aborted", thread.id)
            # The failure is reported in the target channel, never in the
            # source thread, which the service treats as read-only.
            if promoter is not None and promoter.channel is not None:
                with contextlib.suppress(discord.HTTPException):
                    await promoter.channel.send(
                        "The replay stopped early. State has been saved: run "
                        "`/promote-resume` from the source thread to continue "
                        "from the last processed message.",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            else:
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        "The promotion could not start. Check the bot logs.",
                        ephemeral=True,
                    )
        finally:
            bot.running.discard(thread.id)
            bot.active.pop(thread.id, None)

    bot.spawn(worker())


def register(bot: PromoterBot) -> None:
    @bot.tree.command(
        name="promote",
        description="Convert this thread into a dedicated channel, replaying the full history.",
    )
    @app_commands.describe(
        name="Name of the channel to create (defaults to the thread name).",
        visible_to="Role allowed to see the channel during the replay. "
                   "Nobody but you if left empty.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote(
        interaction: discord.Interaction,
        name: str | None = None,
        visible_to: discord.Role | None = None,
    ) -> None:
        await _launch(bot, interaction, name=name, full_replay=True, visible_to=visible_to)

    @bot.tree.command(
        name="promote-link",
        description="Create the dedicated channel and link back to this thread, without replaying.",
    )
    @app_commands.describe(name="Name of the channel to create (defaults to the thread name).")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_link(interaction: discord.Interaction, name: str | None = None) -> None:
        await _launch(bot, interaction, name=name, full_replay=False)

    @bot.tree.command(
        name="promote-resume",
        description="Resume an interrupted promotion from the last processed message.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_resume(interaction: discord.Interaction) -> None:
        await _launch(bot, interaction, name=None, full_replay=True)

    @bot.tree.command(
        name="promote-recover",
        description="Re-open the recovery choice for messages that failed to replay.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_recover(interaction: discord.Interaction) -> None:
        try:
            thread = _preflight(interaction)
        except PromotionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        try:
            promoter = ThreadPromoter(bot, bot.config, thread, interaction.user)
            if not promoter.checkpoint.failures:
                await interaction.response.send_message(
                    "No failed message is pending for this thread.", ephemeral=True
                )
                return
            await promoter.prepare_for_recovery()
        except PromotionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"{len(promoter.checkpoint.failures)} message(s) pending. "
            f"The choice is posted in {promoter.channel.mention}.",
            ephemeral=True,
        )
        bot.spawn(promoter.prompt_recovery())

    @bot.tree.command(
        name="promote-verify",
        description="Count both sides and report what the replayed channel is missing.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_verify(interaction: discord.Interaction) -> None:
        try:
            thread = _preflight(interaction)
        except PromotionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            promoter = ThreadPromoter(bot, bot.config, thread, interaction.user)
            await promoter.attach()
            result = await promoter.verify()
        except PromotionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        missing = result["missing"]
        lines = [
            f"Source messages: {result['source_total']} "
            f"({result['source_replayable']} replayable)",
            f"Found in the target channel: {result['found_in_target']}",
            f"Missing: {len(missing)}",
            f"Failures still pending: {result['pending_failures']}",
        ]
        if missing:
            shown = ", ".join(str(m) for m in missing[:20])
            lines.append(f"First missing ids: {shown}")
            lines.append("Run `/promote-resume` to replay what is missing.")
        else:
            lines.append("Nothing is missing.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @bot.tree.command(
        name="promote-export",
        description="Render this thread's migration manifest as a standalone HTML archive.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_export(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Run this command from inside a thread.", ephemeral=True
            )
            return

        thread_id = interaction.channel.id
        manifest_path = bot.config.manifest_dir / f"{thread_id}.json"
        if not manifest_path.exists():
            await interaction.response.send_message(
                "No manifest for this thread. Run `/promote` first.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        destination = bot.config.export_dir / f"{thread_id}.html"
        try:
            await asyncio.to_thread(export_manifest, manifest_path, destination)
        except (OSError, ValueError) as exc:
            await interaction.followup.send(f"Export failed: {exc}", ephemeral=True)
            return

        payload = destination.read_bytes()
        limit = interaction.guild.filesize_limit
        if len(payload) > limit:
            await interaction.followup.send(
                f"Archive written to {destination} ({len(payload) / 1_048_576:.1f} MB), "
                "too large to attach here.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Archive of {manifest_path.stem}, rendered from the manifest.",
            file=discord.File(io.BytesIO(payload), filename=f"{thread_id}.html"),
            ephemeral=True,
        )

    @bot.tree.command(
        name="promote-preview",
        description="Show what a replay would produce, without creating anything.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_preview(interaction: discord.Interaction) -> None:
        try:
            thread = _preflight(interaction)
        except PromotionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        promoter = ThreadPromoter(bot, bot.config, thread, interaction.user)
        result = await promoter.preview()

        lines = [
            f"{result['total']} message(s) in the thread, "
            f"{result['replayable']} would be replayed, {result['skipped']} skipped.",
            f"{result['attachments']} attachment(s), "
            f"{result['oversized']} too large to carry over.",
            f"Rough run time: {format_duration(result['estimated_seconds'])} "
            f"at the current pacing.",
            "Nothing has been created. The attached page shows the content as it stands.",
        ]
        page = render_manifest(result["manifest"]).encode("utf-8")
        if len(page) > interaction.guild.filesize_limit:
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return
        await interaction.followup.send(
            "\n".join(lines),
            file=discord.File(io.BytesIO(page), filename=f"{thread.id}-preview.html"),
            ephemeral=True,
        )

    @bot.tree.command(
        name="promote-abort",
        description="Stop the replay running on this thread, at the next message boundary.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_abort(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Run this command from inside a thread.", ephemeral=True
            )
            return

        promoter = bot.active.get(interaction.channel.id)
        if promoter is None:
            await interaction.response.send_message(
                "No replay is running on this thread.", ephemeral=True
            )
            return

        promoter.request_abort()
        await interaction.response.send_message(
            "Stopping after the message in flight. Whatever was replayed stays, "
            "and `/promote-resume` picks up from there.",
            ephemeral=True,
        )

    @bot.tree.command(
        name="promote-webhooks",
        description="Move the integrations that posted in this thread into the promoted channel.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_webhooks(interaction: discord.Interaction) -> None:
        try:
            thread = _preflight(interaction)
        except PromotionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            promoter = ThreadPromoter(bot, bot.config, thread, interaction.user)
            await promoter.attach()
            candidates = await promoter.discover_webhooks()
        except PromotionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send(
                "No webhook has published in this thread, so there is nothing "
                "to move. A bot posting as itself rather than through a webhook "
                "needs no migration: the new channel inherits the parent's "
                "permissions, so its access follows on its own.",
                ephemeral=True,
            )
            return

        movable = [c for c in candidates if not c["gone"]]

        async def apply(inner: discord.Interaction, chosen: list[int]) -> None:
            by_id = {c["id"]: c for c in candidates}
            moved, failed = [], []
            for webhook_id in chosen:
                entry = by_id.get(webhook_id)
                if entry is None or entry["gone"]:
                    failed.append((str(webhook_id), "no longer exists"))
                    continue
                try:
                    await promoter.migrate_webhook(entry["webhook"])
                    moved.append(entry.get("name") or str(webhook_id))
                except (discord.HTTPException, discord.Forbidden) as exc:
                    failed.append((entry.get("name") or str(webhook_id), str(exc)))

            lines = []
            if moved:
                lines.append(f"Moved into {promoter.channel.mention}: " + ", ".join(moved))
                lines.append(
                    "Their URLs are unchanged. Any caller passing `thread_id` "
                    "must now drop it, or it will keep targeting a thread that "
                    "no longer belongs to that channel."
                )
            for name, reason in failed:
                lines.append(f"Failed on {name}: {reason}")
            try:
                await inner.followup.send("\n".join(lines), ephemeral=True)
            except discord.HTTPException:
                log.warning("Webhook migration summary could not be delivered")

        await interaction.followup.send(
            embed=webhook_report(candidates, promoter.channel.name),
            view=WebhookMigrationView(interaction.user.id, movable, apply),
            ephemeral=True,
        )

    @bot.tree.command(
        name="promote-forget",
        description="Clear the resume checkpoint for this thread, leaving channels untouched.",
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def promote_forget(interaction: discord.Interaction) -> None:
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Run this command from inside a thread.", ephemeral=True
            )
            return
        CheckpointStore(bot.config.state_dir).discard(interaction.channel.id)
        await interaction.response.send_message("Checkpoint cleared.", ephemeral=True)

    @promote.error
    @promote_link.error
    @promote_resume.error
    @promote_recover.error
    @promote_verify.error
    @promote_export.error
    @promote_webhooks.error
    @promote_preview.error
    @promote_abort.error
    @promote_forget.error
    async def on_error(interaction: discord.Interaction, error: Exception) -> None:
        message = (
            "You need the Manage Channels permission to use this command."
            if isinstance(error, app_commands.MissingPermissions)
            else f"Error: {error}"
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def main() -> None:
    config = Config.load()
    bot = PromoterBot(config)
    register(bot)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
