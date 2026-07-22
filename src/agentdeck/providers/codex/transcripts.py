"""Codex rollout JSONL parsing: sessions/YYYY/MM/DD/rollout-*.jsonl.

The outer envelope is ``{timestamp, type, payload}``. Conversation items are
``response_item`` payloads; token counts are ``event_msg`` payloads. Complete
lines are parsed incrementally and malformed or partial trailing lines never
break a session.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from ...images import SUPPORTED_IMAGE_MEDIA_TYPES
from ...models import TokenTotals, TranscriptEvent
from ..transcript_reader import (
    LineParser,
    TranscriptMeta,
    TranscriptRead,
    TranscriptReader,
    parse_objects,
    parse_ts,
    read_line,
)
from ..transcript_reader import (
    # re-exported so callers keep using transcripts.<name>
    token_totals as token_totals,
)
from ..transcript_reader import (
    transcript_cursor as transcript_cursor,
)

log = logging.getLogger(__name__)

# Local aliases keep the many existing private-name call sites unchanged while
# the shared machinery lives in ``transcript_reader``.
_parse_ts = parse_ts
_objects = parse_objects

_MAX_TOOL_OUTPUT = 4000
_MAX_TOOL_DETAIL = 8000
_MAX_TITLE = 200
_MAX_TOOL_SUMMARY = 160
_META_HEAD = 128 * 1024
_META_TAIL = 256 * 1024
_MAX_IMAGE_URL_CHARS = 14 * 1024 * 1024
_MAX_IMAGE_TOTAL_CHARS = 28 * 1024 * 1024
_MAX_IMAGES = 4
_SAFE_IMAGE_PREFIXES = {
    f"data:{media_type};base64,": media_type
    for media_type in SUPPORTED_IMAGE_MEDIA_TYPES
}
_IMAGE_WRAPPER = re.compile(r'^<image name=\[Image #\d+\] path="[^"\r\n]+">$')
_DELEGATION_STATUS_MARKER = b"AgentDeck delegation: running (/sessions/"
_DELEGATION_SESSION_RE = re.compile(
    r"(?:^|\r?\n|\\n)"
    r"AgentDeck delegation: running \(/sessions/([A-Za-z0-9_.:-]+)\)"
    r"(?=$|\r?\n|\\n)",
    re.MULTILINE,
)


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
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        # Codex surrounds each persisted input_image with private path metadata.
        # The image itself is rendered below; the transport wrapper is not chat.
        if value == "</image>" or _IMAGE_WRAPPER.fullmatch(value):
            continue
        parts.append(value)
    text = "\n".join(parts).strip()
    return text or None


def _image_sources(content: object) -> tuple[tuple[str, str], ...]:
    """Bounded embedded raster image payloads from one Codex message."""
    if not isinstance(content, list):
        return ()
    images = []
    total = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "input_image":
            continue
        value = block.get("image_url")
        if not isinstance(value, str):
            continue
        prefix = next((item for item in _SAFE_IMAGE_PREFIXES if value.startswith(item)), None)
        if prefix is None:
            continue
        if len(images) >= _MAX_IMAGES or len(value) > _MAX_IMAGE_URL_CHARS:
            continue
        total += len(value)
        if total > _MAX_IMAGE_TOTAL_CHARS:
            break
        images.append((_SAFE_IMAGE_PREFIXES[prefix], value[len(prefix) :]))
    return tuple(images)


def transcript_image(path: Path, seq: int, image_index: int) -> tuple[str, bytes] | None:
    """Decode one bounded image from an exact rollout line."""
    if seq < 1 or image_index < 0:
        return None
    data = read_line(path, seq)
    payload = data.get("payload") if isinstance(data, dict) else None
    content = payload.get("content") if isinstance(payload, dict) else None
    images = _image_sources(content)
    if image_index >= len(images):
        return None
    media_type, encoded = images[image_index]
    try:
        return media_type, base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _strings(value: object, *, json_depth: int = 2):
    if isinstance(value, str):
        yield value
        if json_depth:
            candidates = [value]
            if "\n" in value:
                candidates.extend(value.splitlines())
            for candidate in candidates:
                candidate = candidate.strip()
                if not candidate.startswith(("{", "[")):
                    continue
                try:
                    decoded = json.loads(candidate)
                except (TypeError, ValueError):
                    continue
                yield from _strings(decoded, json_depth=json_depth - 1)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, json_depth=json_depth)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item, json_depth=json_depth)


def delegated_session_keys(path: Path) -> frozenset[str]:
    """Find machine delegations proven by AgentDeck CLI status output.

    Legacy delegations predate the database marker used by Deckhand. Their
    parent transcripts still contain the exact status line emitted only after
    the machine API has returned a child session URL. Tool inputs and ordinary
    conversation are deliberately ignored to avoid prompt-text heuristics.
    """
    found: set[str] = set()
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                if _DELEGATION_STATUS_MARKER not in raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except (TypeError, ValueError):
                    continue
                if obj.get("type") != "response_item":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict) or payload.get("type") not in {
                    "custom_tool_call_output",
                    "function_call_output",
                }:
                    continue
                for text in _strings(payload.get("output")):
                    found.update(_DELEGATION_SESSION_RE.findall(text))
    except OSError:
        pass
    return frozenset(found)


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
            "<subagent_notification>",
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
    if re.search(r"\btools\.multi_agent_v1__spawn_agent\s*\(", text):
        return "Start agents"
    if re.search(r"\btools\.multi_agent_v1__wait_agent\s*\(", text):
        return "Wait for agents"
    if re.search(r"\btools\.multi_agent_v1__(?:send_input|send_message)\s*\(", text):
        return "Message agent"
    return None


def _subagent_notification(text: str | None) -> tuple[str | None, str, str | None] | None:
    """Decode Codex's internal user-message wrapper into a compact lifecycle event."""
    if not text or not text.lstrip().startswith("<subagent_notification>"):
        return None
    inner = text.strip().removeprefix("<subagent_notification>").removesuffix(
        "</subagent_notification>"
    ).strip()
    try:
        value = json.loads(inner)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    agent_id = value.get("agent_path")
    status_value = value.get("status")
    if isinstance(status_value, dict) and status_value:
        raw_status, detail = next(iter(status_value.items()))
        status = {
            "completed": "finished",
            "errored": "failed",
            "pending_init": "starting",
        }.get(str(raw_status), str(raw_status).replace("_", " "))
        return (
            agent_id if isinstance(agent_id, str) else None,
            status,
            detail.strip() if isinstance(detail, str) and detail.strip() else None,
        )
    status = str(status_value).replace("_", " ") if status_value is not None else "updated"
    return agent_id if isinstance(agent_id, str) else None, status, None


