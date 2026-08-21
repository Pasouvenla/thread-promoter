"""Integration tests against a mocked discord.py layer.

These cover the paths where a mistake is expensive: writing to the source,
duplicating a history, and losing content without saying so.
"""

from __future__ import annotations

import asyncio

import discord
import pytest

from app.promoter import PromotionError, ThreadPromoter
from app.renderers import source_id_from_replay
from conftest import (
    FakeAttachment,
    FakeAuthor,
    FakeMessage,
    FakeReaction,
    FakeReference,
    ThreadWriteAttempt,
)


def run(coro):
    return asyncio.run(coro)


def make_promoter(bot, config, thread, invoker, **kwargs):
    return ThreadPromoter(bot, config, thread, invoker, **kwargs)


def populate(thread, count: int = 5) -> list[FakeMessage]:
    author = FakeAuthor(2, "alice")
    messages = [
        FakeMessage(1000 + i, author, f"message number {i}", channel_id=thread.id)
        for i in range(count)
    ]
    thread.messages.extend(messages)
    return messages


def replayed_source_ids(channel) -> list[int]:
    found = [source_id_from_replay(m.content) for m in channel.messages]
    return [f for f in found if f is not None]


def test_nominal_replay_carries_every_message(bot, config, thread, invoker, guild):
    populate(thread, 5)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    assert replayed_source_ids(guild.channel) == [1000, 1001, 1002, 1003, 1004]
    assert promoter.progress.sent == 5
    assert promoter.progress.failed == 0


def test_the_source_thread_is_never_written_to(bot, config, thread, invoker):
    """Regression guard. Any write to the source breaks the retry story."""
    populate(thread, 5)
    thread.messages[2].pinned = True
    promoter = make_promoter(bot, config, thread, invoker)
    try:
        run(promoter.run())
    except ThreadWriteAttempt as exc:  # pragma: no cover
        pytest.fail(str(exc))


def test_reporting_lands_in_the_target_channel(bot, config, thread, invoker, guild):
    populate(thread, 3)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert guild.channel.plain_sends, "the header must be posted in the target channel"


def test_a_resume_does_not_replay_what_is_already_there(bot, config, thread, invoker, guild):
    populate(thread, 6)
    first = make_promoter(bot, config, thread, invoker)
    run(first.run())
    before = len(guild.channel.messages)

    # Same thread, same checkpoint, run again.
    second = make_promoter(bot, config, thread, invoker)
    run(second.run())

    assert len(guild.channel.messages) == before, "the replay was done twice"
    assert second.progress.sent == 0


def test_a_resume_picks_up_where_the_channel_stopped(bot, config, thread, invoker, guild):
    populate(thread, 6)
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.channel = guild.channel
    promoter.webhook = guild.channel.webhook
    promoter.checkpoint.target_channel_id = guild.channel.id
    # Only the first three made it across on the previous run.
    run(promoter._replay_one(thread.messages[0], 1))
    run(promoter._replay_one(thread.messages[1], 2))
    run(promoter._replay_one(thread.messages[2], 3))

    resumed = make_promoter(bot, config, thread, invoker)
    run(resumed.run())

    assert replayed_source_ids(guild.channel) == [1000, 1001, 1002, 1003, 1004, 1005]


def test_a_lost_checkpoint_does_not_cause_a_second_replay(bot, config, thread, invoker, guild):
    """The channel is the record, the checkpoint is only a cache."""
    populate(thread, 5)
    first = make_promoter(bot, config, thread, invoker)
    run(first.run())
    before = replayed_source_ids(guild.channel)

    # The checkpoint file is gone, but the channel id is still known.
    second = make_promoter(bot, config, thread, invoker)
    second.checkpoint.target_channel_id = guild.channel.id
    second.checkpoint.id_map.clear()
    second.checkpoint.last_source_id = None
    run(second.run())

    assert replayed_source_ids(guild.channel) == before


