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


def test_an_unresolved_reply_points_at_the_original_without_guessing(
    bot, config, thread, invoker, guild
):
    """Unresolved covers three cases and we cannot tell them apart.

    Outside the thread, not replayed yet, or deleted since: claiming the first
    one is a guess, and it reads as if a message were missing.
    """
    author = FakeAuthor(2, "alice")
    thread.messages.append(
        FakeMessage(2003, author, "answer", channel_id=thread.id,
                    reference=FakeReference(999999))
    )
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    body = "\n".join(call.get("content") or "" for call in guild.channel.webhook.sent)
    assert "In reply to" in body
    assert "outside this thread" not in body, "that asserts more than we know"
    assert f"/{thread.id}/999999" in body, "it should link to the original"


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


def _hook(promoter_id, name, owner=None):
    from conftest import FakeIntegrationWebhook

    return FakeIntegrationWebhook(promoter_id, name, owner)


def test_only_webhooks_that_posted_here_are_offered(bot, config, thread, invoker):
    """A webhook on the parent channel that never posted here is not ours to move."""
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(5000, author, "from a bot", channel_id=thread.id, webhook_id=111))
    populate(thread, 2)
    thread.parent.hooks = [
        _hook(111, "Dofus Bot", "dofus-store-watch"),
        _hook(222, "Some other integration", "unrelated"),
    ]

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    found = run(promoter.discover_webhooks())

    assert [f["id"] for f in found] == [111]
    assert found[0]["name"] == "Dofus Bot"
    assert found[0]["owner"] == "dofus-store-watch#0001"
    assert found[0]["messages"] == 1


