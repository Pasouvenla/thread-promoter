"""The HTML archive is what outlives the guild, so it is tested like output,
not like a debug dump."""

from __future__ import annotations

import json

from app.export import export_manifest, render_manifest

MANIFEST = {
    "thread": {"id": 1, "name": "A discussion", "parent": "general",
               "created_at": "2024-05-01T12:00:00+00:00",
               "url": "https://discord.com/channels/5/1"},
    "messages": [
        {"source_id": 10, "author": "alice#0001", "author_display": "alice",
         "avatar_url": "https://cdn.example/2.png",
         "created_at": "2024-05-01T12:00:00+00:00", "edited_at": None,
         "pinned": True, "content": "hello **world**",
         "attachments": ["photo.png"], "attachments_lost": ["huge.zip"],
         "reactions": [{"emoji": "x", "count": 2, "users": ["bob"]}],
         "reply_to": None},
        {"source_id": 11, "author": "bob#0002", "author_display": "bob",
         "created_at": "2024-05-01T12:05:00+00:00", "content": "reply here",
         "reply_to": 10},
    ],
    "warnings": ["Message 10: huge.zip"],
}


def test_every_message_is_present():
    html = render_manifest(MANIFEST)
    assert "hello" in html and "reply here" in html


def test_markdown_becomes_markup():
    assert "<strong>world</strong>" in render_manifest(MANIFEST)


def test_content_is_escaped():
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["messages"][0]["content"] = "<script>alert(1)</script>"
    html = render_manifest(manifest)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_code_block_survives_as_a_pre():
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["messages"][0]["content"] = "```python\nprint('x')\n```"
    html = render_manifest(manifest)
    assert '<pre><code class="language-python">' in html
    assert "print(&#x27;x&#x27;)" in html or "print('x')" in html


def test_markdown_inside_a_code_block_is_left_alone():
    manifest = json.loads(json.dumps(MANIFEST))
    manifest["messages"][0]["content"] = "```\n**not bold**\n```"
    html = render_manifest(manifest)
    block = html.split("<pre>")[1].split("</pre>")[0]
    assert "**not bold**" in block
    assert "<strong>" not in block


def test_replies_link_to_their_target():
    html = render_manifest(MANIFEST)
    assert 'href="#m10"' in html
    assert 'id="m10"' in html


def test_lost_attachments_are_marked_apart():
    html = render_manifest(MANIFEST)
    assert 'class="lost">huge.zip' in html


def test_warnings_are_carried_over():
    assert "1 warning(s)" in render_manifest(MANIFEST)


def test_the_page_is_self_contained():
    html = render_manifest(MANIFEST)
    assert "<style>" in html
    assert "http-equiv" not in html
    for marker in ("<script src", "cdn.jsdelivr", "googleapis"):
        assert marker not in html


def test_export_writes_a_locked_down_file(tmp_path):
    source = tmp_path / "1.json"
    source.write_text(json.dumps(MANIFEST), encoding="utf-8")
    destination = export_manifest(source, tmp_path / "out" / "1.html")
    assert destination.exists()
    assert destination.stat().st_mode & 0o077 == 0


def test_an_empty_manifest_still_renders():
    html = render_manifest({"thread": {}, "messages": [], "warnings": []})
    assert "<html" in html and "0 message(s)" in html


def test_times_are_rendered_in_the_local_zone_not_utc(monkeypatch):
    """A page showing 07:34 for a message Discord displayed at 09:34 reads as broken."""
    import time

    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    try:
        html = render_manifest(MANIFEST)
        assert "2024-05-01 14:00" in html, "12:00 UTC is 14:00 in Paris in May"
        assert "2024-05-01 12:00" not in html
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_the_page_says_which_zone_it_uses(monkeypatch):
    import time

    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    try:
        assert "times in" in render_manifest(MANIFEST)
    finally:
        monkeypatch.delenv("TZ", raising=False)
        time.tzset()


def test_a_naive_timestamp_is_left_alone():
    import json as _json

    manifest = _json.loads(_json.dumps(MANIFEST))
    manifest["messages"][0]["created_at"] = "2024-05-01T12:00:00"
    assert "2024-05-01 12:00" in render_manifest(manifest)
