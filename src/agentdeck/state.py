"""AppState: single in-memory source of truth for sessions and usage.

Mutations publish coarse-grained topics to the EventBus:
- "sessions" — the session list changed (SSE re-renders the whole list)
- "usage"    — a usage snapshot changed (SSE re-renders the limit bars)
"""

from __future__ import annotations

from dataclasses import replace

from .events import EventBus
from .models import Session, SessionStatus, UsageSnapshot

_STATUS_ORDER = {SessionStatus.LIVE: 0, SessionStatus.IDLE: 1, SessionStatus.REMOTE: 2}


class AppState:
    def __init__(self, bus: EventBus | None = None):
        self.bus = bus or EventBus()
        self.sessions: dict[str, Session] = {}
        self.usage: dict[str, UsageSnapshot] = {}
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
        self.bus.publish("sessions")
        return True

    def update_session(self, session: Session) -> None:
        if self.sessions.get(session.key) == session:
            return
        self.sessions[session.key] = session
        self.bus.publish("sessions")

    def sessions_for_account(self, account_key: str) -> list[Session]:
        return sorted(
            (s for s in self.sessions.values() if s.account_key == account_key),
            key=lambda s: (
                _STATUS_ORDER.get(s.status, 9),
                -(s.last_activity.timestamp() if s.last_activity else 0.0),
            ),
        )

    # --- usage --------------------------------------------------------

    def set_usage(self, snapshot: UsageSnapshot) -> None:
        self.usage[snapshot.account_key] = snapshot
        self.bus.publish("usage")

    def mark_usage_stale(self, account_key: str) -> None:
        snap = self.usage.get(account_key)
        if snap is not None and not snap.stale:
            self.usage[account_key] = replace(snap, stale=True)
            self.bus.publish("usage")
