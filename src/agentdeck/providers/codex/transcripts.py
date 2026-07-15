"""Codex rollout JSONL parsing: sessions/YYYY/MM/DD/rollout-*.jsonl.

The outer envelope is ``{timestamp, type, payload}``. Conversation items are
``response_item`` payloads; token counts are ``event_msg`` payloads. Complete
lines are parsed incrementally and malformed or partial trailing lines never
break a session.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...models import TokenTotals, TranscriptEvent

log = logging.getLogger(__name__)

_MAX_TOOL_OUTPUT = 4000
_MAX_TOOL_DETAIL = 8000
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
    first_prompt: str | None = None
    last_prompt: str | None = None
    last_text: str | None = None
    # The agent's canonical final message from the last `task_complete` event.
    # Unlike last_text (the last assistant *item*), this is what Codex reports as
    # the turn result, so it is immune to intermediate/approval-path noise.
    last_agent_message: str | None = None
    last_role: str | None = None
    model: str | None = None
    kind: str | None = None
    tokens: TokenTotals | None = None
    context_tokens: int | None = None
    is_approval_review: bool = False
    # Codex helper rollouts (spawned agents and guardian/auto-review runs) reuse
    # the parent session_id. They are separate files and must never represent
    # the parent conversation in AgentDeck.
    is_subagent: bool = False
    is_spawned_subagent: bool = False
    task_active: bool = False


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
    return text or None


_INTERNAL_ASSISTANT_BLOCK = re.compile(r"\s*<oai-mem-citation>.*?</oai-mem-citation>\s*", re.DOTALL)


def _strip_internal_assistant_metadata(text: str) -> str | None:
    """Remove renderer metadata that is stored beside the assistant's answer.

    Codex consumers use the oai-mem-citation block for provenance, but it is
    not conversation content and AgentDeck has no reason to expose it.
    """
    cleaned = _INTERNAL_ASSISTANT_BLOCK.sub("\n", text).strip()
    # Be conservative with a truncated tail: never show a half-written internal
    # block while the rollout JSONL is still being appended.
    if "<oai-mem-citation>" in cleaned:
        cleaned = cleaned.partition("<oai-mem-citation>")[0].rstrip()
    return cleaned or None


def _is_noise_user(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    folded = stripped.casefold()
    if folded.startswith(
        (
            "<environment_context>",
            "<recommended_plugins>",
            "# agents.md instructions for ",
            "<instructions>",
        )
    ):
        return True
    first_line = stripped.partition("\n")[0].strip()
    return first_line == "@.cursorrules" or (
        first_line.startswith("@.cursor/") and first_line.endswith(".mdc")
    )


_INTERNAL_SYSTEM_PREFIXES = (
    "<permissions instructions>",
    "<multi_agent_mode>",
)

_PRIMARY_AGENT_PREAMBLE = re.compile(
    r"You are\s+`?/root`?,\s+the primary agent in a team of agents\b"
)


def _is_internal_system(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    return stripped.startswith(_INTERNAL_SYSTEM_PREFIXES) or bool(
        _PRIMARY_AGENT_PREAMBLE.match(stripped)
    )


def _decode_js_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except ValueError:
        return value


def _wrapped_exec_command(text: str) -> str | None:
    if not re.search(r"\b(?:tools\.)?exec_command\s*\(", text):
        return None
    return _tool_string_field(text, "cmd")


def _tool_string_field(value: object, key: str) -> str | None:
    """Read a string argument from either JSON or a wrapped JavaScript call."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        item = parsed.get(key)
        return item if isinstance(item, str) and item else None
    key_pattern = rf'(?<!\w)(?:"{re.escape(key)}"|{re.escape(key)})'
    match = re.search(rf'{key_pattern}\s*:\s*"((?:\\.|[^"\\])*)"', text)
    return _decode_js_string(match.group(1)) if match else None