def test_an_uncached_channel_is_not_treated_as_deleted(bot, config, thread, invoker, guild):
    """This is the path that used to duplicate an entire history."""
    populate(thread, 3)
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.checkpoint.target_channel_id = guild.channel.id
    run(promoter.prepare())

    assert promoter.channel is guild.channel
    assert guild.created_channels == [], "a second channel was created"


def test_an_unreachable_channel_aborts_instead_of_starting_over(bot, config, thread, invoker, guild):
    populate(thread, 3)
    bot.unreachable = True
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.checkpoint.target_channel_id = guild.channel.id

    with pytest.raises(PromotionError):
        run(promoter.prepare())
    assert promoter.checkpoint.target_channel_id == guild.channel.id, "checkpoint was reset blind"
    assert guild.created_channels == []


def test_a_deleted_channel_does_reset_the_checkpoint(bot, config, thread, invoker, guild):
    populate(thread, 3)
    bot.missing_channels.add(4242)
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.checkpoint.target_channel_id = 4242
    run(promoter.prepare())

    assert guild.created_channels, "a fresh channel should be created"
    assert promoter.checkpoint.target_channel_id == guild.channel.id


def test_a_thread_without_a_creation_date_still_runs(bot, config, thread, invoker):
    """Thread.created_at is None for threads made before 9 January 2022."""
    thread.created_at = None
    populate(thread, 2)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert promoter.progress.sent == 2


