"""The shared incremental JSONL Transcript Reader.

One deep module turns a provider transcript file into ordered, normalized
``TranscriptEvent``s. Every provider's transcript is a JSONL file read the same
way: seek to a byte cursor, consume only newline-terminated (complete) lines,
count malformed ones instead of raising, and resume from a returned offset. That
machinery — complete-line windowing, ``seq`` monotonicity, skip counting, the
bounded tail probes, and cursor math — lives here once.

The *only* per-provider variability is how one raw line becomes at most one
event. That is a ``LineParser`` adapter with a single required method plus two
defaulted hooks for the two places providers legitimately differ (turn
boundaries and which events count as the open turn). ``event_from_line`` is
where all of a provider's wire-format knowledge stays; nothing about a
provider's line shape leaks into this module.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import TokenTotals, TranscriptEvent

log = logging.getLogger(__name__)


@dataclass
class TranscriptRead:
    events: list[TranscriptEvent]
    byte_offset: int  # resume point for the next incremental read
    seq: int  # highest seq emitted so far
    skipped: int  # malformed lines skipped this read


@dataclass(frozen=True)
class TranscriptMeta:
    """Cheap head/tail session metadata a provider extracts for list cards.

    The result type is shared; each provider fills it from its own envelope.
    Every field defaults to empty so a provider populates only what it knows.
    """

    session_id: str | None = None
    cwd: str | None = None
    started_at: datetime | None = None
    title: str | None = None
    first_prompt: str | None = None
    last_prompt: str | None = None
    last_text: str | None = None
    # The agent's canonical final message from the last completed turn. Unlike
    # last_text (the last assistant *item*), this is what the provider reports as
    # the turn result, so it is immune to intermediate/approval-path noise.
    last_agent_message: str | None = None
    last_role: str | None = None
    model: str | None = None
    kind: str | None = None
    tokens: TokenTotals | None = None
    context_tokens: int | None = None
    is_approval_review: bool = False
    # Helper rollouts (spawned agents and guardian/auto-review runs) can reuse
    # the parent session_id. They are separate files and must never represent
    # the parent conversation in AgentDeck.
    is_subagent: bool = False
    is_spawned_subagent: bool = False
    task_active: bool = False
    task_started_at: datetime | None = None
    agent_id: str | None = None
    agent_nickname: str | None = None
    agent_role: str | None = None
    task_finished_at: datetime | None = None
    task_status: str | None = None


class LineParser:
    """A Provider's adapter: interpret one raw transcript line as one event.

    ``event_from_line`` owns all knowledge of a provider's wire format. The two
    hooks parameterize the only behavioral differences the reader needs:
    ``is_turn_boundary`` lets a provider close the open turn on a boundary line
    (e.g. a compaction), and ``is_probe_event`` lets a provider decide which
    events count when probing "what is the most recent open-turn line".
    """

    def event_from_line(self, seq: int, obj: dict) -> TranscriptEvent | None:
        raise NotImplementedError

    def is_turn_boundary(self, obj: dict) -> bool:
        """A raw line that closes the current turn; the tail probe resets here."""
        return False

    def is_probe_event(self, event: TranscriptEvent) -> bool:
        """Whether ``event`` counts as the latest open-turn line for ``last_event``."""
        return True


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_objects(
    blob: bytes, *, skip_first_partial: bool, require_final_newline: bool
) -> list[dict]:
    """Parse the JSON dict lines out of a byte blob, tolerating malformed lines.

    ``skip_first_partial`` drops the first line (a mid-file seek can land inside
    one); ``require_final_newline`` drops an unterminated trailing line so a
    half-written tail is never parsed.
    """
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


def transcript_cursor(path: Path, *, chunk_size: int = 256 * 1024) -> tuple[int, int]:
    """End cursor ``(byte_offset, seq)`` with bounded memory, counting only
    newline-terminated lines so a live tail resumes exactly after them."""
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


def read_line(path: Path, seq: int) -> dict | None:
    """The exact 1-based line ``seq`` as a JSON dict, or None. Used to fetch the
    one line an image lives on for ``transcript_image``."""
    if seq < 1:
        return None
    try:
        with path.open("rb") as handle:
            raw = next((line for number, line in enumerate(handle, 1) if number == seq), None)
        data = json.loads(raw) if raw is not None else None
    except (OSError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def token_totals(events: list[TranscriptEvent]) -> TokenTotals:
    """Sum the per-line normalized token usage over a transcript's events. Each
    provider's LineParser normalizes its own wire usage onto ``event.tokens``, so
    this fieldwise sum is provider-neutral."""
    input_tokens = output_tokens = cache_read = cache_creation = 0
    for event in events:
        totals = event.tokens
        if totals is None:
            continue
        input_tokens += totals.input_tokens
        output_tokens += totals.output_tokens
        cache_read += totals.cache_read_tokens
        cache_creation += totals.cache_creation_tokens
    return TokenTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
    )


def _read_tail(path: Path, tail: int) -> tuple[bytes, bool]:
    """Read the last ``tail`` bytes → (blob, seeked). ``seeked`` is True when the
    read started mid-file, so its first (partial) line must be dropped."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail:
                handle.seek(size - tail)
            blob = handle.read(tail)
    except OSError:
        return (b"", False)
    return (blob, size > tail)


