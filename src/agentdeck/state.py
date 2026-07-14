"""AppState: single in-memory source of truth for sessions and usage.

Mutations publish coarse-grained topics to the EventBus:
- "sessions" — the session list changed (SSE re-renders the whole list)
- "usage"    — account limits or host resource usage changed
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .events import EventBus
from .host_stats import HostStats
from .models import Session, SessionStatus, UsageSnapshot

if TYPE_CHECKING:
    from .db import Db, NullDb

_STATUS_ORDER = {SessionStatus.LIVE: 0, SessionStatus.IDLE: 1, SessionStatus.REMOTE: 2}


class AppState:
    def __init__(self, bus: EventBus | None = None, db: Db | NullDb | None = None):
        self.bus = bus or EventBus()
        self.db = db
        self.sessions: dict[str, Session] = {}
        self.usage: dict[str, UsageSnapshot] = {}
        self.host_stats: HostStats | None = None
        self.transcript_offsets: dict[str, tuple[int, int]] = {}  # v0.2: (byte_offset, seq)

    # --- sessions -----------------------------------------------------

    def replace_account_sessions(self, account_key: str, sessions: list[Session]) -> bool:
        """Replace all sessions of one account; returns True (and publishes) on change."""
        new = {s.key: s for s in sessions}
        old = {k: v for k, v in self.sessions.items() if v.account_key == account_key}
        if new == old:
            return False
        for k in old.keys() - new.keys():
            del self.sessions[k]
        self.sessions.update(new)
        if self.db is not None:
            self.db.upsert_sessions_seen(sessions)
        self.bus.publish("sessions")
        return True

    def update_session(self, session: Session) -> None:
        if self.sessions.get(session.key) == session:
            return
        self.sessions[session.key] = session
        self.bus.publish("sessions")

    def _sort_key(self, s: Session) -> tuple[int, int, float]:
        status_order = _STATUS_ORDER.get(s.status, 9)
        if s.status == SessionStatus.IDLE and s.show_when_idle:
            # Providers such as Codex cannot reliably map native process state
            # to session activity, so do not penalize their visible idle sessions.
            status_order = _STATUS_ORDER[SessionStatus.LIVE]
        return (
            status_order,
            0 if s.thinking else 1,  # actively-working sessions float to the top
            # Activity timestamps change on every streamed event. Sorting active
            # chats by that timestamp made multiple workers continually trade
            # the top spot. Tie them here: Python's stable sort preserves their
            # insertion order until a chat enters or leaves the active tier.
            0.0
            if s.thinking
            else -(s.last_activity.timestamp() if s.last_activity else 0.0),
        )

    def sessions_for_account(self, account_key: str) -> list[Session]:
        return sorted(
            (s for s in self.sessions.values() if s.account_key == account_key),
            key=self._sort_key,
        )

    def all_sessions(self) -> list[Session]:
        """All sessions across accounts, working first then most-recently-active."""
        return sorted(self.sessions.values(), key=self._sort_key)

    def visible_sessions(self) -> list[Session]:
        """Sessions providers consider useful in the dashboard.

        Idle sessions are hidden by default, but providers whose process state
        cannot be mapped reliably (such as Codex) can keep them visible.
        """
        return [
            s
            for s in self.all_sessions()
            if s.status != SessionStatus.IDLE or s.show_when_idle
        ]

    # --- usage --------------------------------------------------------

    def set_host_stats(self, snapshot: HostStats) -> None:
        self.host_stats = snapshot
        self.bus.publish("usage")

    def set_usage(self, snapshot: UsageSnapshot) -> None:
        self.usage[snapshot.account_key] = snapshot
        if self.db is not None:
            self.db.record_usage(snapshot)
        self.bus.publish("usage")

    def mark_usage_stale(self, account_key: str) -> None:
        snap = self.usage.get(account_key)
        if snap is not None and not snap.stale:
            self.usage[account_key] = replace(snap, stale=True)
            self.bus.publish("usage")
