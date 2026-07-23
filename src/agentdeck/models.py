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


# The control capabilities a deck-owned runtime agent can grant. Read-only
# affordances (TRANSCRIPT, DEEPLINK) are derived separately from the transcript
# and are never part of this set. Providers strip this whole set before
# reapplying the live projection so a stale control capability cannot linger.
CONTROL_CAPABILITIES = frozenset(
    {Capability.INJECT, Capability.STEER, Capability.INTERRUPT, Capability.INTERACT}
)


def runtime_control_capabilities(
    *, available: bool, active_turn: bool, actionable_interaction: bool
) -> frozenset[Capability]:
    """Control capabilities an owned runtime agent grants right now.

    This is the single home for the "what does ownership grant" policy that both
    providers apply on top of their own read-only capabilities. It is a pure
    projection of three facts each provider reads its own way:

    - ``available`` — the owning runtime is reachable and owns this session;
      without it no control capability is granted (an idle owned worker whose
      runtime is unreachable is read-only).
    - ``active_turn`` — a turn is in flight, so it can be steered or interrupted.
    - ``actionable_interaction`` — a pending interaction is currently answerable.

    Ownership grants INJECT (queue/steer the next turn); an active turn adds
    STEER and INTERRUPT; an actionable interaction adds INTERACT.
    """
    if not available:
        return frozenset()
    capabilities = {Capability.INJECT}
    if active_turn:
        capabilities |= {Capability.STEER, Capability.INTERRUPT}
    if actionable_interaction:
        capabilities.add(Capability.INTERACT)
    return frozenset(capabilities)


@dataclass(frozen=True)
class Account:
    key: str  # "claude_code:main" — provider_id ":" label-slug
    provider_id: str
    label: str  # from config: "main", "alt"
    root: Path  # CLAUDE_CONFIG_DIR for claude_code


