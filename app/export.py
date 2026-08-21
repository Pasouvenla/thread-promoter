"""Render a migration manifest as a standalone HTML archive.

The manifest already holds everything the replay had to degrade: the real
authors, the real timestamps, the per-user reactions. A webhook replay cannot
show any of that faithfully, an HTML page can, and it keeps working the day the
guild is gone.

No template engine and no CSS framework: one file, opened by double-clicking it
in ten years.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

# Only the subset of Discord markdown that changes meaning when dropped.
CODE_BLOCK = re.compile(r"```(\w*)\n?(.*?)```", re.S)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")
STRIKE = re.compile(r"~~(.+?)~~", re.S)
LINK = re.compile(r"https?://[^\s<>\"]+")
CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")

STYLE = """
:root { color-scheme: light; }
body { margin: 0; padding: 2rem 1rem; background: #f7f7f8; color: #1f2124;
       font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 52rem; margin: 0 auto; }
header.doc { border-bottom: 1px solid #d8d8dc; padding-bottom: 1.25rem; margin-bottom: 2rem; }
header.doc h1 { margin: 0 0 .35rem; font-size: 1.6rem; }
header.doc p { margin: .2rem 0; color: #5c6068; font-size: .92rem; }
header.doc a { color: #3a6ea5; }
.msg { display: flex; gap: .85rem; padding: .55rem 0; }
.msg.reply-target { scroll-margin-top: 1rem; }
.avatar { width: 40px; height: 40px; border-radius: 50%; flex: 0 0 40px;
          background: #d8d8dc; object-fit: cover; }
.body { min-width: 0; flex: 1; }
.meta { display: flex; align-items: baseline; gap: .5rem; flex-wrap: wrap; }
.author { font-weight: 600; }
.time { color: #82868e; font-size: .78rem; }
.edited { color: #82868e; font-size: .72rem; }
.pin { color: #b1701f; font-size: .72rem; }
.content { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: .1rem; }
.content code { background: #e9e9ec; padding: .1em .3em; border-radius: 3px;
                font-size: .88em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.content pre { background: #1f2124; color: #f2f2f3; padding: .8rem 1rem; border-radius: 6px;
               overflow-x: auto; font-size: .85em; }
.content pre code { background: none; padding: 0; color: inherit; }
.quote { border-left: 3px solid #c4c6cc; padding-left: .7rem; color: #5c6068;
         font-size: .88rem; margin: .15rem 0; }
.reactions { margin-top: .3rem; display: flex; gap: .35rem; flex-wrap: wrap; }
.reaction { background: #e9e9ec; border-radius: 10px; padding: .05rem .5rem; font-size: .8rem; }
.attachments { margin-top: .3rem; font-size: .85rem; }
.attachments li { color: #5c6068; }
.lost { color: #a3341f; }
.warnings { margin-top: 2.5rem; border-top: 1px solid #d8d8dc; padding-top: 1rem;
            font-size: .85rem; color: #5c6068; }
.warnings li { margin: .2rem 0; }
"""


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _render_markdown(raw: str) -> str:
    """Escape first, then re-introduce the few tags we actually want."""
    blocks: list[str] = []

    def stash_block(match: re.Match) -> str:
        language, code = match.group(1), match.group(2)
        klass = f' class="language-{_escape(language)}"' if language else ""
        blocks.append(f"<pre><code{klass}>{_escape(code.rstrip())}</code></pre>")
        return f"\x00{len(blocks) - 1}\x00"

    text = CODE_BLOCK.sub(stash_block, raw)
    text = _escape(text)
    text = INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = BOLD.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    text = ITALIC.sub(lambda m: f"<em>{m.group(1)}</em>", text)
    text = CUSTOM_EMOJI.sub(lambda m: f":{m.group(1)}:", text)
    text = LINK.sub(lambda m: f'<a href="{m.group(0)}">{m.group(0)}</a>', text)
    for index, block in enumerate(blocks):
        text = text.replace(f"\x00{index}\x00", block)
    return text


def _parse_time(value: Any) -> datetime | None:
    """Parse, then move to the reader's own timezone.

    Manifests store UTC, which is right for storage and wrong for reading: an
    archive showing 07:34 for a message Discord displayed at 09:34 makes the
    reader doubt the whole page. The container carries TZ, so astimezone lands
    on the same wall clock the conversation happened on.
    """
    if not value:
        return None
    parsed = value if isinstance(value, datetime) else None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone()


def _render_message(entry: dict, by_source: dict[int, dict]) -> str:
    author = _escape(entry.get("author_display") or entry.get("author") or "Unknown")
    avatar = entry.get("avatar_url") or ""
    avatar_tag = (
        f'<img class="avatar" src="{html.escape(avatar, quote=True)}" alt="">'
        if avatar
        else '<div class="avatar"></div>'
    )

    created = _parse_time(entry.get("created_at"))
    stamp = created.strftime("%Y-%m-%d %H:%M") if created else ""
    edited = ' <span class="edited">(edited)</span>' if entry.get("edited_at") else ""
    pinned = ' <span class="pin">pinned</span>' if entry.get("pinned") else ""

    parts = [f'<div class="msg" id="m{entry["source_id"]}">', avatar_tag, '<div class="body">']

    reply_to = entry.get("reply_to")
    if reply_to:
        target = by_source.get(reply_to)
        if target:
            label = _escape(target.get("author_display") or target.get("author") or "a message")
            excerpt = _escape((target.get("content") or "").replace("\n", " ")[:110])
            parts.append(
                f'<div class="quote">Replying to <a href="#m{reply_to}">{label}</a>'
                + (f": {excerpt}" if excerpt else "")
                + "</div>"
            )
        else:
            parts.append('<div class="quote">Replying to a message outside this thread</div>')

    parts.append(
        f'<div class="meta"><span class="author">{author}</span>'
        f'<span class="time">{stamp}</span>{edited}{pinned}</div>'
    )

    content = entry.get("content") or ""
    if content:
        parts.append(f'<div class="content">{_render_markdown(content)}</div>')

    attachments = entry.get("attachments") or []
    lost = entry.get("attachments_lost") or []
    if attachments or lost:
        items = "".join(f"<li>{_escape(str(name))}</li>" for name in attachments)
        items += "".join(f'<li class="lost">{_escape(str(name))}</li>' for name in lost)
        parts.append(f'<ul class="attachments">{items}</ul>')

    reactions = entry.get("reactions") or []
    if reactions:
        chips = "".join(
            f'<span class="reaction" title="{_escape(", ".join(r.get("users") or []))}">'
            f'{_escape(str(r.get("emoji", "")))} {r.get("count", 0)}</span>'
            for r in reactions
        )
        parts.append(f'<div class="reactions">{chips}</div>')

    parts.append("</div></div>")
    return "".join(parts)


def render_manifest(manifest: dict) -> str:
    thread = manifest.get("thread") or {}
    messages = manifest.get("messages") or []
    by_source = {m["source_id"]: m for m in messages if "source_id" in m}

    title = _escape(str(thread.get("name") or "Promoted thread"))
    created = _parse_time(thread.get("created_at"))
    source_url = thread.get("url")

    head = [f"<h1>{title}</h1>"]
    if thread.get("parent"):
        head.append(f"<p>Thread from #{_escape(str(thread['parent']))}</p>")
    if created:
        head.append(f"<p>Created {created.strftime('%Y-%m-%d')}</p>")
    zone = datetime.now().astimezone().tzname() or "local time"
    head.append(f"<p>{len(messages)} message(s) archived, times in {_escape(zone)}</p>")
    if source_url:
        href = html.escape(str(source_url), quote=True)
        head.append(f'<p><a href="{href}">Open the original thread</a></p>')

    body = "\n".join(_render_message(entry, by_source) for entry in messages)

    warnings = manifest.get("warnings") or []
    warn_block = ""
    if warnings:
        items = "".join(f"<li>{_escape(str(w))}</li>" for w in warnings)
        warn_block = (
            f'<section class="warnings"><strong>{len(warnings)} warning(s) during the '
            f"migration</strong><ul>{items}</ul></section>"
        )

    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n<style>{STYLE}</style>\n</head>\n<body>\n<main>\n"
        f'<header class="doc">{"".join(head)}</header>\n'
        f"{body}\n{warn_block}\n</main>\n</body>\n</html>\n"
    )


def export_manifest(manifest_path: Path, destination: Path) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_manifest(manifest), encoding="utf-8")
    destination.chmod(0o600)
    return destination
