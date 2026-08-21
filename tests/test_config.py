"""Configuration is the surface an operator touches, so bad values must be
loud and harmless rather than quiet and fatal."""

from __future__ import annotations

import logging

import pytest

from app.config import Config


@pytest.fixture(autouse=True)
def base_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "a-secret-token")
    for name, sub in (
        ("PROMOTER_STATE_DIR", "state"),
        ("PROMOTER_MANIFEST_DIR", "manifests"),
        ("PROMOTER_ATTACHMENT_CACHE", "attachments"),
        ("PROMOTER_EXPORT_DIR", "exports"),
    ):
        monkeypatch.setenv(name, str(tmp_path / sub))


def test_a_missing_token_fails_loudly(monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN")
    with pytest.raises(RuntimeError):
        Config.load()


def test_the_token_never_appears_in_a_repr():
    config = Config.load()
    assert "a-secret-token" not in repr(config)
    assert config.token == "a-secret-token"


def test_the_token_never_appears_in_a_log_record(caplog):
    config = Config.load()
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("test").debug("config=%s", config)
    assert "a-secret-token" not in caplog.text


def test_a_zero_progress_interval_cannot_divide_by_zero(monkeypatch):
    monkeypatch.setenv("PROMOTER_PROGRESS_EVERY", "0")
    assert Config.load().progress_every >= 1


def test_a_negative_delay_is_clamped(monkeypatch):
    monkeypatch.setenv("PROMOTER_MESSAGE_DELAY", "-5")
    assert Config.load().message_delay > 0


def test_a_nonsense_delay_falls_back_to_the_default(monkeypatch, caplog):
    monkeypatch.setenv("PROMOTER_MESSAGE_DELAY", "soon")
    with caplog.at_level(logging.WARNING):
        config = Config.load()
    assert config.message_delay == 2.0
    assert "PROMOTER_MESSAGE_DELAY" in caplog.text


def test_guild_ids_are_parsed_and_bad_entries_reported(monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_GUILD_IDS", "123, 456;789, oops")
    with caplog.at_level(logging.WARNING):
        config = Config.load()
    assert config.guild_ids == (123, 456, 789)
    assert "oops" in caplog.text


def test_state_directories_are_private(tmp_path):
    config = Config.load()
    for directory in (config.state_dir, config.manifest_dir, config.attachment_cache):
        assert directory.stat().st_mode & 0o077 == 0


def test_an_unusable_data_directory_says_what_to_do(tmp_path, monkeypatch):
    """The common deployment mistake is a host directory owned by someone else."""
    locked = tmp_path / "locked"
    locked.mkdir()
    monkeypatch.setenv("PROMOTER_STATE_DIR", str(locked / "state"))
    locked.chmod(0o500)
    try:
        with pytest.raises(RuntimeError) as caught:
            Config.load()
    finally:
        locked.chmod(0o700)
    message = str(caught.value)
    assert "chown" in message, "the error must name the fix, not just the failure"
    assert str(locked / "state") in message
