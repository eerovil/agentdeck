"""AppState: single in-memory source of truth for sessions and usage.

Mutations publish coarse-grained topics to the EventBus:
- "sessions" — the session list changed (SSE re-renders the whole list)
- "usage"    — account limits or host resource usage changed
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .events import EventBus
from .host_stats import HostStats
from .models import GeneratedTitle, Session, SessionStatus, UsageSnapshot

if TYPE_CHECKING:
    from .db import Db, NullDb

_STATUS_ORDER = {SessionStatus.LIVE: 0, SessionStatus.IDLE: 1, SessionStatus.REMOTE: 2}
_ISSUE_URL_RE = re.compile(r"https://github\.com/[^/]+/([^/]+)/(?:issues|pull)/(\d+)")
_KANBAN_MODE_RE = re.compile(r"\s+\((review|merge-fix|merge-arm|resume)\)$", re.IGNORECASE)


def generated_display_title(session: Session, semantic_title: str) -> str:
    """Attach stable issue identity around a Deckhand-generated semantic title."""
    match = _ISSUE_URL_RE.match(session.issue_url or "")
    if match is None:
        return semantic_title
    suffix_match = _KANBAN_MODE_RE.search(session.title or "")
    suffix = suffix_match.group(0) if suffix_match else ""
    return f"{match.group(1)}#{match.group(2)} · {semantic_title}{suffix}"


class AppState:
    def __init__(self, bus: EventBus | None = None, db: Db | NullDb | None = None):
        self.bus = bus or EventBus()
        self.db = db
        self.sessions: dict[str, Session] = {}
        self.usage: dict[str, UsageSnapshot] = {}
        # A usage snapshot is shown as "stale" only once it is older than this
        # (seconds) — data age, not a single failed poll (issue #6). Resolved
        # from the poll interval at app startup; this is the safe default.
        self.usage_stale_after_s: float = 300.0
        self.host_stats: HostStats | None = None
        self.transcript_offsets: dict[str, tuple[int, int]] = {}  # v0.2: (byte_offset, seq)
        # child_key -> delegating parent_key, discovered from delegation markers
        # in each provider's transcripts. Cross-provider (a Claude chat that
        # delegated a Codex session, etc.) is why this lives in state rather than
        # on the Session: the child and parent are produced by different scans.
        self.delegation_parents: dict[str, str] = {}
        # child_key -> raw parent session_id, recorded authoritatively at
        # delegation time (the invoking session captured from
        # CLAUDE_CODE_SESSION_ID, etc.). We store the raw id, not a resolved
        # key, and resolve it against the currently-visible sessions at
        # render time: the parent may not be scanned in yet when the child
        # starts, and lazy resolution self-heals that race. DB-backed and kept
        # separate from the scan-discovered ``delegation_parents`` so a provider
        # scan's set_delegation_parents() can never clear it; survives restarts
        # and cross-provider cases the transcripts cannot express (a Claude chat
        # that delegated a Codex run).
        self.recorded_delegation_parents: dict[str, str] = (
            db.load_delegation_parents() if db else {}
        )
        self.delegated_session_keys = db.load_delegated_sessions() if db else set()
        self.generated_titles: dict[str, GeneratedTitle] = (
            db.load_generated_titles() if db else {}
        )

    # --- sessions -----------------------------------------------------

    def replace_account_sessions(self, account_key: str, sessions: list[Session]) -> bool:
        """Replace all sessions of one account; returns True (and publishes) on change."""
        for session in sessions:
            if session.is_delegated and session.key not in self.delegated_session_keys:
                self.delegated_session_keys.add(session.key)
                if self.db is not None:
                    self.db.record_delegated_session(session.key)
        sessions = [
            replace(session, is_delegated=True)
            if session.key in self.delegated_session_keys and not session.is_delegated
            else session
            for session in sessions
        ]
        sessions = [self._with_generated_title(session) for session in sessions]
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
        if session.is_delegated and session.key not in self.delegated_session_keys:
            self.delegated_session_keys.add(session.key)
            if self.db is not None:
                self.db.record_delegated_session(session.key)
        elif session.key in self.delegated_session_keys:
            session = replace(session, is_delegated=True)
        session = self._with_generated_title(session)
        if self.sessions.get(session.key) == session:
            return
        self.sessions[session.key] = session
        self.bus.publish("sessions")

    def _with_generated_title(self, session: Session) -> Session:
        record = self.generated_titles.get(session.key)
        if record is None:
            return session
        title = generated_display_title(session, record.title)
        return (
            replace(session, generated_title=title)
            if session.generated_title != title
            else session
        )

    def set_generated_title(
        self, session_key: str, title: str, evidence_signature: str
    ) -> None:
        record = (
            self.db.record_generated_title(session_key, title, evidence_signature)
            if self.db is not None
            else GeneratedTitle(title, evidence_signature, datetime.now(UTC))
        )
        self.generated_titles[session_key] = record
        session = self.sessions.get(session_key)
        if session is not None:
            updated = self._with_generated_title(session)
            if updated != session:
                self.sessions[session_key] = updated
                self.bus.publish("sessions")

    def mark_delegated_session(
        self, session_key: str, parent_session_id: str | None = None
    ) -> None:
        """Persist machine-started work so Deckhand ignores it across rescans/restarts.

        ``parent_session_id`` is the raw id of the session that started this
        delegation (e.g. the invoking Claude chat). We store it as-is and
        resolve it to a full key lazily in ``session_tree`` so the delegated
        child nests under its parent, including cross-provider, even if the
        parent has not been scanned in yet at delegation time.
        """
        self.delegated_session_keys.add(session_key)
        if parent_session_id:
            self.recorded_delegation_parents[session_key] = parent_session_id
        if self.db is not None:
            self.db.record_delegated_session(session_key, parent_session_id)
        session = self.sessions.get(session_key)
        if session is not None and not session.is_delegated:
            self.sessions[session_key] = replace(session, is_delegated=True)
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

    def set_delegation_parents(self, account_key: str, pairs: dict[str, str]) -> None:
        """Replace the delegation pairs whose PARENT is in ``account_key``.

        Each provider scan reports the child->parent pairs it found in that
        account's transcripts; scoping the clear to parents in the account keeps
        cross-provider pairs from other accounts intact.
        """
        prefix = f"{account_key}:"
        self.delegation_parents = {
            child: parent
            for child, parent in self.delegation_parents.items()
            if not parent.startswith(prefix)
        }
        self.delegation_parents.update(pairs)

    def _parent_key(
        self, session: Session, key_by_session_id: dict[str, str]
    ) -> str | None:
        """A session's parent: its own subagent link, then a delegation link.

        Recorded (delegation-time) parents take precedence over scan-discovered
        ones: they are authoritative and cover cross-provider delegation the
        transcript markers cannot express. The recorded value is a raw
        session_id, resolved here against ``key_by_session_id`` (built from the
        currently-visible sessions); an unresolved or self-referential match
        yields no parent, so the child simply stays top-level.
        """
        if session.parent_session_key:
            return session.parent_session_key
        recorded = self.recorded_delegation_parents.get(session.key)
        if recorded is not None:
            resolved = key_by_session_id.get(recorded)
            return resolved if resolved != session.key else None
        return self.delegation_parents.get(session.key)

    def session_tree(self) -> tuple[list[Session], dict[str, list[Session]]]:
        """Split visible sessions into a one-level tree.

        Returns ``(top_level, children_by_parent_key)``: subagent sessions
        (``parent_session_key`` set) are grouped under their parent. A subagent
        whose parent isn't visible is dropped rather than floated as a top-level
        card — a subagent only makes sense under its parent, and an orphaned one
        (parent aged out) would otherwise clutter the list with contextless
        cards. Order within each list follows ``visible_sessions``' sort.
        """
        visible = self.visible_sessions()
        # Resolve recorded (raw-id) delegation parents against what's visible
        # now; last writer wins on the astronomically-unlikely UUID collision.
        key_by_session_id = {s.session_id: s.key for s in visible}
        top_keys = {
            s.key for s in visible if self._parent_key(s, key_by_session_id) is None
        }
        top: list[Session] = []
        children: dict[str, list[Session]] = {}
        for s in visible:
            parent = self._parent_key(s, key_by_session_id)
            if parent is None:
                top.append(s)
            elif parent in top_keys:
                children.setdefault(parent, []).append(s)
            # else: orphan subagent (parent not visible) — omit from the tree
        return top, children

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
