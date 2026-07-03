"""Incremental transcript parsing: projects/<slug>/<uuid>.jsonl → events.

Each line is one JSON object. We tolerate partial trailing lines (the CLI
appends line-buffered, but a read can still catch a half-written line) and
skip malformed lines with a counter rather than ever raising.

seq = 1-based line index, so a live tail asks for events with seq > last_seq
and a byte offset lets us resume a read without rescanning the whole file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...models import TokenTotals, TranscriptEvent

log = logging.getLogger(__name__)

_MAX_TOOL_SUMMARY = 160
_MAX_TEXT = 4000


@dataclass
class TranscriptRead:
    events: list[TranscriptEvent]
    byte_offset: int  # resume point for the next incremental read
    seq: int  # highest seq emitted so far
    skipped: int  # malformed lines skipped this read


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _text_from_content(content: object) -> tuple[str | None, str | None, str | None]:
    """Return (text, tool_name, tool_summary) from a message.content value."""
    if isinstance(content, str):
        return (content[:_MAX_TEXT] or None, None, None)
    if not isinstance(content, list):
        return (None, None, None)
    texts: list[str] = []
    tool_name: str | None = None
    tool_summary: str | None = None
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif btype == "tool_use":
            tool_name = block.get("name")
            tool_summary = _summarize_tool_input(block.get("input"))
        elif btype == "tool_result":
            texts.append(_stringify_tool_result(block.get("content")))
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            texts.append(block["thinking"])
    text = "\n".join(t for t in texts if t).strip()
    return (text[:_MAX_TEXT] or None, tool_name, tool_summary)


def _summarize_tool_input(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("command", "file_path", "path", "query", "pattern", "prompt", "description", "url"):
        v = value.get(key)
        if isinstance(v, str) and v:
            return f"{key}: {v}"[:_MAX_TOOL_SUMMARY]
    keys = ", ".join(str(k) for k in value)
    return (keys or None) and keys[:_MAX_TOOL_SUMMARY]


def _stringify_tool_result(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


def _event_from_line(seq: int, data: dict, subagent: str | None = None) -> TranscriptEvent | None:
    ltype = data.get("type")
    if ltype not in ("user", "assistant", "system"):
        return None  # skip mode/queue-operation/summary lines
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text, tool_name, tool_summary = _text_from_content(content)
    model = message.get("model") if isinstance(message, dict) else None
    usage = message.get("usage") if isinstance(message, dict) else None
    is_tool_result = tool_name is None and ltype == "user" and _looks_like_tool_result(content)
    role = "tool" if is_tool_result else ltype
    if text is None and tool_name is None:
        return None  # nothing renderable (e.g. an isMeta bookkeeping line)
    return TranscriptEvent(
        seq=seq,
        role=role,
        text=text,
        tool_name=tool_name,
        tool_summary=tool_summary,
        model=model if isinstance(model, str) else None,
        usage=usage if isinstance(usage, dict) else None,
        ts=_parse_ts(data.get("timestamp")),
        subagent=subagent,
    )


def _looks_like_tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def read_events(
    path: Path, *, byte_offset: int = 0, seq: int = 0, subagent: str | None = None
) -> TranscriptRead:
    """Read new complete lines from ``byte_offset`` onward.

    A trailing line without a newline is left unconsumed (offset stops before
    it) so the next read picks it up once fully written.
    """
    events: list[TranscriptEvent] = []
    skipped = 0
    try:
        with path.open("rb") as f:
            f.seek(byte_offset)
            data = f.read()
    except OSError:
        return TranscriptRead(events, byte_offset, seq, 0)

    consumed = 0
    # Only iterate complete lines (those terminated by \n).
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return TranscriptRead(events, byte_offset, seq, 0)
    complete = data[: last_nl + 1]
    consumed = len(complete)
    for raw in complete.splitlines():
        line = raw.strip()
        if not line:
            seq += 1
            continue
        seq += 1
        try:
            obj = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        ev = _event_from_line(seq, obj, subagent=subagent)
        if ev is not None:
            events.append(ev)
    if skipped:
        log.debug("%s: skipped %d malformed transcript lines", path.name, skipped)
    return TranscriptRead(events, byte_offset + consumed, seq, skipped)


def _user_text(obj: dict) -> str | None:
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()[:200] or None
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        joined = " ".join(parts).strip()
        return joined[:200] or None
    return None


def _assistant_text(obj: dict) -> str | None:
    """The visible text of an assistant line (text blocks only; tool_use and
    thinking blocks are ignored) — used as the agent's 'latest response'."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()[:200] or None
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        joined = " ".join(parts).strip()
        return joined[:200] or None
    return None


