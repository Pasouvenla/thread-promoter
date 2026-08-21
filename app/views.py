"""Interactive recovery prompt.

Failed messages are not decided one by one: a thousand-message replay runs
unattended for the better part of an hour, and stopping on every failure would
turn a background job into a babysitting exercise. Failures are collected during
the replay, then a single policy is applied to all of them at the end.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

FORCE = "force"
BEST_EFFORT = "best_effort"
LEAVE = "leave"

EMBED_FIELD_LIMIT = 1024

POLICY_LABELS = {
    FORCE: "Force",
    BEST_EFFORT: "Best effort",
    LEAVE: "Leave as is",
}

POLICY_HELP = {
    FORCE: (
        "Every failed message ends up present, degraded as far as necessary: "
        "without attachments, then without embeds, then with truncated content, "
        "and as a last resort a stub carrying the author, the date and a link "
        "to the original. Completeness first."
    ),
    BEST_EFFORT: (
        "Retries the full message, then without attachments, then without "
        "embeds. Anything still failing keeps its placeholder rather than being "
        "silently truncated. Fidelity first."
    ),
    LEAVE: (
        "Leaves every placeholder as it is. The gaps stay visible and the "
        "details remain in the manifest. You can decide later with "
        "/promote-recover."
    ),
}


class RecoveryView(discord.ui.View):
    def __init__(
        self,
        invoker_id: int,
        callback: Callable[[discord.Interaction, str], Awaitable[None]],
        *,
        timeout: float = 3600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.invoker_id = invoker_id
        self.callback = callback
        self.choice: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who started this promotion can choose.",
                ephemeral=True,
            )
            return False
        return True

    async def _choose(self, interaction: discord.Interaction, policy: str) -> None:
        self.choice = policy
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()
        await self.callback(interaction, policy)

    @discord.ui.button(label="Force", style=discord.ButtonStyle.primary)
    async def force(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._choose(interaction, FORCE)

    @discord.ui.button(label="Best effort", style=discord.ButtonStyle.secondary)
    async def best_effort(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._choose(interaction, BEST_EFFORT)

    @discord.ui.button(label="Leave as is", style=discord.ButtonStyle.secondary)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._choose(interaction, LEAVE)


def _fit(lines: list[str], footer: str | None, limit: int = EMBED_FIELD_LIMIT) -> str:
    """Pack whole entries into the field, keeping the footer that says so.

    Truncating the joined text mid-character would eat the very line telling
    the reader that something is missing.
    """
    budget = limit - (len(footer) + 1 if footer else 0)
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (1 if kept else 0)
        if used + cost > budget:
            break
        kept.append(line)
        used += cost

    dropped = len(lines) - len(kept)
    if dropped and footer is None:
        footer = f"-# and {dropped} more, full list in the manifest"
        return _fit(lines, footer, limit)
    return "\n".join([*kept, footer] if footer else kept)


def failure_report(failures: list[dict], channel_id: int, guild_id: int) -> discord.Embed:
    """List every failed message and where it sits in the replayed channel."""
    embed = discord.Embed(
        title=f"{len(failures)} message(s) could not be replayed",
        description=(
            "Each one holds its place in the channel through a placeholder, so "
            "the order of the conversation is intact. Pick one policy for the "
            "whole set."
        ),
    )

    lines = []
    for entry in failures[:10]:
        anchor = entry.get("placeholder_id")
        where = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{anchor}"
            if anchor
            else None
        )
        position = f"[placeholder]({where})" if where else "position unknown"
        lines.append(
            f"`#{entry.get('index', '?')}` {entry.get('author', 'unknown')} "
            f"<t:{entry.get('created_at_epoch', 0)}:f> {position}\n"
            f"-# {entry.get('reason', 'no reason recorded')}"
        )

    footer = (
        f"-# and {len(failures) - 10} more, full list in the manifest"
        if len(failures) > 10
        else None
    )
    embed.add_field(name="Messages", value=_fit(lines, footer) or "none", inline=False)

    for policy, help_text in POLICY_HELP.items():
        embed.add_field(name=POLICY_LABELS[policy], value=help_text, inline=False)
    return embed
