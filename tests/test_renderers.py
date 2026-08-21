"""Unit tests on the rendering helpers. No network, no discord.py client."""

from __future__ import annotations

import pytest

from app.renderers import (
    CONTENT_LIMIT,
    assemble,
    safe_webhook_username,
    slugify_channel_name,
    source_id_from_replay,
    split_content,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A Long Discussion", "a-long-discussion"),
        ("Éléphant & Co", "elephant-co"),
        ("  spaced  out  ", "spaced-out"),
        ("!!!", "promoted-thread"),
        ("", "promoted-thread"),
        ("already-fine_2", "already-fine_2"),
    ],
)
def test_slugify(raw, expected):
    assert slugify_channel_name(raw) == expected


def test_slugify_respects_the_length_cap():
    assert len(slugify_channel_name("x" * 300)) == 100


@pytest.mark.parametrize("raw", ["Discord Fan", "clyde", "EVERYONE", "over here", "discordian"])
def test_forbidden_usernames_are_neutralised(raw):
    out = safe_webhook_username(raw)
    lowered = out.lower()
    assert not any(part in lowered for part in ("discord", "clyde", "everyone", "here"))


def test_username_keeps_casing_and_readability():
    assert safe_webhook_username("Discord Fan").replace("​", "") == "Discord Fan"


def test_username_falls_back_when_blank():
    assert safe_webhook_username("   ") == "User"


def test_username_respects_the_length_cap():
    assert len(safe_webhook_username("a" * 200)) == 80


def test_short_content_is_not_split():
    assert split_content("hello") == ["hello"]
    assert split_content("") == []


def test_split_never_exceeds_the_limit():
    parts = split_content("word " * 2000)
    assert parts
    assert all(len(part) <= CONTENT_LIMIT for part in parts)


def test_split_preserves_every_word():
    body = " ".join(f"w{i}" for i in range(2000))
    parts = split_content(body)
    rebuilt = " ".join(part.strip() for part in parts)
    assert rebuilt.split() == body.split()


def test_split_does_not_break_a_code_fence():
    content = "intro\n```python\n" + "print('x')\n" * 250 + "```\nend"
    parts = split_content(content)
    assert len(parts) > 1
    for part in parts:
        assert part.count("```") % 2 == 0, "a fence was left open"
    for part in parts[1:]:
        assert part.startswith("```python"), "the language tag was lost on reopen"
    assert sum(part.count("print('x')") for part in parts) == 250


def test_split_reopens_a_fence_without_a_language():
    content = "```\n" + "line\n" * 600 + "```"
    parts = split_content(content)
    assert len(parts) > 1
    assert parts[1].startswith("```\n")
    for part in parts:
        assert part.count("```") % 2 == 0


def test_assemble_keeps_every_block_within_the_limit():
    # A long suffix used to push the assembled block past 2000 characters,
    # because the body budget was floored instead of the suffix being moved.
    suffix = ["-# " + "s" * 1600]
    for body_length in range(100, 3000, 37):
        blocks = assemble("x" * body_length, [], suffix)
        assert all(len(block) <= CONTENT_LIMIT for block in blocks)


def test_assemble_detaches_an_oversized_suffix_rather_than_dropping_it():
    suffix = ["-# " + "s" * 1600]
    blocks = assemble("body", [], suffix)
    assert len(blocks) > 1
    assert "s" * 100 in blocks[-1]


def test_assemble_returns_nothing_for_an_empty_message():
    assert assemble("", [], []) == []


def test_assemble_puts_the_prefix_first_and_the_suffix_last():
    blocks = assemble("body", ["-# prefix"], ["-# suffix"])
    assert blocks == ["-# prefix\nbody\n-# suffix"]


def test_assemble_only_annotates_the_edge_blocks():
    blocks = assemble("word " * 1000, ["-# prefix"], ["-# suffix"])
    assert len(blocks) > 1
    assert blocks[0].startswith("-# prefix")
    assert blocks[-1].endswith("-# suffix")
    assert "-# prefix" not in blocks[-1]


def test_source_id_is_recovered_from_a_jump_link():
    content = "text\n-# [original message](https://discord.com/channels/1/2/987654321)"
    assert source_id_from_replay(content) == 987654321


def test_a_reply_link_is_not_mistaken_for_a_jump_link():
    content = "-# Replying to **someone**: https://discord.com/channels/1/2/555"
    assert source_id_from_replay(content) is None


@pytest.mark.parametrize("content", [None, "", "no link at all"])
def test_source_id_returns_none_without_an_anchor(content):
    assert source_id_from_replay(content) is None
