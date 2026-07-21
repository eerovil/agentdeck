"""Incremental transcript parsing: projects/<slug>/<uuid>.jsonl → events.

Each line is one JSON object. We tolerate partial trailing lines (the CLI
appends line-buffered, but a read can still catch a half-written line) and
skip malformed lines with a counter rather than ever raising.

seq = 1-based line index, so a live tail asks for events with seq > last_seq
and a byte offset lets us resume a read without rescanning the whole file.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...models import TokenTotals, TranscriptEvent

log = logging.getLogger(__name__)

_MAX_TOOL_SUMMARY = 160
# Short caps for list-card previews (last_text) and bookkeeping snippets.
_MAX_TEXT = 4000
# The rendered chat transcript shows the *full* agent/user message, not a
# preview — cap it generously so long responses aren't truncated, while still
# bounding a pathological multi-MB paste from bloating a transcript payload.
_MAX_RENDERED_TEXT = 200_000
_MAX_IMAGE_DATA_CHARS = 14 * 1024 * 1024
_MAX_IMAGE_TOTAL_CHARS = 28 * 1024 * 1024
_MAX_IMAGES = 4
_SAFE_IMAGE_MEDIA_TYPES = frozenset(
    ("image/png", "image/jpeg", "image/webp", "image/gif")
)


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


def _text_from_content(
    content: object,
) -> tuple[str | None, str | None, str | None, str | None, str | None, tuple[str, ...]]:
    """Return (text, tool_name, tool_summary, question, answer, image types) from a
    message.content value. ``question`` is the AskUserQuestion prompt when this
    line asks one; ``answer`` is your reply, parsed from the AskUserQuestion
    tool_result (which the CLI writes as "Your questions have been answered: …")
    so the choice is visible in the chat instead of being hidden with the other
    tool output."""
    if isinstance(content, str):
        return (content[:_MAX_RENDERED_TEXT] or None, None, None, None, None, ())
    if not isinstance(content, list):
        return (None, None, None, None, None, ())
    texts: list[str] = []
    tool_name: str | None = None
    tool_summary: str | None = None
    question: str | None = None
    answer: str | None = None
    image_media_types = tuple(media_type for media_type, _data in _image_sources(content))
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif btype == "tool_use":
            tool_name = block.get("name")
            if tool_name == "AskUserQuestion":
                question = _ask_question(block.get("input"))
                tool_summary = question or _summarize_tool_input(block.get("input"))
            else:
                tool_summary = _summarize_tool_input(block.get("input"))
        elif btype == "tool_result":
            result_text = _stringify_tool_result(block.get("content"))
            parsed = _askuserquestion_answer(result_text)
            if parsed is not None:
                answer = parsed  # surfaced as your reply; the raw text stays hidden
            else:
                texts.append(result_text)
        elif btype == "thinking" and isinstance(block.get("thinking"), str):
            texts.append(block["thinking"])
    text = "\n".join(t for t in texts if t).strip()
    return (
        text[:_MAX_RENDERED_TEXT] or None,
        tool_name,
        tool_summary,
        question,
        answer,
        image_media_types,
    )


def _image_sources(content: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(content, list):
        return ()
    images = []
    total = 0
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            continue
        media_type = source.get("media_type")
        data = source.get("data")
        if (
            media_type not in _SAFE_IMAGE_MEDIA_TYPES
            or not isinstance(data, str)
            or not data
            or len(data) > _MAX_IMAGE_DATA_CHARS
            or len(images) >= _MAX_IMAGES
        ):
            continue
        total += len(data)
        if total > _MAX_IMAGE_TOTAL_CHARS:
            break
        images.append((media_type, data))
    return tuple(images)


def transcript_image(path: Path, seq: int, image_index: int) -> tuple[str, bytes] | None:
    """Decode one bounded image from an exact transcript line."""
    if seq < 1 or image_index < 0:
        return None
    try:
        with path.open("rb") as handle:
            raw = next((line for number, line in enumerate(handle, 1) if number == seq), None)
        data = json.loads(raw) if raw is not None else None
    except (OSError, TypeError, ValueError):
        return None
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    images = _image_sources(content)
    if image_index >= len(images):
        return None
    media_type, encoded = images[image_index]
    try:
        return media_type, base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


# The AskUserQuestion tool_result the CLI writes once you answer (or dismiss)
# through the interactive widget. The answer payload after the prefix is a
# comma-joined list of ``"question"="answer"`` pairs, or free text for the
# responded/selected variants. "did not answer" carries no choice, so it is not
# surfaced. (Phrasings are stable strings emitted by the tool; if they change a
# future CLI just falls back to hiding the tool_result — no wrong output.)
_ASKQ_ANSWER_PREFIXES = (
    "Your questions have been answered: ",
    "Before going idle the user had selected: ",
    "The user responded: ",
)
_ASKQ_ANSWER_SUFFIX = " You can now continue with these answers in mind."
_ASKQ_PAIR = re.compile(r'"([^"]*)"="([^"]*)"')


def _askuserquestion_answer(text: object) -> str | None:
    """Your choice, extracted from an AskUserQuestion tool_result, or None."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    for prefix in _ASKQ_ANSWER_PREFIXES:
        if stripped.startswith(prefix):
            body = stripped[len(prefix) :].removesuffix(_ASKQ_ANSWER_SUFFIX).strip()
            body = body[:-1] if body.endswith(".") else body
            pairs = _ASKQ_PAIR.findall(body)
            if pairs:
                return "; ".join(f"{q}: {a}" for q, a in pairs)[:_MAX_TEXT] or None
            return body[:_MAX_TEXT] or None
    return None