def _tool_number_field(value: object, key: str) -> int | None:
    """Read a non-negative integer from JSON or a wrapped JavaScript call."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        item = parsed.get(key)
        return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else None
    key_pattern = rf'(?<!\w)(?:"{re.escape(key)}"|{re.escape(key)})'
    match = re.search(rf"{key_pattern}\s*:\s*(\d+)", text)
    return int(match.group(1)) if match else None


def _wrapped_exec_detail(value: str, command: str) -> str:
    """Turn the Codex JavaScript shell wrapper into readable labelled fields."""
    sections = [f"Command\n{command}"]
    workdir = _tool_string_field(value, "workdir")
    if workdir:
        sections.append(f"Working directory\n{workdir}")
    wait_ms = _tool_number_field(value, "yield_time_ms")
    if wait_ms is not None:
        sections.append(f"Wait before update\n{wait_ms / 1000:g} seconds")
    output_tokens = _tool_number_field(value, "max_output_tokens")
    if output_tokens is not None:
        sections.append(f"Output limit\n{output_tokens:,} tokens")
    return "\n\n".join(sections)[:_MAX_TOOL_DETAIL]


def _tool_display_name(native_name: object, value: object) -> str | None:
    """Classify common operations hidden inside the Codex JS orchestrator."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    folded_name = native_name.casefold() if isinstance(native_name, str) else ""
    if _tool_string_field(value, "justification") and (
        _wrapped_exec_command(text) or folded_name in ("exec", "exec_command")
    ):
        return "Approval"
    if re.search(r"\btools\.apply_patch\s*\(", text):
        return "Edit files"
    return None


