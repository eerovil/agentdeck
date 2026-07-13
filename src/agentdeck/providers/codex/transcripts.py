"""Codex rollout JSONL parsing: sessions/YYYY/MM/DD/rollout-*.jsonl.

The outer envelope is ``{timestamp, type, payload}``. Conversation items are
``response_item`` payloads; token counts are ``event_msg`` payloads. Complete
lines are parsed incrementally and malformed or partial trailing lines never
break a session.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...models import TokenTotals, TranscriptEvent

log = logging.getLogger(__name__)

_MAX_TEXT = 4000
_MAX_TITLE = 200
_MAX_TOOL_SUMMARY = 160
_META_HEAD = 128 * 1024
_META_TAIL = 256 * 1024


@dataclass
class TranscriptRead:
    events: list[TranscriptEvent]
    byte_offset: int
    seq: int
    skipped: int


@dataclass(frozen=True)
class TranscriptMeta:
    session_id: str | None = None
    cwd: str | None = None
    started_at: datetime | None = None
    title: str | None = None
    last_prompt: str | None = None
    last_text: str | None = None
    last_role: str | None = None
    model: str | None = None
    kind: str | None = None
    tokens: TokenTotals | None = None
    context_tokens: int | None = None


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _usage_totals(usage: object) -> TokenTotals | None:
    if not isinstance(usage, dict):
        return None
    raw_input = _safe_int(usage.get("input_tokens"))
    cached = min(raw_input, _safe_int(usage.get("cached_input_tokens")))
    output = _safe_int(usage.get("output_tokens"))
    if not (raw_input or output or cached):
        return None
    return TokenTotals(
        input_tokens=raw_input - cached,
        output_tokens=output,
        cache_read_tokens=cached,
    )


def _usage_from_event(payload: dict) -> dict | None:
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    usage = info.get("last_token_usage")
    return usage if isinstance(usage, dict) else None


def _text_content(content: object) -> str | None:
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in ("input_text", "output_text"):
            continue
        value = block.get("text")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    text = "\n".join(parts).strip()
    return text[:_MAX_TEXT] or None


def _is_noise_user(text: str | None) -> bool:
    return bool(text and text.lstrip().startswith("<environment_context>"))


def _tool_summary(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:_MAX_TOOL_SUMMARY]
    if not isinstance(parsed, dict):
        return text[:_MAX_TOOL_SUMMARY]
    for key in (
        "cmd",
        "command",
        "path",
        "query",
        "prompt",
        "message",
        "description",
        "url",
    ):
        item = parsed.get(key)
        if isinstance(item, str) and item:
            return f"{key}: {item}"[:_MAX_TOOL_SUMMARY]
    keys = ", ".join(str(key) for key in parsed)
    return keys[:_MAX_TOOL_SUMMARY] or None


def _tool_output(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip()[:_MAX_TEXT] or None
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)[:_MAX_TEXT]
    except (TypeError, ValueError):
        return str(value)[:_MAX_TEXT] or None


def _event_from_line(seq: int, data: dict) -> TranscriptEvent | None:
    timestamp = _parse_ts(data.get("timestamp"))
    outer_type = data.get("type")
    payload = data.get("payload")
    if not isinstance(payload, dict):
        return None

    if outer_type == "event_msg":
        usage = _usage_from_event(payload)
        if usage is None:
            return None
        # Usage is a standalone Codex event. Keep it in the neutral stream so
        # callers can inspect the raw block; the template hides contentless
        # bookkeeping events.
        return TranscriptEvent(seq=seq, role="system", usage=usage, ts=timestamp)

    if outer_type != "response_item":
        return None
    item_type = payload.get("type")
    if item_type == "message":
        native_role = payload.get("role")
        role = "system" if native_role in ("developer", "system") else native_role
        if role not in ("user", "assistant", "system"):
            return None
        text = _text_content(payload.get("content"))
        if text is None or (role == "user" and _is_noise_user(text)):
            return None
        return TranscriptEvent(seq=seq, role=role, text=text, ts=timestamp)
    if item_type in ("custom_tool_call", "function_call"):
        name = payload.get("name")
        arguments = payload.get("input", payload.get("arguments"))
        return TranscriptEvent(
            seq=seq,
            role="assistant",
            tool_name=name if isinstance(name, str) else "tool",
            tool_summary=_tool_summary(arguments),
            ts=timestamp,
        )
    if item_type in ("custom_tool_call_output", "function_call_output"):
        return TranscriptEvent(
            seq=seq,
            role="tool",
            text=_tool_output(payload.get("output")),
            ts=timestamp,
        )
    if item_type == "reasoning":
        summary = payload.get("summary")
        if not isinstance(summary, list):
            return None
        parts = [
            item["text"]
            for item in summary
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        text = "\n".join(parts).strip()[:_MAX_TEXT]
        if text:
            return TranscriptEvent(seq=seq, role="assistant", text=text, ts=timestamp)
    return None


def read_events(path: Path, *, byte_offset: int = 0, seq: int = 0) -> TranscriptRead:
    """Read complete lines after a byte cursor; leave a partial tail pending."""
    events: list[TranscriptEvent] = []
    skipped = 0
    try:
        size = path.stat().st_size
        if byte_offset > size:
            byte_offset = 0
            seq = 0
        with path.open("rb") as handle:
            handle.seek(byte_offset)
            data = handle.read()
    except OSError:
        return TranscriptRead(events, byte_offset, seq, skipped)
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return TranscriptRead(events, byte_offset, seq, skipped)
    complete = data[: last_newline + 1]
    for raw in complete.splitlines():
        seq += 1
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        event = _event_from_line(seq, obj)
        if event is not None:
            events.append(event)
    if skipped:
        log.debug("%s: skipped %d malformed Codex transcript lines", path.name, skipped)
    return TranscriptRead(events, byte_offset + len(complete), seq, skipped)


def transcript_cursor(path: Path, *, chunk_size: int = 256 * 1024) -> tuple[int, int]:
    """End cursor with bounded memory, counting only newline-terminated lines."""
    offset = seq = position = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                seq += chunk.count(b"\n")
                last_newline = chunk.rfind(b"\n")
                if last_newline != -1:
                    offset = position + last_newline + 1
                position += len(chunk)
    except OSError:
        return (0, 0)
    return (offset, seq)


def _bounded_parts(path: Path, head: int, tail: int) -> tuple[bytes, bytes]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            first = handle.read(head)
            last = b""
            if size > head:
                handle.seek(max(head, size - tail))
                last = handle.read(tail)
        return (first, last)
    except OSError:
        return (b"", b"")


def _objects(blob: bytes, *, skip_first_partial: bool, require_final_newline: bool) -> list[dict]:
    lines = blob.split(b"\n")
    if skip_first_partial and lines:
        lines = lines[1:]
    if require_final_newline and blob and not blob.endswith(b"\n"):
        lines = lines[:-1]
    out = []
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def transcript_meta(
    path: Path, *, head: int = _META_HEAD, tail: int = _META_TAIL
) -> TranscriptMeta:
    """Bounded head/tail metadata for cheap rescans of large rollouts."""
    first, last = _bounded_parts(path, head, tail)
    head_objects = _objects(first, skip_first_partial=False, require_final_newline=True)
    tail_objects = _objects(last, skip_first_partial=bool(last), require_final_newline=True)

    session_id = cwd = kind = model = None
    started_at = None
    title = last_prompt = last_text = last_role = None
    tokens = None
    context_tokens = None

    def scan(objects: list[dict], *, find_title: bool) -> None:
        nonlocal session_id, cwd, kind, model, started_at
        nonlocal title, last_prompt, last_text, last_role, tokens, context_tokens
        for obj in objects:
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            outer_type = obj.get("type")
            if outer_type == "session_meta":
                sid = payload.get("session_id", payload.get("id"))
                session_id = sid if isinstance(sid, str) else session_id
                value = payload.get("cwd")
                cwd = value if isinstance(value, str) else cwd
                value = payload.get("source")
                kind = value if isinstance(value, str) else kind
                started_at = _parse_ts(payload.get("timestamp")) or _parse_ts(
                    obj.get("timestamp")
                ) or started_at
            elif outer_type == "turn_context":
                value = payload.get("model")
                model = value if isinstance(value, str) else model
                value = payload.get("cwd")
                cwd = value if isinstance(value, str) else cwd
            elif outer_type == "response_item" and payload.get("type") == "message":
                native_role = payload.get("role")
                text = _text_content(payload.get("content"))
                if native_role == "user" and text and not _is_noise_user(text):
                    if find_title and title is None:
                        title = " ".join(text.split())[:_MAX_TITLE]
                    last_prompt = text
                    last_role = "user"
                elif native_role == "assistant" and text:
                    last_text = text
                    last_role = "agent"
            elif outer_type == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                totals = _usage_totals(info.get("total_token_usage"))
                if totals is not None:
                    tokens = totals
                latest = info.get("last_token_usage")
                if isinstance(latest, dict):
                    context_tokens = _safe_int(latest.get("input_tokens")) or None

    scan(head_objects, find_title=True)
    if last:
        scan(tail_objects, find_title=False)
    return TranscriptMeta(
        session_id=session_id,
        cwd=cwd,
        started_at=started_at,
        title=title,
        last_prompt=last_prompt,
        last_text=last_text,
        last_role=last_role,
        model=model,
        kind=kind,
        tokens=tokens,
        context_tokens=context_tokens,
    )


def last_event(path: Path, *, tail: int = _META_TAIL) -> TranscriptEvent | None:
    """Most recent conversational/tool event from a bounded complete-line tail."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail:
                handle.seek(size - tail)
            blob = handle.read(tail)
    except OSError:
        return None
    skip_first = size > tail
    found = None
    for obj in _objects(blob, skip_first_partial=skip_first, require_final_newline=True):
        event = _event_from_line(0, obj)
        if event is not None and (event.text or event.tool_name or event.question):
            found = event
    return found


def token_totals(events: list[TranscriptEvent]) -> TokenTotals:
    input_tokens = output_tokens = cached_tokens = 0
    for event in events:
        usage = event.usage
        totals = _usage_totals(usage)
        if totals is None:
            continue
        input_tokens += totals.input_tokens
        output_tokens += totals.output_tokens
        cached_tokens += totals.cache_read_tokens
    return TokenTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cached_tokens,
    )