def test_a_message_with_only_an_attachment_keeps_its_file(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    message = FakeMessage(2000, author, "", channel_id=thread.id,
                          attachments=[FakeAttachment(1, "photo.png", 1024, b"bytes")])
    thread.messages.append(message)

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    sent_with_files = [call for call in guild.channel.webhook.sent if call.get("files")]
    assert sent_with_files, "the attachment never left"
    assert sent_with_files[0]["files"][0].filename == "photo.png"


def test_an_oversized_attachment_is_named_in_the_message(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    big = FakeAttachment(1, "huge.zip", 50 * 1024 * 1024)
    thread.messages.append(FakeMessage(2001, author, "look", channel_id=thread.id, attachments=[big]))

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    body = "\n".join(call.get("content") or "" for call in guild.channel.webhook.sent)
    assert "huge.zip" in body
    assert "not carried over" in body
    assert promoter.progress.attachments_lost == 1


def test_an_unreadable_attachment_is_reported_rather_than_dropped(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    broken = FakeAttachment(1, "broken.png", 2048)
    broken.fail = True
    thread.messages.append(FakeMessage(2002, author, "see", channel_id=thread.id, attachments=[broken]))

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    body = "\n".join(call.get("content") or "" for call in guild.channel.webhook.sent)
    assert "broken.png" in body and "read failed" in body


def test_a_reply_outside_the_thread_is_labelled(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(2003, author, "answer", channel_id=thread.id,
                    reference=FakeReference(999999))
    )
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    body = "\n".join(call.get("content") or "" for call in guild.channel.webhook.sent)
    assert "Replying to" in body


def test_a_reply_inside_the_thread_points_at_the_replayed_copy(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    first = FakeMessage(3000, author, "question", channel_id=thread.id)
    second = FakeMessage(3001, author, "answer", channel_id=thread.id,
                         reference=FakeReference(3000))
    thread.messages.extend([first, second])

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    body = "\n".join(call.get("content") or "" for call in guild.channel.webhook.sent)
    assert f"/{guild.channel.id}/" in body, "the reply should point into the new channel"


def test_an_inaccessible_emoji_does_not_stop_the_replay(bot, config, thread, invoker, guild):
    import dataclasses

    config = dataclasses.replace(config, replay_reactions=True)
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(2004, author, "reacted", channel_id=thread.id,
                    reactions=[FakeReaction("<:secret:123>", 2, ["bob", "carol"])])
    )
    promoter = make_promoter(bot, config, thread, invoker)

    async def refuse(*args, **kwargs):
        raise discord.HTTPException(type("R", (), {"status": 400, "reason": "x"})(), "no access")

    from conftest import FakePartialMessage

    original = FakePartialMessage.add_reaction
    FakePartialMessage.add_reaction = refuse
    try:
        run(promoter.run())
    finally:
        FakePartialMessage.add_reaction = original

    assert promoter.progress.sent == 1
    body = "\n".join(call.get("content") or "" for call in guild.channel.webhook.sent)
    assert "Original reactions" in body, "the reaction should survive as text"
    assert any("could not be replayed" in w for w in promoter.manifest.payload["warnings"])


def test_a_failed_message_leaves_a_placeholder_holding_its_slot(bot, config, thread, invoker, guild):
    populate(thread, 3)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    guild.channel.webhook.failures_left = 1

    run(promoter._replay_one(thread.messages[0], 1)) if False else None
    try:
        run(promoter._replay_one(thread.messages[0], 1))
    except discord.HTTPException as exc:
        run(promoter._handle_failure(thread.messages[0], 1, exc))

    assert promoter.checkpoint.failures
    entry = promoter.checkpoint.failures[0]
    assert entry["placeholder_id"], "the slot must be held"
    assert entry["source_id"] == 1000


def test_a_placeholder_anchors_a_resume(bot, config, thread, invoker, guild):
    """A failed message must not be replayed twice by the next run."""
    populate(thread, 3)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    try:
        raise discord.HTTPException(type("R", (), {"status": 400, "reason": "x"})(), "boom")
    except discord.HTTPException as exc:
        run(promoter._handle_failure(thread.messages[0], 1, exc))

    assert 1000 in replayed_source_ids(guild.channel)


def test_the_webhook_is_deleted_once_the_run_is_clean(bot, config, thread, invoker, guild):
    populate(thread, 2)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert guild.channel.webhook.deleted
    assert promoter.checkpoint.webhook_token is None


def test_promote_link_creates_the_channel_without_replaying(bot, config, thread, invoker, guild):
    populate(thread, 4)
    promoter = make_promoter(bot, config, thread, invoker, full_replay=False)
    run(promoter.run())

    assert guild.created_channels
    assert promoter.progress.sent == 0
    assert replayed_source_ids(guild.channel) == []


def test_promote_link_points_back_at_the_source_thread(bot, config, thread, invoker, guild):
    promoter = make_promoter(bot, config, thread, invoker, full_replay=False)
    run(promoter.run())

    embed = guild.channel.plain_sends[0]["embed"]
    assert f"/{thread.id}" in embed.description, "nothing links back to the thread"


def test_the_header_is_written_once_across_runs(bot, config, thread, invoker, guild):
    populate(thread, 3)
    run(make_promoter(bot, config, thread, invoker).run())
    headers = [s for s in guild.channel.plain_sends if s.get("embed") is not None]
    run(make_promoter(bot, config, thread, invoker).run())
    headers_after = [s for s in guild.channel.plain_sends if s.get("embed") is not None]
    assert len(headers) == len(headers_after) == 1


def test_a_private_thread_grants_the_bot_access_to_its_own_channel(bot, config, thread, invoker, guild):
    thread._private = True
    populate(thread, 1)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    overwrites = guild.created_channels[0]["overwrites"]
    assert guild.me in overwrites, "the bot cannot see the channel it just made"
    assert overwrites[guild.me].view_channel is True


def test_verify_reports_nothing_missing_after_a_clean_run(bot, config, thread, invoker):
    populate(thread, 4)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    result = run(promoter.verify())

    assert result["missing"] == []
    assert result["found_in_target"] == 4


def test_verify_names_what_is_missing(bot, config, thread, invoker):
    populate(thread, 4)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    thread.messages.append(FakeMessage(1099, FakeAuthor(2, "alice"), "late", channel_id=thread.id))

    result = run(promoter.verify())
    assert result["missing"] == [1099]


def test_pins_are_restored_in_the_target(bot, config, thread, invoker, guild):
    messages = populate(thread, 3)
    messages[1].pinned = True
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert len(guild.channel.pinned) == 1


def test_a_system_message_is_skipped_but_still_anchors(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(2100, author, "", channel_id=thread.id,
                    message_type=discord.MessageType.pins_add)
    )
    thread.messages.append(FakeMessage(2101, author, "after", channel_id=thread.id))

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert promoter.progress.skipped == 1
    assert 2100 in replayed_source_ids(guild.channel)


def test_the_starter_message_is_replayed_when_it_exists(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    thread.parent.starter = FakeMessage(thread.id, author, "the opening post", channel_id=42)
    populate(thread, 2)

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert thread.id in replayed_source_ids(guild.channel)


def test_an_unexpected_error_is_not_swallowed_as_a_failed_message(bot, config, thread, invoker):
    """A programming error must surface, not hide under a placeholder."""
    populate(thread, 3)
    promoter = make_promoter(bot, config, thread, invoker)

    async def boom(*args, **kwargs):
        raise TypeError("a genuine bug")

    promoter._replay_one = boom
    with pytest.raises(TypeError):
        run(promoter.run())


def test_emit_attaches_files_even_with_no_text_block(bot, config, thread, invoker, guild):
    """Guards the logic, not just the path that reaches it.

    Annotations currently guarantee a non-empty block list, which hides this
    case from the end-to-end tests. If those annotations ever become optional
    again, the files must still go out.
    """
    from app.promoter import Payload

    author = FakeAuthor(2, "alice")
    message = FakeMessage(4000, author, "", channel_id=thread.id)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    payload = Payload(blocks=[], files=["a-file"], embeds=["an-embed"])
    run(promoter._emit(message, payload))

    call = guild.channel.webhook.sent[-1]
    assert call["files"] == ["a-file"], "the attachment was dropped"
    assert call["embeds"] == ["an-embed"]


def test_preview_creates_absolutely_nothing(bot, config, thread, invoker, guild):
    populate(thread, 6)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.preview())

    assert guild.created_channels == [], "a channel was created by a preview"
    assert guild.channel.messages == [], "a message was emitted by a preview"
    assert guild.channel.webhook.sent == [], "the webhook was used by a preview"
    assert promoter.channel is None and promoter.webhook is None


def test_preview_counts_what_a_replay_would_do(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    populate(thread, 4)
    thread.messages.append(
        FakeMessage(2200, author, "", channel_id=thread.id,
                    message_type=discord.MessageType.pins_add)
    )
    thread.messages.append(
        FakeMessage(2201, author, "with a file", channel_id=thread.id,
                    attachments=[FakeAttachment(1, "small.png", 1024)])
    )

    result = run(make_promoter(bot, config, thread, invoker).preview())
    assert result["total"] == 6
    assert result["replayable"] == 5
    assert result["skipped"] == 1
    assert result["attachments"] == 1
    assert result["estimated_seconds"] > 0


def test_preview_flags_attachments_that_will_not_fit(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(2300, author, "big", channel_id=thread.id,
                    attachments=[FakeAttachment(1, "huge.zip", 50 * 1024 * 1024)])
    )
    result = run(make_promoter(bot, config, thread, invoker).preview())
    assert result["oversized"] == 1
    assert any("huge.zip" in w for w in result["manifest"]["warnings"])


def test_preview_renders_to_html(bot, config, thread, invoker):
    from app.export import render_manifest

    populate(thread, 3)
    result = run(make_promoter(bot, config, thread, invoker).preview())
    page = render_manifest(result["manifest"])
    assert "message number 0" in page and "<html" in page


def test_an_abort_stops_the_replay_between_messages(bot, config, thread, invoker, guild):
    populate(thread, 10)
    promoter = make_promoter(bot, config, thread, invoker)

    original = promoter._replay_one
    calls = {"n": 0}

    async def counting(message, index):
        calls["n"] += 1
        await original(message, index)
        if calls["n"] == 3:
            promoter.request_abort()

    promoter._replay_one = counting
    run(promoter.run())

    assert promoter.progress.sent == 3
    assert len(replayed_source_ids(guild.channel)) == 3
    assert not promoter.checkpoint.done, "an aborted run is not a finished one"


def test_an_aborted_run_can_be_resumed(bot, config, thread, invoker, guild):
    populate(thread, 8)
    first = make_promoter(bot, config, thread, invoker)
    original = first._replay_one
    calls = {"n": 0}

    async def counting(message, index):
        calls["n"] += 1
        await original(message, index)
        if calls["n"] == 3:
            first.request_abort()

    first._replay_one = counting
    run(first.run())

    second = make_promoter(bot, config, thread, invoker)
    run(second.run())

    assert replayed_source_ids(guild.channel) == [1000 + i for i in range(8)]
    assert second.checkpoint.done


def test_an_abort_before_the_first_message_emits_nothing(bot, config, thread, invoker, guild):
    populate(thread, 5)
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.request_abort()
    run(promoter.run())

    assert promoter.progress.sent == 0
    assert replayed_source_ids(guild.channel) == []


def test_throttling_shows_up_in_the_status_line(bot, config, thread, invoker):
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.rate_limits.bucket_hits = 4
    promoter.rate_limits.seconds_waited = 12.0
    assert "Throttling" in promoter.status_line("Replay in progress:")


def test_a_quiet_run_does_not_mention_throttling(bot, config, thread, invoker):
    promoter = make_promoter(bot, config, thread, invoker)
    promoter.rate_limits.bucket_hits = 0
    promoter.rate_limits.global_hits = 0
    promoter.rate_limits.preemptive_waits = 0
    assert "Throttling" not in promoter.status_line("Replay in progress:")


def test_rate_limits_are_recorded_in_the_manifest(bot, config, thread, invoker):
    populate(thread, 2)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert "rate_limits" in promoter.manifest.payload


def test_a_resume_reattaches_to_the_stored_webhook(bot, config, thread, invoker, guild):
    """An interrupted run keeps its webhook, and picks it back up by token."""
    populate(thread, 6)
    first = make_promoter(bot, config, thread, invoker)
    original = first._replay_one
    calls = {"n": 0}

    async def counting(message, index):
        calls["n"] += 1
        await original(message, index)
        if calls["n"] == 2:
            first.request_abort()

    first._replay_one = counting
    run(first.run())
    assert first.checkpoint.webhook_token, "an unfinished run must keep its webhook"

    guild.partial_calls.clear()
    second = make_promoter(bot, config, thread, invoker)
    run(second.run())

    assert guild.partial_calls, "the stored webhook was not reused"
    webhook_id, token, client = guild.partial_calls[0]
    assert webhook_id == guild.channel.webhook.id
    assert token == "webhook-token"
    assert client is bot, "partial needs the client to send without re-authenticating"


def test_the_thread_starter_pointer_is_not_replayed_twice(bot, config, thread, invoker, guild):
    """Discord heads a thread with an empty pointer to the opening post.

    The opening post is already fetched from the parent channel, so replaying
    the pointer as well produced an empty duplicate carrying a stray
    "Replying to" line.
    """
    author = FakeAuthor(2, "alice")
    thread.parent.starter = FakeMessage(thread.id, author, "the opening post", channel_id=42)
    pointer = FakeMessage(
        9000, author, "", channel_id=thread.id,
        message_type=discord.MessageType.thread_starter_message,
        reference=FakeReference(thread.id),
    )
    thread.messages.append(pointer)
    populate(thread, 2)

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    bodies = [c.get("content") or "" for c in guild.channel.webhook.sent]
    assert sum("the opening post" in b for b in bodies) == 1, "the opening post was duplicated"
    assert 9000 not in replayed_source_ids(guild.channel)
    assert promoter.progress.skipped == 1


def test_a_reply_link_does_not_render_as_a_channel_mention(bot, config, thread, invoker, guild):
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(3100, author, "question", channel_id=thread.id))
    thread.messages.append(
        FakeMessage(3101, author, "answer", channel_id=thread.id, reference=FakeReference(3100))
    )
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    body = "\n".join(c.get("content") or "" for c in guild.channel.webhook.sent)
    assert "Replying to [**" in body, "a bare URL is unfurled into a channel mention"
    assert f"](https://discord.com/channels/{thread.guild.id}/{guild.channel.id}/" in body


def test_preview_does_not_list_what_the_replay_would_skip(bot, config, thread, invoker):
    """The preview page must show what would land, not everything read."""
    author = FakeAuthor(2, "alice")
    thread.parent.starter = FakeMessage(thread.id, author, "the opening post", channel_id=42)
    thread.messages.append(
        FakeMessage(9100, author, "", channel_id=thread.id,
                    message_type=discord.MessageType.thread_starter_message,
                    reference=FakeReference(thread.id))
    )
    populate(thread, 3)

    result = run(make_promoter(bot, config, thread, invoker).preview())
    listed = [m["source_id"] for m in result["manifest"]["messages"]]

    assert 9100 not in listed, "the empty starter pointer was listed"
    assert result["total"] == 5 and result["replayable"] == 4 and result["skipped"] == 1
    assert len(listed) == 4


def test_verify_does_not_report_a_skipped_message_as_missing(bot, config, thread, invoker):
    """The starter pointer is never replayed, so it can never be missing.

    Reporting it advised /promote-resume for a gap no resume could ever close.
    """
    author = FakeAuthor(2, "alice")
    thread.parent.starter = FakeMessage(thread.id, author, "the opening post", channel_id=42)
    thread.messages.append(
        FakeMessage(9200, author, "", channel_id=thread.id,
                    message_type=discord.MessageType.thread_starter_message,
                    reference=FakeReference(thread.id))
    )
    populate(thread, 3)

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    result = run(promoter.verify())

    assert result["missing"] == [], "a message that is never replayed cannot be missing"
    assert result["source_total"] == 5
    assert result["source_replayable"] == 4


def test_a_second_run_does_not_empty_the_manifest(bot, config, thread, invoker):
    """End to end guard on the archive: replay, then run again with nothing to do."""
    populate(thread, 4)
    run(make_promoter(bot, config, thread, invoker).run())

    import json as _json
    path = config.manifest_dir / f"{thread.id}.json"
    before = len(_json.loads(path.read_text())["messages"])
    assert before == 4

    run(make_promoter(bot, config, thread, invoker).run())
    after = len(_json.loads(path.read_text())["messages"])
    assert after == before, "the archive was wiped by a resume with nothing to replay"


def test_reactions_are_not_replayed_by_default(bot, config, thread, invoker, guild):
    """Replayed reactions are all the bot's, which looks right and is not.

    The count and the names survive as text, which is honest about what it is.
    """
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(3200, author, "reacted", channel_id=thread.id,
                    reactions=[FakeReaction("thumb", 3, ["bob", "carol", "dave"])])
    )
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    assert guild.channel.reactions == [], "a reaction was added on someone else's behalf"
    body = "\n".join(c.get("content") or "" for c in guild.channel.webhook.sent)
    assert "Original reactions" in body and "thumb x3" in body
    entry = promoter.manifest.payload["messages"][-1]
    assert entry["reactions"][0]["users"] == ["bob", "carol", "dave"], "detail lost"


def test_reactions_can_be_turned_back_on(bot, config, thread, invoker, guild):
    import dataclasses

    config = dataclasses.replace(config, replay_reactions=True)
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(3201, author, "reacted", channel_id=thread.id,
                    reactions=[FakeReaction("thumb", 2, ["bob", "carol"])])
    )
    run(make_promoter(bot, config, thread, invoker).run())
    assert len(guild.channel.reactions) == 1


def test_the_shipped_default_keeps_reactions_off(config):
    """Pinned so a later refactor cannot quietly flip it back."""
    assert config.replay_reactions is False
