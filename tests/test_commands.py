"""The command surface, checked by introspection.

/promote-forget once shipped without any permission check while the README
promised one, so the guarantee is asserted rather than read.
"""

from __future__ import annotations

import discord
import pytest

from app.bot import PromoterBot, register


@pytest.fixture
def registered(config):
    bot = PromoterBot(config)
    register(bot)
    return bot


EXPECTED = {
    "promote",
    "promote-link",
    "promote-resume",
    "promote-recover",
    "promote-verify",
    "promote-export",
    "promote-preview",
    "promote-webhooks",
    "promote-abort",
    "promote-forget",
}


def test_every_documented_command_is_registered(registered):
    assert {command.name for command in registered.tree.get_commands()} == EXPECTED


def test_every_command_requires_manage_channels(registered):
    for command in registered.tree.get_commands():
        assert command.checks, f"/{command.name} has no permission check at all"


def test_every_command_is_guild_only(registered):
    for command in registered.tree.get_commands():
        assert command.guild_only, f"/{command.name} can be invoked outside a guild"


def test_the_bot_asks_for_the_privileged_intents(registered):
    assert registered.intents.message_content, "content comes back empty without it"
    assert registered.intents.members


def test_claiming_a_thread_twice_fails(registered):
    assert registered.claim(1) is True
    assert registered.claim(1) is False, "two replays could run on one thread"
    registered.running.discard(1)
    assert registered.claim(1) is True


def test_required_permissions_all_have_a_readable_label():
    from app.bot import PERMISSION_LABELS, REQUIRED_BOT_PERMISSIONS

    active = {name for name, value in REQUIRED_BOT_PERMISSIONS if value}
    assert active == set(PERMISSION_LABELS), "a missing permission would print a raw flag name"


def test_view_channel_is_yielded_under_its_legacy_alias():
    """discord.py yields read_messages, the interface says View Channel."""
    permissions = discord.Permissions(view_channel=True)
    assert "read_messages" in {name for name, value in permissions if value}


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 second(s)"),
        (10, "10 second(s)"),
        (59, "59 second(s)"),
        (60, "1 minute(s)"),
        (150, "3 minute(s)"),
        (3599, "60 minute(s)"),
        (3600, "1h00"),
        (7830, "2h11"),
    ],
)
def test_durations_are_readable_at_both_ends(seconds, expected):
    """A five-message thread used to be advertised as "0 minute(s)"."""
    from app.bot import format_duration

    assert format_duration(seconds) == expected


def test_the_webhook_selector_needs_two_deliberate_steps():
    """Migrating is not reversible from the UI, so a misclick must not fire it."""
    from app.views import WebhookMigrationView

    candidates = [
        {"id": 111, "name": "Dofus Bot", "messages": 12, "owner": "dofus", "gone": False},
        {"id": 222, "name": "Other", "messages": 3, "owner": None, "gone": False},
    ]
    view = WebhookMigrationView(1, candidates, lambda i, c: None)

    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(selects) == 1
    assert len(buttons) == 2, "a confirm and a way out"
    assert view.chosen == [], "nothing is selected until the operator selects it"


def test_the_selector_allows_picking_a_subset():
    from app.views import WebhookMigrationView

    candidates = [
        {"id": i, "name": f"hook {i}", "messages": i, "owner": None, "gone": False}
        for i in range(1, 5)
    ]
    view = WebhookMigrationView(1, candidates, lambda i, c: None)
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert select.min_values == 0 and select.max_values == 4
    assert [o.value for o in select.options] == ["1", "2", "3", "4"]


def test_the_selector_stays_within_the_discord_cap():
    from app.views import WebhookMigrationView

    candidates = [
        {"id": i, "name": f"hook {i}", "messages": 1, "owner": None, "gone": False}
        for i in range(40)
    ]
    view = WebhookMigrationView(1, candidates, lambda i, c: None)
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    assert len(select.options) == 25, "Discord refuses more than 25 options"


def test_the_report_warns_about_thread_id():
    """The one thing that breaks after a migration, said before it happens."""
    from app.views import webhook_report

    embed = webhook_report(
        [{"id": 111, "name": "Dofus Bot", "messages": 4, "owner": "dofus", "gone": False}],
        "promoted-channel",
    )
    description = embed.description
    # The mechanism, the exception, and what happens to whoever ignores it.
    assert "URL" in description and "no reconfiguration" in description
    assert "thread_id" in description
    assert "drop" in description and "failing" in description, (
        "naming thread_id is not enough: the report must say what breaks"
    )
    assert "promoted-channel" in description
    assert "Dofus Bot" in embed.fields[0].name


def test_the_report_flags_a_webhook_that_cannot_be_moved():
    from app.views import webhook_report

    embed = webhook_report([{"id": 9, "name": "Gone", "messages": 1, "gone": True}], "x")
    assert "no longer exists" in embed.fields[0].value