def _one_line(value: str | None, limit: int = _MAX_TOOL_SUMMARY) -> str | None:
    if not value:
        return None
    line = next((part.strip() for part in value.splitlines() if part.strip()), "")
    return line[:limit] or None


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


def _subagent_identities(value: object) -> tuple[tuple[str, str], ...]:
    """Find agent id/nickname pairs inside nested orchestrator output blocks."""
    found: list[tuple[str, str]] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            agent_id = item.get("agent_id")
            nickname = item.get("nickname")
            if isinstance(agent_id, str) and isinstance(nickname, str):
                pair = (agent_id, nickname)
                if pair not in found:
                    found.append(pair)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, str) and "agent_id" in item and "nickname" in item:
            for line in item.splitlines():
                try:
                    parsed = json.loads(line.strip())
                except (TypeError, ValueError):
                    continue
                visit(parsed)

    visit(value)
    return tuple(found)


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
        # Usage is a standalone Codex event. Keep it in the neutral stream so its
        # normalized tokens are summed; the template hides contentless events.
        return TranscriptEvent(
            seq=seq, role="system", tokens=_usage_totals(usage), ts=timestamp
        )

    if outer_type != "response_item":
        return None
    item_type = payload.get("type")
    if item_type == "message":
        native_role = payload.get("role")
        content = payload.get("content")
        text = _text_content(content)
        image_media_types = tuple(
            media_type for media_type, _data in _image_sources(content)
        )
        notification = _subagent_notification(text) if native_role == "user" else None
        if notification is not None:
            agent_id, status, detail = notification
            return TranscriptEvent(
                seq=seq,
                role="system",
                text=detail,
                tool_name="subagent",
                tool_display_name="Subagent",
                tool_summary=_one_line(detail),
                ts=timestamp,
                subagent_status=status,
                subagent_id=agent_id,
            )
        role = "system" if native_role in ("developer", "system") else native_role
        if role not in ("user", "assistant", "system"):
            return None
        if text is not None and role == "assistant":
            text = _strip_internal_assistant_metadata(text)
        if (
            (text is None and not image_media_types)
            or (role == "user" and _is_noise_user(text))
            or (role == "system" and _is_internal_system(text))
        ):
            return None
        return TranscriptEvent(
            seq=seq,
            role=role,
            text=text,
            ts=timestamp,
            image_media_types=image_media_types,
        )
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
            subagent_identities=_subagent_identities(payload.get("output")),
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


class _CodexLineParser(LineParser):
    def event_from_line(self, seq: int, obj: dict) -> TranscriptEvent | None:
        return _event_from_line(seq, obj)

    def is_probe_event(self, event: TranscriptEvent) -> bool:
        # Usage-only heartbeat events carry no open-turn signal; the latest
        # conversational/tool line is the one that decides busy-vs-waiting.
        return bool(event.text or event.tool_name or event.question)


_READER = TranscriptReader(_CodexLineParser())


def read_events(path: Path, *, byte_offset: int = 0, seq: int = 0) -> TranscriptRead:
    """Read complete lines after a byte cursor; leave a partial tail pending."""
    return _READER.read_events(path, byte_offset=byte_offset, seq=seq)


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