def _ask_question(value: object) -> str | None:
    """The prompt(s) from an AskUserQuestion tool input — its question text lives
    in ``input.questions[].question`` (the multiple-choice tool), not in any text
    block, so it is invisible to the plain-text extractors. Multiple questions are
    joined so the whole ask surfaces."""
    if not isinstance(value, dict):
        return None
    questions = value.get("questions")
    if not isinstance(questions, list):
        return None
    asks = [
        q["question"].strip()
        for q in questions
        if isinstance(q, dict) and isinstance(q.get("question"), str) and q["question"].strip()
    ]
    return " ".join(asks)[:_MAX_TEXT] or None if asks else None


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


# Slash-command echoes are written as `user` lines wrapped in these tags (the
# `/compact` command, its stdout, the injected caveat). They are bookkeeping,
# not real user turns, so they must not read as "the agent is working" nor
# clobber the last-prompt / context readings.
_COMMAND_NOISE_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)


def _user_content_text(obj: dict) -> str | None:
    """First text of a user line's content (string or first text block), or None."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                return b["text"]
    return None


def _is_slash_command_line(obj: dict) -> bool:
    text = _user_content_text(obj)
    return isinstance(text, str) and text.lstrip().startswith(_COMMAND_NOISE_PREFIXES)


def _is_compact_boundary(obj: dict) -> bool:
    """A completed local command or a compaction summary: the *user* took the
    turn, so the agent's prior turn is closed (idle), not open/working. Used to
    stop a post-``/compact`` transcript reading as a live, in-progress turn."""
    return bool(obj.get("isCompactSummary")) or _is_slash_command_line(obj)


def _is_noise_user(obj: dict) -> bool:
    """User lines that are bookkeeping, not conversation: meta caveats, the
    compaction summary, and local slash-command echoes."""
    return bool(obj.get("isMeta") or obj.get("isCompactSummary")) or _is_slash_command_line(obj)


def _event_from_line(seq: int, data: dict, subagent: str | None = None) -> TranscriptEvent | None:
    ltype = data.get("type")
    if ltype == "queue-operation":
        # A message typed while the agent was busy. Only the "enqueue" carries
        # the text; render it as a user turn (deduped in read_events against the
        # real user line it becomes if/when the agent later processes it).
        content = data.get("content")
        if data.get("operation") == "enqueue" and isinstance(content, str) and content.strip():
            return TranscriptEvent(
                seq=seq,
                role="user",
                text=content.strip()[:_MAX_RENDERED_TEXT],
                queued=True,
                ts=_parse_ts(data.get("timestamp")),
                subagent=subagent,
            )
        return None
    if ltype not in ("user", "assistant", "system"):
        return None  # skip mode/summary lines
    if ltype == "user" and _is_noise_user(data):
        return None  # meta caveat, compaction summary, or slash-command echo
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text, tool_name, tool_summary, question, answer, image_media_types = _text_from_content(
        content
    )
    model = message.get("model") if isinstance(message, dict) else None
    usage = message.get("usage") if isinstance(message, dict) else None
    is_tool_result = tool_name is None and ltype == "user" and _looks_like_tool_result(content)
    # An AskUserQuestion answer arrives on a tool_result line but is your reply —
    # render it as a user turn, not a (hidden) tool result.
    role = "user" if answer is not None else ("tool" if is_tool_result else ltype)
    if text is None and tool_name is None and answer is None and not image_media_types:
        return None  # nothing renderable (e.g. an isMeta bookkeeping line)
    return TranscriptEvent(
        seq=seq,
        role=role,
        text=text,
        tool_name=tool_name,
        tool_summary=tool_summary,
        question=question,
        answer=answer,
        model=model if isinstance(model, str) else None,
        usage=usage if isinstance(usage, dict) else None,
        ts=_parse_ts(data.get("timestamp")),
        subagent=subagent,
        image_media_types=image_media_types,
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
    # An enqueued message the agent later processed shows up twice — once as the
    # queue-operation and once as the real user turn. Keep the real turn (shown
    # where it was processed) and drop the queued duplicate; queued messages
    # that were never processed have no real turn and stay.
    real_user_texts = {e.text for e in events if e.role == "user" and not e.queued and e.text}
    if real_user_texts:
        events = [e for e in events if not (e.queued and e.text in real_user_texts)]
    return TranscriptRead(events, byte_offset + consumed, seq, skipped)


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


def last_event(path: Path, *, tail: int = 65536) -> TranscriptEvent | None:
    """The most recent renderable event, read cheaply from the file tail. Used
    to tell whether the agent's turn is still open (last line is a tool call /
    tool result / user prompt → busy) or closed (last line is an assistant text
    reply → waiting for input)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail:
                f.seek(size - tail)
                f.readline()  # discard the partial first line after a mid-file seek
            data = f.read()
    except OSError:
        return None
    found: TranscriptEvent | None = None
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if _is_compact_boundary(obj):
            # A completed /compact (or other slash command) closes the turn: the
            # agent is idle afterwards, not mid-work. Drop whatever tool_result /
            # prompt preceded it so the open-turn probe reads as waiting.
            found = None
            continue
        ev = _event_from_line(0, obj)
        if ev is not None:
            found = ev
    return found