def transcript_meta(
    path: Path, *, head: int = 65536, tail: int = 32768
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Cheap extraction without parsing the whole file → (ai_title, last_prompt,
    first_user_text, cwd, last_agent_text). Claude Code writes an ``ai-title``
    line (a concise session summary), ``last-prompt`` lines, and a ``cwd`` field
    on every entry; we read the head (ai-title/first prompt/cwd live there) and
    the tail (latest last-prompt + latest assistant reply), latest occurrence
    winning. ``cwd`` is shown for idle sessions too — the live-process registry
    is gone once a session is idle."""
    ai_title: str | None = None
    last_prompt: str | None = None
    first_user: str | None = None
    cwd: str | None = None
    last_text: str | None = None
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head_bytes = f.read(head)
            tail_bytes = b""
            if size > head:
                f.seek(max(head, size - tail))
                tail_bytes = f.read()
    except OSError:
        return (None, None, None, None, None)

    def scan(blob: bytes, skip_first_partial: bool) -> None:
        nonlocal ai_title, last_prompt, first_user, cwd, last_text
        lines = blob.split(b"\n")
        if skip_first_partial and lines:
            lines = lines[1:]  # a mid-file seek can land inside a line
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            if cwd is None and isinstance(obj.get("cwd"), str) and obj["cwd"]:
                cwd = obj["cwd"]
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                ai_title = obj["aiTitle"].strip() or ai_title
            elif t == "last-prompt" and isinstance(obj.get("lastPrompt"), str):
                last_prompt = obj["lastPrompt"].strip() or last_prompt
            elif t == "assistant":
                last_text = _assistant_text(obj) or last_text
            elif t == "user" and first_user is None:
                if not obj.get("isMeta") and not obj.get("isSidechain"):
                    first_user = _user_text(obj)

    scan(head_bytes, skip_first_partial=False)
    if tail_bytes:
        scan(tail_bytes, skip_first_partial=True)
    return (ai_title, last_prompt, first_user, cwd, last_text)


def token_totals(events: list[TranscriptEvent]) -> TokenTotals:
    inp = out = cread = ccreate = 0
    for ev in events:
        u = ev.usage
        if not u:
            continue
        inp += int(u.get("input_tokens", 0) or 0)
        out += int(u.get("output_tokens", 0) or 0)
        cread += int(u.get("cache_read_input_tokens", 0) or 0)
        ccreate += int(u.get("cache_creation_input_tokens", 0) or 0)
    return TokenTotals(
        input_tokens=inp, output_tokens=out, cache_read_tokens=cread, cache_creation_tokens=ccreate
    )


def last_model(events: list[TranscriptEvent]) -> str | None:
    for ev in reversed(events):
        if ev.model:
            return ev.model
    return None


def load_todos(config_dir: Path, session_id: str) -> list[dict]:
    """Read tasks/<sessionId>/*.json todo items; best-effort, never fatal."""
    tdir = config_dir / "tasks" / session_id
    if not tdir.is_dir():
        return []
    todos: list[dict] = []
    for path in sorted(tdir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and (item.get("subject") or item.get("description")):
                todos.append(
                    {
                        "subject": item.get("subject") or item.get("description"),
                        "status": item.get("status", "pending"),
                    }
                )
    return todos