def _dedupe_queued(events: list[TranscriptEvent]) -> list[TranscriptEvent]:
    """Drop an enqueued message that also appears as its real processed turn.

    A message typed while the agent was busy shows up once as a queue operation
    and again as the real user turn once processed. Keep the real turn; drop the
    queued duplicate. Queued messages never processed have no real turn and stay.
    """
    real_user_texts = {e.text for e in events if e.role == "user" and not e.queued and e.text}
    if not real_user_texts:
        return events
    return [e for e in events if not (e.queued and e.text in real_user_texts)]


class TranscriptReader:
    """Incremental reader bound to one provider's ``LineParser``."""

    def __init__(self, parser: LineParser) -> None:
        self._parser = parser

    def read_events(
        self, path: Path, *, byte_offset: int = 0, seq: int = 0
    ) -> TranscriptRead:
        """Read new complete lines from ``byte_offset`` onward.

        A trailing line without a newline is left unconsumed (the offset stops
        before it) so the next read picks it up once fully written. A truncated
        file (offset past EOF) resets to the start. A parser that raises on a
        line counts as skipped rather than crashing the scan.
        """
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
            try:
                event = self._parser.event_from_line(seq, obj)
            except Exception:  # a bad line must skip/count, never crash the scan
                log.warning("%s: transcript line parser raised; skipping line", path.name)
                skipped += 1
                continue
            if event is not None:
                events.append(event)
        if skipped:
            log.debug("%s: skipped %d malformed transcript lines", path.name, skipped)
        return TranscriptRead(
            _dedupe_queued(events), byte_offset + len(complete), seq, skipped
        )

    def last_event(self, path: Path, *, tail: int = 65536) -> TranscriptEvent | None:
        """The most recent open-turn event from a bounded complete-line tail.

        Tells whether the agent's turn is still open (last line is a tool
        call/result/prompt → busy) or closed (last line is a reply → waiting). A
        turn-boundary line resets the probe; ``is_probe_event`` filters which
        events count.
        """
        blob, skip_first = _read_tail(path, tail)
        found: TranscriptEvent | None = None
        for obj in parse_objects(
            blob, skip_first_partial=skip_first, require_final_newline=True
        ):
            if self._parser.is_turn_boundary(obj):
                found = None
                continue
            event = self._safe_event(obj)
            if event is not None and self._parser.is_probe_event(event):
                found = event
        return found

    def recent_conversation(
        self, path: Path, *, limit: int = 4, tail: int = 1024 * 1024
    ) -> list[TranscriptEvent]:
        """Recent conversational messages from a bounded complete-line tail."""
        blob, skip_first = _read_tail(path, tail)
        events: list[TranscriptEvent] = []
        for obj in parse_objects(
            blob, skip_first_partial=skip_first, require_final_newline=True
        ):
            event = self._safe_event(obj)
            if event is not None and event.role in ("user", "assistant") and event.text:
                events.append(event)
        return _dedupe_queued(events)[-limit:]

    def _safe_event(self, obj: dict) -> TranscriptEvent | None:
        """``event_from_line`` on a probe line, contained: a raiser is dropped."""
        try:
            return self._parser.event_from_line(0, obj)
        except Exception:
            log.warning("transcript line parser raised during a tail probe; dropping")
            return None