def recent_conversation(
    path: Path, *, limit: int = 4, tail: int = 1024 * 1024
) -> list[TranscriptEvent]:
    """Recent conversational messages from a bounded complete-line file tail."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail:
                handle.seek(size - tail)
                handle.readline()
            data = handle.read(tail)
    except OSError:
        return []
    events = []
    for raw in data.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        event = _event_from_line(0, obj)
        if event is not None and event.role in ("user", "assistant") and event.text:
            events.append(event)
    real_user_texts = {
        event.text for event in events if event.role == "user" and not event.queued
    }
    events = [
        event
        for event in events
        if not (event.queued and event.text in real_user_texts)
    ]
    return events[-limit:]


def last_context_tokens(path: Path, *, tail: int = 65536) -> int | None:
    """Current context-window occupancy in tokens: the input side of the most
    recent usage block (``input + cache_read + cache_creation``) — i.e. how large
    the prompt last sent to the model was, which is how full the context window
    is right now. Deliberately NOT ``token_totals``: that sums cache reads over
    every turn and so balloons far past the window. Read cheaply from the tail
    (the latest usage block lives near the end); None when no usage is present."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail:
                f.seek(size - tail)
                f.readline()  # discard the partial first line after a mid-file seek
            data = f.read()
    except OSError:
        return None
    latest: dict | None = None
    reset = False  # a compaction *after* the last usage block makes it stale
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("isCompactSummary"):
            reset = True  # context was just compacted; the last usage predates it
        message = obj.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if isinstance(usage, dict):
            latest = usage
            reset = False  # a usage block after the compaction reflects the new size
    if latest is None or reset:
        # No usage yet, or the only usage predates a compaction — the real
        # (smaller) context isn't known until the session's next turn.
        return None
    return (
        int(latest.get("input_tokens", 0) or 0)
        + int(latest.get("cache_read_input_tokens", 0) or 0)
        + int(latest.get("cache_creation_input_tokens", 0) or 0)
    )


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


# Shortest fragment we'll treat as a real question (filters ternaries / "y?").
_QUESTION_MIN_LEN = 8
# Split on sentence terminators OR line breaks — a bulleted / colon-terminated
# block has no full stops, so a newline is often the only boundary before a
# trailing "So, what's next?".
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|[\r\n]+")
# Markdown emphasis / quote / bracket characters that can wrap a question so it
# ends in "?**" or '?"' rather than a bare "?". Stripped from both ends before
# judging (and off the returned text) so a bolded question still registers.
_QUESTION_WRAP = "*_`~\"'”’)]}>"