@dataclass(frozen=True)
class GeneratedTitle:
    """Deckhand's persisted semantic title for one provider session."""

    title: str
    evidence_signature: str
    updated_at: datetime


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
class SubagentProgress:
    """Compact progress for one Codex agent spawned by a parent chat."""

    agent_id: str
    nickname: str | None = None
    role: str | None = None
    task: str | None = None
    status: str = "working"  # working | quiet | finished | failed
    result: str | None = None
    started_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class Session:
    key: str  # f"{account_key}:{session_id}" — used in all URLs (urlsafe)
    account_key: str
    session_id: str  # provider-scoped source id; child agents may use their agent id
    status: SessionStatus
    # User-facing Working Session state. This stays true through quiet tool-use
    # gaps; it turns false only when the turn completes, waits on the operator,
    # or crosses the stall threshold.
    thinking: bool = False
    stalled: bool = False  # an Active Turn with no Turn Progress for STALL_S
    # Explicit provider lifecycle, when one exists. None means transcript
    # structure/brief recency is the best available Active Turn evidence.
    lifecycle_active: bool | None = None
    cwd: Path | None = None
    title: str | None = None  # provider-native title
    generated_title: str | None = None  # Deckhand display title; native title stays intact
    initial_prompt: str | None = None  # first real user prompt; stable ownership/reference context
    last_prompt: str | None = None  # user's most recent prompt
    last_text: str | None = None  # agent's most recent response text
    last_role: str | None = None  # "user" | "agent": who sent the most recent message
    question: str | None = None  # trailing question from the agent's latest reply (awaiting you)
    activity: str | None = None  # what it's doing now: "Using tools" / "Working"
    subagent_count: int = 0  # currently-running Codex spawned agents owned by this chat
    subagents: tuple[SubagentProgress, ...] = ()  # active + recently-finished agents
    parent_session_key: str | None = None  # set on a subagent session: the parent it nests under
    #  (kept out of the top-level list and shown as a compact row under the parent)
    model: str | None = None  # last assistant line's model (v0.2)
    kind: str | None = None  # "interactive" | "sdk-cli" | RC worker …
    worker_type: str | None = None  # "kanban" | "cloud" | "you" — drives list colour
    # Broad background/child-work marker used for Deckhand exclusion; recorded
    # delegation lineage is tracked separately in AppState.
    is_delegated: bool = False
    issue_url: str | None = None  # GitHub issue/PR link for kanban worker sessions
    issue_status: str | None = None  # GitHub state text: open / closed / merged
    issue_status_kind: str | None = None  # badge colour: open|merged|done|dropped|closed
    pid: int | None = None
    proc_start: str | None = None  # /proc starttime token — pid-reuse guard
    started_at: datetime | None = None
    last_activity: datetime | None = None
    # Latest execution advancement (assistant/reasoning output, tool call/result,
    # or descendant work). Presentation metadata must not move this clock.
    last_progress: datetime | None = None
    tokens: TokenTotals | None = None  # summed from transcript usage blocks (v0.2)
    context_tokens: int | None = None  # context-window occupancy (input side of latest usage block)
    deep_link: str | None = None  # provider-native URL when applicable
    deep_link_label: str | None = None
    show_when_idle: bool = False  # keep in the dashboard after the native process exits
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @property
    def display_state(self) -> str:
        """User-facing state, in the vocabulary that matters: a session with a
        live process but no Active Turn is **idle** (alive but resting); a
        Working Session is **thinking**, including quiet tool-use gaps.
        (``status`` stays process-based — LIVE means a process exists; providers
        may still keep no-process sessions in the dashboard.)"""
        if self.thinking:
            return "thinking"
        if self.status == SessionStatus.LIVE:
            return "idle"
        return self.status.value

    @property
    def display_title(self) -> str:
        return self.generated_title or self.title or self.session_id[:8]

    @property
    def is_waiting(self) -> bool:
        """Waiting Session (CONTEXT.md): paused on a question directed at the operator."""
        return bool(self.question)


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
    transcript_expected: bool = True


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
    multiselect: bool = False  # render checkboxes (many answers) rather than radios


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

    @classmethod
    def from_dict(cls, data: object) -> PendingInteraction | None:
        """Rebuild a PendingInteraction from its ``asdict`` wire form — the single
        inverse of ``dataclasses.asdict``, so the two provider runtimes that
        serialize this neutral shape over their sockets deserialize it the same
        way. Tolerant of a partial/legacy dict: a missing or non-string ``id``
        yields None."""
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return None
        questions = tuple(
            InteractionQuestion(
                id=str(q.get("id")),
                header=q.get("header") or "",
                prompt=q.get("prompt") or "",
                options=tuple(
                    InteractionOption(
                        label=o.get("label") or "",
                        description=o.get("description") or "",
                        value=o["value"] if isinstance(o.get("value"), str) else None,
                    )
                    for o in (q.get("options") or [])
                    if isinstance(o, dict)
                ),
                allow_other=bool(q.get("allow_other")),
                secret=bool(q.get("secret")),
                multiselect=bool(q.get("multiselect")),
            )
            for q in (data.get("questions") or [])
            if isinstance(q, dict)
        )
        return cls(
            id=data["id"],
            kind=data.get("kind") or "question",
            thread_id=data.get("thread_id") or "",
            turn_id=data["turn_id"] if isinstance(data.get("turn_id"), str) else None,
            title=data.get("title") or "",
            message=data["message"] if isinstance(data.get("message"), str) else None,
            questions=questions,
            command=data["command"] if isinstance(data.get("command"), str) else None,
            cwd=data["cwd"] if isinstance(data.get("cwd"), str) else None,
            url=data["url"] if isinstance(data.get("url"), str) else None,
            decisions=tuple(str(d) for d in (data.get("decisions") or [])),
        )


# An open turn older than this is considered stalled (hung worker), not busy —
# without it, a dead-but-LIVE process whose last line is a tool call would show
# "Using tools" forever.
STALL_S = 600.0


def event_turn_open(last_ev: TranscriptEvent | None) -> bool | None:
    """Structural Active Turn evidence carried by the latest transcript event."""
    if last_ev is None:
        return None
    if last_ev.tool_name == "AskUserQuestion" or last_ev.question:
        return False
    if last_ev.turn_continues is not None:
        return last_ev.turn_continues
    if last_ev.role in {"user", "tool"} or last_ev.tool_name:
        return True
    return None