def _latest_task_state(
    path: Path, *, chunk_size: int = 64 * 1024
) -> tuple[str | None, datetime | None, datetime | None]:
    """Return the latest task status and its start/end by scanning lines backwards.

    Task boundaries may sit outside the bounded metadata head/tail on large,
    multi-turn rollouts. Reverse scanning stops at the latest turn's start and
    is cached by the provider's mtime-keyed metadata cache.
    """
    latest_status = None
    latest_finished_at = None
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            position = handle.tell()
            remainder = b""
            while position > 0:
                size = min(chunk_size, position)
                position -= size
                handle.seek(position)
                data = handle.read(size) + remainder
                lines = data.split(b"\n")
                remainder = lines[0]
                for raw in reversed(lines[1:]):
                    if b'"task_' not in raw and b'"turn_aborted"' not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    payload = obj.get("payload")
                    if obj.get("type") != "event_msg" or not isinstance(payload, dict):
                        continue
                    boundary = payload.get("type")
                    if boundary not in ("task_started", "task_complete", "turn_aborted"):
                        continue
                    timestamp = _parse_ts(obj.get("timestamp"))
                    if latest_status is None:
                        latest_status = {
                            "task_started": "working",
                            "task_complete": "finished",
                            "turn_aborted": "stopped",
                        }[boundary]
                        if boundary != "task_started":
                            latest_finished_at = timestamp
                    if boundary == "task_started":
                        return latest_status, timestamp, latest_finished_at
            if remainder:
                try:
                    obj = json.loads(remainder)
                except (TypeError, ValueError):
                    obj = None
                if isinstance(obj, dict) and obj.get("type") == "event_msg":
                    payload = obj.get("payload")
                    if isinstance(payload, dict) and payload.get("type") == "task_started":
                        latest_status = latest_status or "working"
                        return latest_status, _parse_ts(obj.get("timestamp")), latest_finished_at
    except OSError:
        pass
    return latest_status, None, latest_finished_at


def transcript_meta(
    path: Path, *, head: int = _META_HEAD, tail: int = _META_TAIL
) -> TranscriptMeta:
    """Bounded head/tail metadata for cheap rescans of large rollouts."""
    first, last = _bounded_parts(path, head, tail)
    head_objects = _objects(first, skip_first_partial=False, require_final_newline=True)
    tail_objects = _objects(last, skip_first_partial=bool(last), require_final_newline=True)

    session_id = cwd = kind = model = None
    is_approval_review = is_subagent = is_spawned_subagent = task_active = False
    agent_id = agent_nickname = agent_role = None
    task_finished_at = None
    task_started_at = None
    task_status = None
    started_at = None
    title = first_prompt = last_prompt = last_text = last_role = None
    last_agent_message = None
    tokens = None
    context_tokens = None

    def scan(objects: list[dict], *, find_title: bool) -> None:
        nonlocal session_id, cwd, kind, model, started_at, is_approval_review
        nonlocal is_subagent, is_spawned_subagent, task_active
        nonlocal agent_id, agent_nickname, agent_role, task_finished_at, task_started_at
        nonlocal task_status
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
                    task_started_at = _parse_ts(obj.get("timestamp")) or task_started_at
                    task_status = "working"
                elif boundary in ("task_complete", "turn_aborted"):
                    task_active = False
                    task_status = "finished" if boundary == "task_complete" else "stopped"
                    task_finished_at = _parse_ts(obj.get("timestamp")) or task_finished_at
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
                thread_spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                is_spawned_subagent = is_spawned_subagent or (
                    isinstance(thread_spawn, dict)
                )
                child_id = payload.get("id")
                agent_id = child_id if isinstance(child_id, str) else agent_id
                nickname = payload.get("agent_nickname")
                if not isinstance(nickname, str) and isinstance(thread_spawn, dict):
                    nickname = thread_spawn.get("agent_nickname")
                agent_nickname = nickname if isinstance(nickname, str) else agent_nickname
                role_name = payload.get("agent_role")
                if not isinstance(role_name, str) and isinstance(thread_spawn, dict):
                    role_name = thread_spawn.get("agent_role")
                agent_role = role_name if isinstance(role_name, str) else agent_role
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
    exact_task_status, exact_task_started_at, exact_task_finished_at = _latest_task_state(path)
    if exact_task_status is not None:
        task_status = exact_task_status
        task_active = exact_task_status == "working"
        task_started_at = exact_task_started_at
        task_finished_at = exact_task_finished_at
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
        task_started_at=task_started_at,
        agent_id=agent_id,
        agent_nickname=agent_nickname,
        agent_role=agent_role,
        task_finished_at=task_finished_at,
        task_status=task_status,
    )


def last_event(path: Path, *, tail: int = _META_TAIL) -> TranscriptEvent | None:
    """Most recent conversational/tool event from a bounded complete-line tail."""
    return _READER.last_event(path, tail=tail)


def recent_conversation(
    path: Path, *, limit: int = 4, tail: int = 1024 * 1024
) -> list[TranscriptEvent]:
    """Recent conversational messages from a bounded complete-line rollout tail."""
    return _READER.recent_conversation(path, limit=limit, tail=tail)


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