def _tool_summary(value: object, display_name: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    justification = _tool_string_field(value, "justification")
    if display_name == "Approval" and justification:
        return f"reason: {justification}"[:_MAX_TOOL_SUMMARY]
    # Newer Codex builds wrap tool calls in a small JavaScript orchestration
    # snippet. Pull the useful nested shell command back out for the UI.
    command = _wrapped_exec_command(text)
    if command:
        return f"cmd: {command}"[:_MAX_TOOL_SUMMARY]
    patch_path = re.search(
        r"\*\*\* (?:Update|Add|Delete) File: ([^\\\r\n\"]+)", text
    )
    if patch_path:
        return f"path: {patch_path.group(1).strip()[: _MAX_TOOL_SUMMARY - 6]}"
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


def _tool_detail(value: object, display_name: str | None = None) -> str | None:
    """Useful expandable input without exposing the orchestration wrapper."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    command = _wrapped_exec_command(text)
    justification = _tool_string_field(value, "justification")
    if display_name == "Approval" and justification:
        command = command or _tool_string_field(value, "cmd")
        sections = [f"Reason\n{justification}"]
        if command:
            sections.append(f"Command\n{command}")
        return "\n\n".join(sections)[:_MAX_TOOL_DETAIL]
    if command:
        return _wrapped_exec_detail(text, command)
    patch = re.search(r'\bconst\s+patch\s*=\s*"((?:\\.|[^"\\])*)"', text)
    if patch:
        return _decode_js_string(patch.group(1))[:_MAX_TOOL_DETAIL]
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:_MAX_TOOL_DETAIL]
    try:
        return json.dumps(parsed, ensure_ascii=False, indent=2)[:_MAX_TOOL_DETAIL]
    except (TypeError, ValueError):
        return text[:_MAX_TOOL_DETAIL]


def _tool_output(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip()[:_MAX_TOOL_OUTPUT] or None
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False)[:_MAX_TOOL_OUTPUT]
    except (TypeError, ValueError):
        return str(value)[:_MAX_TOOL_OUTPUT] or None


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
        if text is not None and role == "assistant":
            text = _strip_internal_assistant_metadata(text)
        if (
            text is None
            or (role == "user" and _is_noise_user(text))
            or (role == "system" and _is_internal_system(text))
        ):
            return None
        return TranscriptEvent(seq=seq, role=role, text=text, ts=timestamp)
    if item_type in ("custom_tool_call", "function_call"):
        name = payload.get("name")
        arguments = payload.get("input", payload.get("arguments"))
        display_name = _tool_display_name(name, arguments)
        return TranscriptEvent(
            seq=seq,
            role="assistant",
            tool_name=name if isinstance(name, str) else "tool",
            tool_display_name=display_name,
            tool_summary=_tool_summary(arguments, display_name),
            tool_detail=_tool_detail(arguments, display_name),
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
        text = "\n".join(parts).strip()
        if text:
            return TranscriptEvent(seq=seq, role="assistant", text=text, ts=timestamp)
        # The raw event intentionally contains no chain-of-thought, but its
        # arrival proves the backend is alive. Keep it as a hidden heartbeat so
        # live activity says "Thinking" rather than looking frozen.
        return TranscriptEvent(
            seq=seq,
            role="system",
            tool_name="reasoning",
            tool_summary="Thinking",
            ts=timestamp,
        )
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
    is_approval_review = is_subagent = is_spawned_subagent = task_active = False
    started_at = None
    title = first_prompt = last_prompt = last_text = last_role = None
    last_agent_message = None
    tokens = None
    context_tokens = None

    def scan(objects: list[dict], *, find_title: bool) -> None:
        nonlocal session_id, cwd, kind, model, started_at, is_approval_review
        nonlocal is_subagent, is_spawned_subagent, task_active
        nonlocal title, first_prompt, last_prompt, last_text, last_role, tokens, context_tokens
        nonlocal last_agent_message
        for obj in objects:
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            outer_type = obj.get("type")
            if outer_type == "event_msg":
                boundary = payload.get("type")
                if boundary == "task_started":
                    task_active = True
                elif boundary in ("task_complete", "turn_aborted"):
                    task_active = False
            if outer_type == "session_meta":
                sid = payload.get("session_id", payload.get("id"))
                session_id = sid if isinstance(sid, str) else session_id
                value = payload.get("cwd")
                cwd = value if isinstance(value, str) else cwd
                value = payload.get("source")
                kind = value if isinstance(value, str) else kind
                is_subagent = is_subagent or (
                    (isinstance(value, dict) and "subagent" in value)
                    or payload.get("thread_source") == "subagent"
                )
                subagent = value.get("subagent") if isinstance(value, dict) else None
                is_spawned_subagent = is_spawned_subagent or (
                    isinstance(subagent, dict)
                    and isinstance(subagent.get("thread_spawn"), dict)
                )
                instructions = payload.get("base_instructions")
                if isinstance(instructions, dict):
                    text = instructions.get("text")
                    is_approval_review = is_approval_review or (
                        isinstance(text, str)
                        and "You are judging one planned coding-agent action." in text
                    )
                started_at = (
                    _parse_ts(payload.get("timestamp"))
                    or _parse_ts(obj.get("timestamp"))
                    or started_at
                )
            elif outer_type == "turn_context":
                value = payload.get("model")
                model = value if isinstance(value, str) else model
                value = payload.get("cwd")
                cwd = value if isinstance(value, str) else cwd
            elif outer_type == "response_item" and payload.get("type") == "message":
                native_role = payload.get("role")
                text = _text_content(payload.get("content"))
                if native_role == "user" and text and not _is_noise_user(text):
                    if first_prompt is None:
                        first_prompt = text
                    if find_title and title is None:
                        title = " ".join(text.split())[:_MAX_TITLE]
                    last_prompt = text
                    last_role = "user"
                elif native_role == "assistant" and text:
                    text = _strip_internal_assistant_metadata(text)
                    if text:
                        last_text = text
                        last_role = "agent"
            elif outer_type == "event_msg" and payload.get("type") == "task_complete":
                msg = payload.get("last_agent_message")
                if isinstance(msg, str) and msg.strip():
                    last_agent_message = _strip_internal_assistant_metadata(msg)
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
        first_prompt=first_prompt,
        last_prompt=last_prompt,
        last_text=last_text,
        last_agent_message=last_agent_message,
        last_role=last_role,
        model=model,
        kind=kind,
        tokens=tokens,
        context_tokens=context_tokens,
        is_approval_review=is_approval_review,
        is_subagent=is_subagent,
        is_spawned_subagent=is_spawned_subagent,
        task_active=task_active,
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


def last_turn_complete(path: Path, *, tail: int = _META_TAIL) -> bool:
    """Whether the most recent native turn boundary is a completed turn.

    Codex does not expose a cross-process session registry. For non-interactive
    ``exec`` rollouts, a final ``task_complete`` is the durable indication that
    the owning process finished its turn; ``task_started`` means another writer
    may still be active even when the file has been quiet for a long tool call.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail:
                handle.seek(size - tail)
            blob = handle.read(tail)
    except OSError:
        return False
    boundary = None
    for obj in _objects(blob, skip_first_partial=size > tail, require_final_newline=True):
        payload = obj.get("payload")
        if obj.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        event_type = payload.get("type")
        if event_type in ("task_started", "task_complete", "turn_aborted"):
            boundary = event_type
    return boundary == "task_complete"


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
