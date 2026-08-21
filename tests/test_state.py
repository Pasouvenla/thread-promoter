"""Unit tests on the checkpoint store. The file on disk is the only thing
standing between an interruption and a wasted run, so it is tested as such."""

from __future__ import annotations

import json
import stat

import pytest

from app.state import Checkpoint, CheckpointStore, Manifest


@pytest.fixture
def store(tmp_path):
    return CheckpointStore(tmp_path / "state")


def test_missing_file_yields_a_fresh_checkpoint(store):
    checkpoint = store.load(1, 2)
    assert checkpoint.thread_id == 1
    assert checkpoint.guild_id == 2
    assert checkpoint.id_map == {}


def test_round_trip(store):
    checkpoint = store.load(1, 2)
    checkpoint.remember(10, 100)
    checkpoint.skip(11)
    checkpoint.pinned_source_ids.append(10)
    store.save(checkpoint)

    reloaded = store.load(1, 2)
    assert reloaded.translated(10) == 100
    assert reloaded.skipped_source_ids == [11]
    assert reloaded.last_source_id == 11
    assert reloaded.pinned_source_ids == [10]


def test_saved_file_is_not_world_readable(store):
    checkpoint = store.load(1, 2)
    checkpoint.webhook_token = "a-live-token"
    store.save(checkpoint)
    mode = (store._path(1).stat().st_mode) & 0o777
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH


def test_a_truncated_file_does_not_raise(store):
    checkpoint = store.load(1, 2)
    checkpoint.remember(10, 100)
    store.save(checkpoint)
    path = store._path(1)
    path.write_text(path.read_text()[: len(path.read_text()) // 2])

    recovered = store.load(1, 2)
    assert recovered.id_map == {}
    assert recovered.thread_id == 1


def test_a_corrupted_file_is_kept_aside_rather_than_deleted(store, tmp_path):
    store.save(store.load(1, 2))
    store._path(1).write_text("{not json")
    store.load(1, 2)
    quarantined = list((tmp_path / "state").glob("1.corrupt-*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{not json"


def test_an_unknown_field_does_not_raise(store):
    store._path(1).write_text(
        json.dumps({"thread_id": 1, "guild_id": 2, "from_a_newer_build": True})
    )
    checkpoint = store.load(1, 2)
    assert checkpoint.thread_id == 1


def test_skipped_messages_never_land_in_the_id_map():
    checkpoint = Checkpoint(thread_id=1, guild_id=2)
    checkpoint.skip(10)
    assert checkpoint.translated(10) is None
    assert 10 not in [int(k) for k in checkpoint.id_map]


def test_translated_returns_none_and_never_a_zero():
    checkpoint = Checkpoint(thread_id=1, guild_id=2)
    checkpoint.skip(10)
    for source_id in (None, 10, 999):
        assert checkpoint.translated(source_id) is None


def test_reset_clears_everything_but_identity():
    checkpoint = Checkpoint(thread_id=1, guild_id=2)
    checkpoint.remember(10, 100)
    checkpoint.skip(11)
    checkpoint.webhook_token = "token"
    checkpoint.target_channel_id = 55
    checkpoint.reset()
    assert checkpoint.thread_id == 1 and checkpoint.guild_id == 2
    assert checkpoint.id_map == {} and checkpoint.skipped_source_ids == []
    assert checkpoint.webhook_token is None and checkpoint.target_channel_id is None


def test_recording_a_failure_twice_keeps_one_entry():
    checkpoint = Checkpoint(thread_id=1, guild_id=2)
    checkpoint.record_failure({"source_id": 10, "reason": "first"})
    checkpoint.record_failure({"source_id": 10, "reason": "second"})
    assert len(checkpoint.failures) == 1
    assert checkpoint.failures[0]["reason"] == "second"


def test_save_leaves_no_temporary_file_behind(store):
    store.save(store.load(1, 2))
    assert not list(store.directory.glob("*.tmp"))


def test_manifest_is_not_world_readable(tmp_path):
    manifest = Manifest(tmp_path / "manifests", 1)
    manifest.add_message({"source_id": 1, "content": "private"})
    manifest.flush()
    mode = manifest.path.stat().st_mode & 0o777
    assert not mode & stat.S_IRGRP and not mode & stat.S_IROTH
    assert json.loads(manifest.path.read_text())["messages"][0]["content"] == "private"


def test_the_manifest_survives_a_run_that_replays_nothing(tmp_path):
    """A resume with nothing left to do used to flush an empty payload over it.

    The manifest is the only record of what the replay could not carry over,
    so losing it loses the per-user reactions and the oversized attachments.
    """
    first = Manifest(tmp_path, 1)
    first.set_header({"name": "a thread"}, {"id": 55})
    first.add_message({"source_id": 10, "content": "hello"})
    first.warn("something was lost")
    first.flush()

    second = Manifest(tmp_path, 1)
    second.flush()

    reloaded = json.loads((tmp_path / "1.json").read_text())
    assert len(reloaded["messages"]) == 1, "the archive was wiped"
    assert reloaded["thread"]["name"] == "a thread"
    assert reloaded["warnings"] == ["something was lost"]


def test_a_later_run_adds_to_the_archive(tmp_path):
    first = Manifest(tmp_path, 1)
    first.add_message({"source_id": 10, "content": "one"})
    first.flush()

    second = Manifest(tmp_path, 1)
    second.add_message({"source_id": 20, "content": "two"})
    second.flush()

    messages = json.loads((tmp_path / "1.json").read_text())["messages"]
    assert [m["source_id"] for m in messages] == [10, 20]


def test_replaying_a_message_twice_keeps_one_entry(tmp_path):
    manifest = Manifest(tmp_path, 1)
    manifest.add_message({"source_id": 10, "content": "first attempt"})
    manifest.add_message({"source_id": 10, "content": "after recovery"})
    assert len(manifest.payload["messages"]) == 1
    assert manifest.payload["messages"][0]["content"] == "after recovery"


def test_the_archive_stays_in_chronological_order(tmp_path):
    """Ordered by snowflake, because index restarts at one on every resume."""
    manifest = Manifest(tmp_path, 1)
    for source_id in (30, 10, 20):
        manifest.add_message({"source_id": source_id, "index": 1})
    assert [m["source_id"] for m in manifest.payload["messages"]] == [10, 20, 30]


def test_a_warning_is_not_repeated_across_runs(tmp_path):
    first = Manifest(tmp_path, 1)
    first.warn("huge.zip was too large")
    first.flush()
    second = Manifest(tmp_path, 1)
    second.warn("huge.zip was too large")
    assert second.payload["warnings"] == ["huge.zip was too large"]


def test_an_unreadable_manifest_does_not_stop_a_run(tmp_path):
    (tmp_path / "1.json").write_text("{ truncated")
    manifest = Manifest(tmp_path, 1)
    manifest.add_message({"source_id": 10})
    manifest.flush()
    assert len(json.loads((tmp_path / "1.json").read_text())["messages"]) == 1


def test_an_empty_header_does_not_erase_a_recorded_one(tmp_path):
    first = Manifest(tmp_path, 1)
    first.set_header({"name": "kept"}, {"id": 5})
    first.flush()
    second = Manifest(tmp_path, 1)
    second.set_header({}, {})
    assert second.payload["thread"]["name"] == "kept"