def trailing_question(text: str | None) -> str | None:
    """The last natural-language question in an agent message, or None.

    Heuristic: split into sentences (also on line breaks) and return the final
    one ending in ``?`` — skipping short or code-like fragments (a lone token, a
    ternary, a URL query string) that end in a question mark without being a
    question. A trailing markdown wrapper (``**…?**``, ``_…?_``, ``"…?"``) is
    peeled off first, so an emphasised question is not missed. Used to surface
    "the agent is waiting on an answer" on the card."""
    if not text or "?" not in text:
        return None
    for part in reversed(_SENTENCE_SPLIT.split(text)):
        p = " ".join(part.split())  # flatten any internal whitespace
        core = p.rstrip(_QUESTION_WRAP)  # peel a bold/quote wrapper: "…?**" -> "…?"
        # A real question ends "word?"; a ternary ("a ? b") has a space before
        # the mark, and a URL query keeps the "?" mid-sentence (so the split
        # never ends a segment there).
        if core.endswith("?") and len(core) >= _QUESTION_MIN_LEN and core[-2] != " ":
            return core.lstrip(_QUESTION_WRAP + " ")
    return None


def _assistant_text(obj: dict) -> str | None:
    """The visible text of an assistant line (text blocks only; tool_use and
    thinking blocks are ignored) — used as the agent's 'latest response'."""
    message = obj.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content.strip()[:_MAX_TEXT] or None
    if isinstance(content, list):
        parts = [
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        joined = " ".join(parts).strip()
        return joined[:_MAX_TEXT] or None
    return None


def transcript_meta(
    path: Path, *, head: int = 65536, tail: int = 32768
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Cheap extraction without parsing the whole file → (ai_title, last_prompt,
    first_user_text, cwd, last_agent_text, last_role). Claude Code writes an
    ``ai-title`` line (a concise session summary), ``last-prompt`` lines, and a
    ``cwd`` field on every entry; we read the head (ai-title/first prompt/cwd
    live there) and the tail (latest last-prompt + latest assistant reply),
    latest occurrence winning. ``last_role`` ("user"/"agent") is who sent the
    most recent *text* message, so the card can order the two lines correctly.
    ``cwd`` is shown for idle sessions too — the live-process registry is gone
    once a session is idle."""
    ai_title: str | None = None
    last_prompt: str | None = None
    first_user: str | None = None
    cwd: str | None = None
    last_text: str | None = None
    last_role: str | None = None
    compacted = False  # a /compact with no real prompt after it → drop stale last-prompt
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            head_bytes = f.read(head)
            tail_bytes = b""
            if size > head:
                f.seek(max(head, size - tail))
                tail_bytes = f.read()
    except OSError:
        return (None, None, None, None, None, None)

    def scan(blob: bytes, skip_first_partial: bool) -> None:
        nonlocal ai_title, last_prompt, first_user, cwd, last_text, last_role, compacted
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
            if _is_compact_boundary(obj):
                # A completed /compact resets the conversation: drop the stale
                # pre-compact prompt/reply so the card doesn't resurface an old
                # turn (the cheap head+tail read can't reach the real latest
                # message past a huge summary). A real turn after the compaction
                # repopulates these below.
                compacted = True
                last_prompt = None
                last_text = None
                last_role = None
                continue
            if cwd is None and isinstance(obj.get("cwd"), str) and obj["cwd"]:
                cwd = obj["cwd"]
            t = obj.get("type")
            if t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                ai_title = obj["aiTitle"].strip() or ai_title
            elif t == "last-prompt" and not compacted and isinstance(obj.get("lastPrompt"), str):
                # bookkeeping fallback — written a few lines *after* the user
                # turn, so the real user line below is what keeps us in sync.
                # Skipped after a compaction: its record is the stale pre-compact
                # prompt, which would undo the reset above.
                last_prompt = obj["lastPrompt"].strip() or last_prompt
            elif t == "assistant":
                at = _assistant_text(obj)
                if at:
                    last_text = at
                    last_role = "agent"
            elif t == "user":
                if not _is_noise_user(obj) and not obj.get("isSidechain"):
                    ut = _user_text(obj)  # None for tool_result-only user lines
                    if ut:
                        if first_user is None:
                            first_user = ut
                        last_prompt = ut  # authoritative newest prompt (matches detail)
                        last_role = "user"
                        compacted = False  # a genuine post-compact prompt is current
            elif (
                t == "queue-operation"
                and obj.get("operation") == "enqueue"
                and isinstance(obj.get("content"), str)
                and obj["content"].strip()
            ):
                last_prompt = obj["content"].strip()  # typed-while-busy message
                last_role = "user"
                compacted = False

    scan(head_bytes, skip_first_partial=False)
    if tail_bytes:
        scan(tail_bytes, skip_first_partial=True)
    return (ai_title, last_prompt, first_user, cwd, last_text, last_role)


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
