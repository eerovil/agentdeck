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
    DEEPLINK = "deeplink"


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
    model: str | None = None  # last assistant line's model (v0.2)
    kind: str | None = None  # "interactive" | "sdk-cli" | RC worker …
    pid: int | None = None
    proc_start: str | None = None  # /proc starttime token — pid-reuse guard
    started_at: datetime | None = None
    last_activity: datetime | None = None
    tokens: TokenTotals | None = None  # summed from transcript usage blocks (v0.2)
    deep_link: str | None = None  # claude.ai URL when applicable
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @property
    def display_state(self) -> str:
        """User-facing state, in the vocabulary that matters: a session with a
        live process that isn't writing is **idle** (alive but resting); only one
        actively writing is **thinking**. (``status`` stays process-based — LIVE
        means a process exists, and only those are listed; no-process sessions
        are hidden.)"""
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


@dataclass
class TranscriptEvent:  # normalized transcript line (parsed from v0.2)
    seq: int  # monotonically increasing per session (line number)
    role: str  # "user" | "assistant" | "tool" | "system"
    text: str | None = None
    tool_name: str | None = None
    tool_summary: str | None = None  # short rendering of tool_use input
    model: str | None = None
    usage: dict | None = None  # raw usage block passthrough
    ts: datetime | None = None
    subagent: str | None = None  # set when from <uuid>/subagents/
    queued: bool = False  # user message typed while the agent was busy (enqueued)


@dataclass
class TranscriptDetail:  # v0.2 — the bundle a session detail page needs
    events: list[TranscriptEvent]  # windowed (most-recent slice) for display
    tokens: TokenTotals  # summed over the WHOLE transcript
    model: str | None
    todos: list[dict]
    total_events: int  # count across the whole transcript
    earliest_seq: int  # smallest seq in ``events`` (for "load earlier")
    skipped: int = 0