def event_progress_at(
    last_ev: TranscriptEvent | None, fallback: datetime | None
) -> datetime | None:
    """Latest execution-progress timestamp, excluding non-event file writes."""
    return last_ev.ts if last_ev is not None and last_ev.ts is not None else fallback


def transcript_event_is_progress(event: TranscriptEvent) -> bool:
    """Whether a normalized transcript event advances execution.

    Usage/token bookkeeping is intentionally excluded. Visible model output,
    prompts, tool calls/results, reasoning heartbeats, and descendant lifecycle
    events are progress.
    """
    return bool(
        event.role == "tool"
        or event.text
        or event.tool_name
        or event.question
        or event.answer
        or event.image_media_types
        or event.subagent_status
    )


def turn_stalled(
    *,
    live: bool,
    lifecycle_active: bool | None,
    last_ev: TranscriptEvent | None,
    age_s: float,
    stall_s: float = STALL_S,
) -> bool:
    """Whether current normalized evidence represents a Stalled Turn."""
    if not live or (last_ev is not None and last_ev.tool_name == "AskUserQuestion"):
        return False
    active = lifecycle_active
    if active is None:
        active = event_turn_open(last_ev)
    return active is True and age_s >= stall_s


def activity_label(
    live: bool,
    streaming: bool,
    last_ev,
    age_s: float = 0.0,
    stall_s: float = STALL_S,
    *,
    lifecycle_active: bool | None = None,
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
    active = lifecycle_active
    if active is None:
        active = event_turn_open(last_ev)
    # Explicit lifecycle/structure closes immediately; recency is only a
    # fallback for an ambiguous assistant event or an empty live tail.
    if active is False:
        return None
    if active is None:
        return "Working" if streaming else None
    if age_s >= stall_s:
        return None
    if last_ev is not None and (last_ev.role == "tool" or last_ev.tool_name):
        return "Using tools"
    return "Working"


def detailed_activity_label(label: str | None, last_ev) -> str | None:
    """Add a compact user-facing detail to a generic tool activity label."""
    if label != "Using tools" or last_ev is None or not last_ev.tool_name:
        return label
    name = last_ev.tool_display_name or (
        last_ev.tool_name.rsplit("__", 1)[-1].replace("_", " ").strip()
    )
    folded_name = name.casefold()
    if folded_name == "reasoning":
        return "Thinking"
    if folded_name in ("wait", "write stdin"):
        return "Waiting for command output"
    if folded_name == "wait for agents":
        return "Waiting for subagents"
    if folded_name == "start agents":
        return "Starting subagents"
    summary = (last_ev.tool_summary or "").strip()
    if folded_name == "approval":
        reason = summary.removeprefix("reason: ")
        return f"Requesting approval: {reason}" if reason else "Requesting approval"
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
    tool_display_name: str | None = None  # optional user-facing override
    tool_summary: str | None = None  # short rendering of tool_use input
    question: str | None = None  # AskUserQuestion prompt, when this line asks one
    answer: str | None = None  # your reply to an AskUserQuestion (from its tool_result)
    model: str | None = None
    tokens: TokenTotals | None = None  # normalized token usage for this line
    ts: datetime | None = None
    subagent: str | None = None  # set when from <uuid>/subagents/
    queued: bool = False  # user message typed while the agent was busy (enqueued)
    # Provider-declared continuation on this event: True keeps the Active Turn
    # open, False closes it, None leaves structure/brief recency to decide.
    turn_continues: bool | None = None
    tool_detail: str | None = None  # expandable provider-native tool input
    subagent_status: str | None = None  # compact spawned-agent lifecycle update
    subagent_id: str | None = None
    subagent_name: str | None = None
    subagent_identities: tuple[tuple[str, str], ...] = ()  # spawn output id/name pairs
    image_media_types: tuple[str, ...] = ()  # safe raster images on this message


@dataclass
class TranscriptDetail:  # v0.2 — the bundle a session detail page needs
    events: list[TranscriptEvent]  # windowed (most-recent slice) for display
    tokens: TokenTotals  # summed over the WHOLE transcript
    model: str | None
    todos: list[dict]
    total_events: int  # count across the whole transcript
    earliest_seq: int  # smallest seq in ``events`` (for "load earlier")
    skipped: int = 0
