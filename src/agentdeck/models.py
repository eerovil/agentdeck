"""Provider-neutral data model.

The web layer imports only this module (and the PROVIDERS registry);
providers translate their native session sources into these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class SessionStatus(StrEnum):
    LIVE = "live"  # owning process running locally — read-only + deep-link
    IDLE = "idle"  # transcript exists, no live pid — read-only
    REMOTE = "remote"  # cloud-only, no local transcript — deep-link only


class Capability(StrEnum):
    TRANSCRIPT = "transcript"
    INJECT = "inject"
    DEEPLINK = "deeplink"
    STEER = "steer"
    INTERRUPT = "interrupt"
    INTERACT = "interact"


@dataclass(frozen=True)
class Account:
    key: str  # "claude_code:main" — provider_id ":" label-slug
    provider_id: str
    label: str  # from config: "main", "alt"
    root: Path  # CLAUDE_CONFIG_DIR for claude_code


@dataclass(frozen=True)
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


@dataclass
class Session:
    key: str  # f"{account_key}:{session_id}" — used in all URLs (urlsafe)
    account_key: str
    session_id: str  # provider-native id (Claude UUID)
    status: SessionStatus
    thinking: bool = False  # LIVE and actively writing its transcript right now
    cwd: Path | None = None
    title: str | None = None  # from history.jsonl "display"
    last_prompt: str | None = None  # user's most recent prompt
    last_text: str | None = None  # agent's most recent response text
    last_role: str | None = None  # "user" | "agent": who sent the most recent message
    question: str | None = None  # trailing question from the agent's latest reply (awaiting you)
    activity: str | None = None  # what it's doing now: "Using tools" / "Working"
    subagent_count: int = 0  # currently-running Codex spawned agents owned by this chat
    model: str | None = None  # last assistant line's model (v0.2)
    kind: str | None = None  # "interactive" | "sdk-cli" | RC worker …
    worker_type: str | None = None  # "kanban" | "cloud" | "you" — drives list colour
    issue_url: str | None = None  # GitHub issue/PR link for kanban worker sessions
    issue_status: str | None = None  # GitHub state text: open / closed / merged
    issue_status_kind: str | None = None  # badge colour: open|merged|done|dropped|closed
    pid: int | None = None
    proc_start: str | None = None  # /proc starttime token — pid-reuse guard
    started_at: datetime | None = None
    last_activity: datetime | None = None
    tokens: TokenTotals | None = None  # summed from transcript usage blocks (v0.2)
    context_tokens: int | None = None  # context-window occupancy (input side of latest usage block)
    deep_link: str | None = None  # provider-native URL when applicable
    deep_link_label: str | None = None
    show_when_idle: bool = False  # keep in the dashboard after the native process exits
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @property
    def display_state(self) -> str:
        """User-facing state, in the vocabulary that matters: a session with a
        live process that isn't writing is **idle** (alive but resting); only one
        actively writing is **thinking**. (``status`` stays process-based — LIVE
        means a process exists; providers may still keep no-process sessions in
        the dashboard with ``show_when_idle``.)"""
        if self.thinking:
            return "thinking"
        if self.status == SessionStatus.LIVE:
            return "idle"
        return self.status.value


@dataclass
class UsageSnapshot:
    account_key: str
    five_hour_pct: float | None
    five_hour_resets_at: datetime | None
    seven_day_pct: float | None
    seven_day_resets_at: datetime | None
    fetched_at: datetime
    stale: bool = False  # true when backoff/errors mean this is old data


@dataclass(frozen=True)
class InjectResult:
    accepted: bool
    reason: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class InteractionOption:
    label: str
    description: str = ""
    value: str | None = None


@dataclass(frozen=True)
class InteractionQuestion:
    id: str
    header: str
    prompt: str
    options: tuple[InteractionOption, ...] = ()
    allow_other: bool = False
    secret: bool = False


@dataclass(frozen=True)
class PendingInteraction:
    """A provider-neutral request that blocks an active agent turn."""

    id: str
    kind: str
    thread_id: str
    turn_id: str | None
    title: str
    message: str | None = None
    questions: tuple[InteractionQuestion, ...] = ()
    command: str | None = None
    cwd: str | None = None
    url: str | None = None
    decisions: tuple[str, ...] = ()


# An open turn older than this is considered stalled (hung worker), not busy —
# without it, a dead-but-LIVE process whose last line is a tool call would show
# "Using tools" forever.
STALL_S = 300.0


def activity_label(
    live: bool, streaming: bool, last_ev, age_s: float = 0.0, stall_s: float = STALL_S
) -> str | None:
    """What the agent is doing right now, or None when idle/dead/stalled.

    Keyed off the *open turn*, not just recent writes, so a long tool run or a
    slow first token doesn't read as idle:
    - last line is an unanswered AskUserQuestion → None (the agent is paused on
      *your* answer, not working — the question is surfaced separately);
    - last line is a tool call / tool result → "Using tools" (persists through
      long tools, where the transcript is quiet for the tool's whole duration);
    - last line is an unanswered user/queued prompt → "Working";
    - actively writing (recent transcript write) → "Working";
    - open turn but no write for ``stall_s`` → stalled, treated as idle;
    - otherwise (LIVE but quiet, last line a finished reply) → None (idle)."""
    if not live:
        return None
    # An unanswered AskUserQuestion is the agent waiting on the user, not busy —
    # regardless of how recently it was written (streaming just finished a
    # question). Surfaced as a question on the card instead of an activity badge.
    if last_ev is not None and last_ev.tool_name == "AskUserQuestion":
        return None
    if last_ev is not None and age_s < stall_s:
        if last_ev.role == "tool" or last_ev.tool_name:
            return "Using tools"
        if last_ev.role == "user":
            return "Working"
    if streaming:
        return "Working"
    return None


def detailed_activity_label(label: str | None, last_ev) -> str | None:
    """Add a compact user-facing detail to a generic tool activity label."""
    if label != "Using tools" or last_ev is None or not last_ev.tool_name:
        return label
    name = last_ev.tool_name.rsplit("__", 1)[-1].replace("_", " ").strip()
    folded_name = name.casefold()
    if folded_name == "reasoning":
        return "Thinking"
    if folded_name in ("wait", "write stdin"):
        return "Waiting for command output"
    summary = (last_ev.tool_summary or "").strip()
    if not summary:
        display_name = {"exec": "shell", "exec command": "shell"}.get(folded_name, name)
        return f"Using {display_name}" if display_name else label

    key, separator, value = summary.partition(": ")
    if separator:
        action = {
            "cmd": "Running",
            "command": "Running",
            "path": "Accessing",
            "query": "Searching",
            "url": "Opening",
        }.get(key.casefold())
        if action:
            return f"{action}: {value}"
    if last_ev.tool_name == "apply_patch" and summary.startswith("***"):
        return "Editing files"
    return f"{name.title()}: {summary}" if name else label


@dataclass
class TranscriptEvent:  # normalized transcript line (parsed from v0.2)
    seq: int  # monotonically increasing per session (line number)
    role: str  # "user" | "assistant" | "tool" | "system"
    text: str | None = None
    tool_name: str | None = None
    tool_summary: str | None = None  # short rendering of tool_use input
    question: str | None = None  # AskUserQuestion prompt, when this line asks one
    model: str | None = None
    usage: dict | None = None  # raw usage block passthrough
    ts: datetime | None = None
    subagent: str | None = None  # set when from <uuid>/subagents/
    queued: bool = False  # user message typed while the agent was busy (enqueued)
    tool_detail: str | None = None  # expandable provider-native tool input


@dataclass
class TranscriptDetail:  # v0.2 — the bundle a session detail page needs
    events: list[TranscriptEvent]  # windowed (most-recent slice) for display
    tokens: TokenTotals  # summed over the WHOLE transcript
    model: str | None
    todos: list[dict]
    total_events: int  # count across the whole transcript
    earliest_seq: int  # smallest seq in ``events`` (for "load earlier")
    skipped: int = 0
