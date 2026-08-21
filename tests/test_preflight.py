"""The preflight is the only thing standing between a user and a 403 with no
explanation. It has to check permissions where they are actually used."""

from __future__ import annotations

import discord
import pytest

from app.bot import _preflight
from app.promoter import PromotionError
from conftest import FakeCategory


class FakeInteraction:
    def __init__(self, channel, guild):
        self.channel = channel
        self.guild = guild


def _permissive(_member):
    return discord.Permissions.all()


def test_a_healthy_thread_passes(thread, guild):
    thread.permissions_for = _permissive
    guild.me.guild_permissions = discord.Permissions.all()
    assert _preflight(FakeInteraction(thread, guild)) is thread


def test_a_missing_thread_permission_is_named(thread, guild):
    thread.permissions_for = lambda _m: discord.Permissions(view_channel=True)
    with pytest.raises(PromotionError, match="on this thread"):
        _preflight(FakeInteraction(thread, guild))


def test_manage_channels_is_checked_on_the_category_not_the_thread(thread, guild):
    """The exact failure seen in production: green light, then a bare 403.

    Manage Channels on the thread says nothing about the category, and the
    category is where the new channel is created.
    """
    thread.permissions_for = _permissive
    thread.parent.category = FakeCategory("Dofus", manage=False)

    with pytest.raises(PromotionError) as caught:
        _preflight(FakeInteraction(thread, guild))

    message = str(caught.value)
    assert "Dofus" in message, "the category must be named, or nobody finds it"
    assert "Manage Channels" in message
    assert "category" in message


def test_a_parentless_channel_falls_back_to_server_permissions(thread, guild):
    thread.permissions_for = _permissive
    thread.parent.category = None
    guild.me.guild_permissions = discord.Permissions(view_channel=True)

    with pytest.raises(PromotionError, match="server"):
        _preflight(FakeInteraction(thread, guild))


def test_outside_a_thread_it_refuses(guild):
    with pytest.raises(PromotionError, match="inside a thread"):
        _preflight(FakeInteraction(object(), guild))