def test_webhooks_are_ordered_by_how_much_they_posted(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    for i in range(4):
        thread.messages.append(FakeMessage(5100 + i, author, "a", channel_id=thread.id, webhook_id=222))
    thread.messages.append(FakeMessage(5200, author, "b", channel_id=thread.id, webhook_id=111))
    thread.parent.hooks = [_hook(111, "Quiet"), _hook(222, "Chatty")]

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    found = run(promoter.discover_webhooks())
    assert [f["name"] for f in found] == ["Chatty", "Quiet"]


def test_a_deleted_webhook_is_reported_rather_than_hidden(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(5300, author, "x", channel_id=thread.id, webhook_id=999))
    thread.parent.hooks = []

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    found = run(promoter.discover_webhooks())
    assert len(found) == 1 and found[0]["gone"] is True


def test_no_webhook_means_nothing_to_offer(bot, config, thread, invoker):
    populate(thread, 3)
    thread.parent.hooks = [_hook(111, "Unused")]
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    assert run(promoter.discover_webhooks()) == []


def test_missing_permission_says_which_one(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(5400, author, "x", channel_id=thread.id, webhook_id=111))
    thread.parent.hooks_forbidden = True
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    with pytest.raises(PromotionError, match="Manage Webhooks"):
        run(promoter.discover_webhooks())


def test_migrating_moves_the_webhook_and_keeps_its_identity(bot, config, thread, invoker, guild):
    """Editing the channel preserves id and token, so the URL keeps working."""
    hook = _hook(111, "Dofus Bot", "dofus-store-watch")
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    run(promoter.migrate_webhook(hook))

    assert hook.edits, "the webhook was not edited"
    assert hook.edits[0]["channel"] is guild.channel
    assert hook.id == 111, "the id must not change, the URL depends on it"
    assert str(thread.id) in hook.edits[0]["reason"]


def test_migrating_without_a_target_channel_refuses(bot, config, thread, invoker):
    promoter = make_promoter(bot, config, thread, invoker)
    with pytest.raises(PromotionError):
        run(promoter.migrate_webhook(_hook(111, "Dofus Bot")))


def test_discovery_does_not_write_to_the_source_thread(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(5500, author, "x", channel_id=thread.id, webhook_id=111))
    thread.parent.hooks = [_hook(111, "Dofus Bot")]
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    try:
        run(promoter.discover_webhooks())
    except ThreadWriteAttempt as exc:  # pragma: no cover
        pytest.fail(str(exc))


def test_pins_survive_a_lost_checkpoint(bot, config, thread, invoker, guild):
    """Pins must not depend on the checkpoint surviving.

    Everything else about resuming was made independent of it: the channel is
    the record. Pins were the exception, accumulated in the checkpoint during
    the replay and lost with it.
    """
    messages = populate(thread, 4)
    messages[1].pinned = True
    messages[3].pinned = True

    first = make_promoter(bot, config, thread, invoker)
    run(first.run())
    assert len(guild.channel.pinned) == 2

    # Same channel, same content, but the checkpoint is gone.
    guild.channel.pinned.clear()
    second = make_promoter(bot, config, thread, invoker)
    second.checkpoint.target_channel_id = guild.channel.id
    second.checkpoint.pinned_source_ids.clear()
    run(second.run())

    assert len(guild.channel.pinned) == 2, "pins were lost with the checkpoint"


def test_pins_are_restored_in_chronological_order(bot, config, thread, invoker, guild):
    """Discord lists pins newest first; the target must not end up reversed."""
    messages = populate(thread, 5)
    for i in (0, 2, 4):
        messages[i].pinned = True

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    ordre = [promoter.checkpoint.translated(messages[i].id) for i in (0, 2, 4)]
    assert guild.channel.pinned == ordre, "pins were restored out of order"


def test_unreadable_pins_fall_back_to_the_checkpoint(bot, config, thread, invoker, guild):
    messages = populate(thread, 3)
    messages[1].pinned = True
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    run(promoter._replay_one(messages[1], 1))

    thread.pins_forbidden = True
    run(promoter._restore_pins())
    assert len(guild.channel.pinned) == 1, "the checkpoint should have covered it"


def test_the_checkpoint_is_not_rewritten_on_every_message(bot, config, thread, invoker):
    """Rewriting it per message costs tens of gigabytes on a large thread.

    Safe to batch precisely because the checkpoint is not the authority: the
    channel is, so losing a few entries costs one extra read.
    """
    from app.promoter import CHECKPOINT_EVERY

    populate(thread, CHECKPOINT_EVERY * 2)
    promoter = make_promoter(bot, config, thread, invoker)

    ecritures = {"n": 0}
    vrai_save = promoter.store.save

    def compte(checkpoint):
        ecritures["n"] += 1
        return vrai_save(checkpoint)

    promoter.store.save = compte
    run(promoter.run())

    assert promoter.progress.sent == CHECKPOINT_EVERY * 2
    assert ecritures["n"] < CHECKPOINT_EVERY, (
        f"{ecritures['n']} writes for {CHECKPOINT_EVERY * 2} messages, still per-message"
    )


def test_a_batched_checkpoint_still_resumes_without_duplicating(bot, config, thread, invoker, guild):
    """The point of batching: what is lost is recoverable from the channel."""
    populate(thread, 8)
    first = make_promoter(bot, config, thread, invoker)
    run(first.run())
    avant = replayed_source_ids(guild.channel)

    # Simulate a hard stop: the checkpoint lags behind what the channel holds.
    second = make_promoter(bot, config, thread, invoker)
    second.checkpoint.target_channel_id = guild.channel.id
    second.checkpoint.last_source_id = 1002
    second.checkpoint.id_map.clear()
    run(second.run())

    assert replayed_source_ids(guild.channel) == avant, "a lagging checkpoint caused a re-replay"
    assert second.checkpoint.id_map, "the id map should have been rebuilt from the channel"


def test_the_channel_is_created_closed(bot, config, thread, invoker, guild):
    """Thirty thousand messages into an open channel floods a whole server.

    Silent sends suppress the push notification, not the unread badge, so the
    only real answer is to keep the door shut while the replay runs.
    """
    populate(thread, 3)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    overwrites = guild.created_channels[0]["overwrites"]
    assert overwrites[guild.default_role].view_channel is False, "everyone can see it"
    assert overwrites[guild.me].view_channel is True, "the bot must see its own channel"


def test_the_invoker_can_see_what_they_asked_for(bot, config, thread, invoker, guild):
    populate(thread, 2)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    overwrites = guild.created_channels[0]["overwrites"]
    vus = [o for o in overwrites if getattr(o, "id", None) == invoker.id]
    assert vus, "whoever ran the command should see the result"
    assert overwrites[vus[0]].view_channel is True


def test_a_chosen_role_gets_access_during_the_replay(bot, config, thread, invoker, guild):
    role = FakeAuthor(555, "Admins")
    populate(thread, 2)
    promoter = make_promoter(bot, config, thread, invoker, visible_to=role)
    run(promoter.prepare())

    overwrites = guild.created_channels[0]["overwrites"]
    assert role in overwrites and overwrites[role].view_channel is True


def test_progress_reports_a_share_of_the_whole(bot, config, thread, invoker):
    """Without a denominator you cannot tell 0.3% from 90%, which cost a run."""
    populate(thread, 4)
    thread.message_count = 1000
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    ligne = promoter.status_line("Replay in progress:")
    assert "of about 1000" in ligne
    assert "%" in ligne


def test_progress_counts_what_a_previous_run_had_done(bot, config, thread, invoker, guild):
    populate(thread, 6)
    thread.message_count = 6
    run(make_promoter(bot, config, thread, invoker).run())

    second = make_promoter(bot, config, thread, invoker)
    run(second.run())
    ligne = second.status_line("Replay complete:")
    assert "6 of about 6" in ligne, f"a resume restarted the count: {ligne}"


def test_progress_stays_usable_without_a_count(bot, config, thread, invoker):
    populate(thread, 2)
    thread.message_count = None
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert "message(s) replayed" in promoter.status_line("Replay in progress:")


def test_messages_posted_during_the_replay_are_picked_up(bot, config, thread, invoker, guild):
    """A sixteen hour replay runs while people keep talking in the thread.

    Each page of history is fetched when the cursor reaches it, and a new
    message always has a higher snowflake than the cursor, so it lands in a
    later page rather than being missed.
    """
    author = FakeAuthor(2, "alice")
    populate(thread, 5)
    promoter = make_promoter(bot, config, thread, invoker)

    original = promoter._replay_one
    arrive = {"fait": False}

    async def pendant(message, index):
        await original(message, index)
        if not arrive["fait"] and promoter.progress.sent == 2:
            # Someone posts while the replay is halfway through.
            thread.messages.append(
                FakeMessage(9999, author, "posted mid-replay", channel_id=thread.id)
            )
            arrive["fait"] = True

    promoter._replay_one = pendant
    run(promoter.run())

    assert 9999 in replayed_source_ids(guild.channel), "a live message was missed"
    body = "\n".join(c.get("content") or "" for c in guild.channel.webhook.sent)
    assert "posted mid-replay" in body


def test_history_is_read_page_by_page_not_all_at_once(bot, config, thread, invoker):
    """Buffering the whole history would defeat both streaming and catch-up."""
    populate(thread, 250)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    vus = []

    async def collecte():
        async for message in promoter._iter_source(None):
            vus.append(message.id)
            if len(vus) == 150:
                thread.messages.append(
                    FakeMessage(99999, FakeAuthor(2, "alice"), "late", channel_id=thread.id)
                )

    run(collecte())
    assert 99999 in vus, "a message added after page one was never seen"
    assert len(vus) == 251


def test_webhooks_are_noted_during_the_replay(bot, config, thread, invoker):
    """Rediscovering them later means walking the whole thread again.

    On a thirty thousand message thread that is 300 API calls and several
    minutes, long enough to risk outliving the interaction it answers.
    """
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(6000, author, "from a bot", channel_id=thread.id, webhook_id=111))
    populate(thread, 3)
    thread.parent.hooks = [_hook(111, "Dofus Bot", "dofus")]

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())
    assert promoter.checkpoint.seen_webhook_ids == [111]


def test_discovery_uses_what_the_replay_noted_instead_of_rereading(bot, config, thread, invoker):
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(6100, author, "x", channel_id=thread.id, webhook_id=222))
    populate(thread, 2)
    thread.parent.hooks = [_hook(222, "Some bot")]

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.run())

    lectures = {"n": 0}
    original = promoter._iter_source

    def compte(after_id):
        lectures["n"] += 1
        return original(after_id)

    promoter._iter_source = compte
    found = run(promoter.discover_webhooks())

    assert [f["id"] for f in found] == [222]
    assert lectures["n"] == 0, "the thread was walked again for nothing"


def test_discovery_still_works_without_a_record(bot, config, thread, invoker):
    """A channel promoted by an older build, or with /promote-link, has none."""
    author = FakeAuthor(2, "alice")
    thread.messages.append(FakeMessage(6200, author, "x", channel_id=thread.id, webhook_id=333))
    thread.parent.hooks = [_hook(333, "Legacy bot")]

    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())
    promoter.checkpoint.seen_webhook_ids.clear()

    found = run(promoter.discover_webhooks())
    assert [f["id"] for f in found] == [333], "the fallback should still find it"


def test_the_bot_always_gets_access_to_the_channel_it_creates(bot, config, thread, invoker, guild):
    """A closed channel the bot cannot see is one it cannot pin in.

    Messages still land, because the webhook carries its own right to post,
    which is precisely what makes this failure quiet: thirty thousand messages
    arrive and every pin silently 403s.
    """
    populate(thread, 2)
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    overwrites = guild.created_channels[0]["overwrites"]
    assert guild.me in overwrites, "the bot has no overwrite of its own"
    acces = overwrites[guild.me]
    assert acces.view_channel is True
    assert acces.manage_messages is True, "no Manage Messages means no pins"
    assert acces.manage_webhooks is True


def test_the_bot_access_does_not_depend_on_a_warm_cache(bot, config, thread, invoker, guild):
    """guild.me reads a cache that can be empty, and returning None there is
    how the bot ended up locked out of a channel it had just created."""
    populate(thread, 2)
    guild.me = None
    promoter = make_promoter(bot, config, thread, invoker)
    run(promoter.prepare())

    overwrites = guild.created_channels[0]["overwrites"]
    membres = [o for o in overwrites if getattr(o, "name", None) == "promoter-bot"]
    assert membres, "the member should have been fetched rather than skipped"


def test_a_channel_the_bot_cannot_work_in_fails_loudly(bot, config, thread, invoker, guild):
    """Discovering this five hours into a replay is too late."""
    populate(thread, 2)
    guild.channel.bot_permissions = discord.Permissions(view_channel=True, send_messages=True)

    promoter = make_promoter(bot, config, thread, invoker)
    with pytest.raises(PromotionError) as caught:
        run(promoter.prepare())

    message = str(caught.value)
    assert "Manage Messages" in message, "it should name what is missing"
    assert "cannot work in it" in message
