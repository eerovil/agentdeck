"""AppState: single in-memory source of truth for sessions and usage.

Mutations publish coarse-grained topics to the EventBus:
- "sessions" — the session list changed (SSE re-renders the whole list)
- "usage"    — account limits or host resource usage changed
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from .events import EventBus
from .host_stats import HostStats
from .models import GeneratedTitle, PinnedMessage, Session, SessionStatus, UsageSnapshot

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


@dataclass(frozen=True)
class SessionPresentation:
    """One stable, request-scoped projection of the visible session graph."""

    visible: tuple[Session, ...]
    top_level: tuple[Session, ...]
    children_of: Mapping[str, tuple[Session, ...]]
    working_count: int
    _display_by_key: Mapping[str, Session]
    _working_through_subagents: frozenset[str]

    def display(self, session: Session) -> Session:
        """Return this snapshot's effective display copy of ``session``."""
        return self._display_by_key.get(session.key, session)

    def has_working_subagent(self, session: Session) -> bool:
        """Whether embedded or nested subagent work makes ``session`` active."""
        return session.key in self._working_through_subagents

    def active_child_sessions(self, session: Session) -> tuple[Session, ...]:
        """Active child Sessions not already represented as embedded progress.

        Codex native subagents can appear through both ``Session.subagents`` and
        the relationship graph. Delegated Sessions have only the graph entry.
        Keeping this projection here gives session detail and its SSE fragment
        the same authoritative children as the dashboard without duplicating a
        native subagent row.
        """
        embedded_ids = {agent.agent_id for agent in session.subagents}
        return tuple(
            child
            for child in self.children_of.get(session.key, ())
            if child.thinking and child.session_id not in embedded_ids
        )


class AppState:
    def __init__(self, bus: EventBus | None = None, db: Db | NullDb | None = None):
        self.bus = bus or EventBus()
        self.db = db
        self.sessions: dict[str, Session] = {}
        self.session_scan_revisions: dict[str, int] = {}
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
        self.message_pins: dict[str, dict[int, PinnedMessage]] = {}
        if db is not None:
            for pin in db.load_message_pins():
                self.message_pins.setdefault(pin.session_key, {})[pin.seq] = pin

    # --- invalidations -------------------------------------------------

    def sessions_changed(self) -> None:
        """Announce that consumers should render the current session state."""
        self.bus.publish("sessions")

    def usage_changed(self) -> None:
        """Announce that consumers should render the current usage state."""
        self.bus.publish("usage")

    def assistant_changed(self) -> None:
        """Announce that consumers should render the current Deckhand state."""
        self.bus.publish("assistant")

    def pins_changed(self, session_key: str) -> None:
        """Announce a fresh complete pin projection for one session."""
        self.bus.publish("pins", session_key)

    # --- pinned messages ---------------------------------------------

    def pins_for(self, session_key: str) -> tuple[PinnedMessage, ...]:
        return tuple(
            pin
            for _, pin in sorted(self.message_pins.get(session_key, {}).items())
        )

    def pin_message(self, pin: PinnedMessage) -> None:
        current = self.message_pins.setdefault(pin.session_key, {})
        if current.get(pin.seq) == pin:
            return
        current[pin.seq] = pin
        if self.db is not None:
            self.db.record_message_pin(pin)
        self.pins_changed(pin.session_key)

    def unpin_message(self, session_key: str, seq: int) -> bool:
        current = self.message_pins.get(session_key)
        if current is None or current.pop(seq, None) is None:
            return False
        if not current:
            self.message_pins.pop(session_key, None)
        if self.db is not None:
            self.db.delete_message_pin(session_key, seq)
        self.pins_changed(session_key)
        return True

    def sessions_scanned(self, account_key: str) -> None:
        """Record one successful authoritative provider scan for ``account_key``."""
        revision = self.session_scan_revisions.get(account_key, 0) + 1
        self.session_scan_revisions[account_key] = revision
        self.bus.publish("session_scans", account_key)

    def session_scan_revision(self, account_key: str) -> int:
        return self.session_scan_revisions.get(account_key, 0)

    # --- sessions -----------------------------------------------------

    def replace_account_sessions(self, account_key: str, sessions: list[Session]) -> bool:
        """Replace all sessions of one account; returns True (and publishes) on change."""
        admitted = [self._admit(session) for session in sessions]
        new = {s.key: s for s in admitted}
        old = {k: v for k, v in self.sessions.items() if v.account_key == account_key}
        if new == old:
            return False
        for k in old.keys() - new.keys():
            del self.sessions[k]
        self.sessions.update(new)
        if self.db is not None:
            self.db.upsert_sessions_seen(admitted)
        self.sessions_changed()
        return True

    def update_session(self, session: Session) -> None:
        session = self._admit(session)
        if self.sessions.get(session.key) == session:
            return
        self.sessions[session.key] = session
        self.sessions_changed()

    def _admit(self, session: Session) -> Session:
        """Apply the account-session admission rule and return the canonical session.

        Learns and persists a newly self-flagged delegation, stamps
        ``is_delegated=True`` on a session whose key is already persisted, and
        attaches any generated title. Callers own change-detection and publish.
        ``mark_delegated_session`` deliberately does NOT route through here — it
        flags an *un*-self-flagged session unconditionally and with a different
        persist/publish contract.
        """
        if session.is_delegated and session.key not in self.delegated_session_keys:
            self.delegated_session_keys.add(session.key)
            if self.db is not None:
                self.db.record_delegated_session(session.key)
        elif session.key in self.delegated_session_keys:
            session = replace(session, is_delegated=True)
        return self._with_generated_title(session)

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
                self.sessions_changed()

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
        self.sessions_changed()

    def apply_session_changes(
        self,
        sessions: Iterable[Session],
        project: Callable[[Session], object],
    ) -> list[Session]:
        """Own the live-overlay transition: mutate, detect change, publish once.

        The liveness sweep and Persistent Runtime callbacks refresh fields on
        the ``Session`` objects the read model already holds. ``project``
        applies that in-place refresh to each session; a session counts as
        changed only when it differs from a whole-object pre-image, so no
        per-field list can fall out of sync with what ``project`` writes.
        ``"sessions"`` is published exactly once for the batch, and only when
        something changed. Returns the changed sessions (same identities) so a
        sweep can still report what it touched.
        """
        changed: list[Session] = []
        for session in sessions:
            before = replace(session)
            project(session)
            if session != before:
                changed.append(session)
        if changed:
            self.sessions_changed()
        return changed

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
        """Sessions providers consider eligible for dashboard presentation.

        Sessions without confirmed local liveness are hidden by default, but
        providers whose process state cannot be mapped reliably (such as Codex)
        can keep them eligible.
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

    def _effective_activity(
        self, visible: list[Session], parent_by_key: Mapping[str, str | None]
    ) -> tuple[set[str], set[str], dict[str, datetime | None]]:
        """Return effective-working keys, descendant-work keys, and progress.

        Providers keep ``Session.thinking`` scoped to the session's own turn.
        Parent activity is relationship state: Codex may report spawned agents
        directly through ``subagent_count``, while Claude and delegated workers
        appear as child sessions. Propagate either form through the parent graph
        without mutating provider-owned session records.
        """
        own_working = {session.key for session in visible if session.thinking}
        through_subagents = {
            session.key for session in visible if session.subagent_count
        }
        progress: dict[str, datetime | None] = {}
        for session in visible:
            stamps = [session.last_progress or session.last_activity]
            if session.subagent_count:
                stamps.extend(
                    agent.updated_at or agent.started_at
                    for agent in session.subagents
                    if agent.status in ("working", "quiet")
                )
            progress[session.key] = max(
                (stamp for stamp in stamps if stamp is not None), default=None
            )
        frontier = list(own_working | through_subagents)
        while frontier:
            child = frontier.pop()
            parent = parent_by_key.get(child)
            if parent is None:
                continue
            child_progress = progress.get(child)
            parent_progress = progress.get(parent)
            progress_changed = child_progress is not None and (
                parent_progress is None or child_progress > parent_progress
            )
            if progress_changed:
                progress[parent] = child_progress
            newly_working = parent not in through_subagents
            if newly_working:
                through_subagents.add(parent)
            if newly_working or progress_changed:
                frontier.append(parent)
        return own_working | through_subagents, through_subagents, progress

    @staticmethod
    def _with_effective_activity(
        session: Session,
        working_keys: set[str],
        progress: Mapping[str, datetime | None],
    ) -> Session:
        effective_progress = progress.get(session.key)
        if session.key not in working_keys:
            return replace(session, last_progress=effective_progress)
        return replace(
            session,
            thinking=True,
            stalled=False,
            activity=session.activity or "Working",
            last_progress=effective_progress,
        )

    def session_presentation(self) -> SessionPresentation:
        """Build one immutable display projection of the visible session graph.

        ``top_level`` and ``children_of`` split sessions into a one-level tree: subagents
        (``parent_session_key`` set) are grouped under their parent. A subagent
        whose parent isn't visible is dropped rather than floated as a top-level
        card — a subagent only makes sense under its parent, and an orphaned one
        (parent aged out) would otherwise clutter the list with contextless
        cards. Order within each list follows ``visible_sessions``' sort, with
        parents promoted when descendant work makes them effectively active.
        """
        visible = self.visible_sessions()
        # Resolve recorded (raw-id) delegation parents against what's visible
        # now; last writer wins on the astronomically-unlikely UUID collision.
        key_by_session_id = {s.session_id: s.key for s in visible}
        parent_by_key = {
            session.key: self._parent_key(session, key_by_session_id)
            for session in visible
        }
        working_keys, through_subagents, effective_progress = self._effective_activity(
            visible, parent_by_key
        )
        top_keys = {key for key, parent in parent_by_key.items() if parent is None}
        display_by_key = {
            session.key: self._with_effective_activity(
                session, working_keys, effective_progress
            )
            for session in visible
        }
        top: list[Session] = []
        children: dict[str, list[Session]] = {}
        for s in visible:
            parent = parent_by_key[s.key]
            effective = display_by_key[s.key]
            if parent is None:
                top.append(effective)
            elif parent in top_keys:
                # A finished Task/Agent subagent (its own transcript went quiet,
                # so ``thinking`` aged out) is dead weight: it keeps padding the
                # parent's sub-agent count and nested rows with work that is over.
                # Nest it only while it is still alive. This is scoped to true
                # subagents (``parent_session_key`` set); a delegated worker child
                # (cross-provider link, no ``parent_session_key``) still nests when
                # idle, since an idle worker is not "dead".
                if s.parent_session_key is not None and s.key not in working_keys:
                    continue
                children.setdefault(parent, []).append(effective)
            # else: orphan subagent (parent not visible) — omit from the tree
        top.sort(key=self._sort_key)
        immutable_children = MappingProxyType(
            {parent: tuple(sessions) for parent, sessions in children.items()}
        )
        return SessionPresentation(
            visible=tuple(display_by_key[session.key] for session in visible),
            top_level=tuple(top),
            children_of=immutable_children,
            working_count=sum(session.thinking for session in top),
            _display_by_key=MappingProxyType(display_by_key),
            _working_through_subagents=frozenset(through_subagents),
        )

    # --- usage --------------------------------------------------------

    def set_host_stats(self, snapshot: HostStats) -> None:
        self.host_stats = snapshot
        self.usage_changed()

    def set_usage(self, snapshot: UsageSnapshot) -> None:
        self.usage[snapshot.account_key] = snapshot
        if self.db is not None:
            self.db.record_usage(snapshot)
        self.usage_changed()

    def warm_usage(self, snapshot: UsageSnapshot) -> None:
        """Seed a persisted snapshot at startup without re-recording it to
        history. ``setdefault`` so a live poll that already landed wins; a stale
        warm value only fills the gap until the first successful poll, and
        age-based staleness in the render marks it accordingly."""
        self.usage.setdefault(snapshot.account_key, snapshot)

    def mark_usage_stale(self, account_key: str) -> None:
        snap = self.usage.get(account_key)
        if snap is not None and not snap.stale:
            self.usage[account_key] = replace(snap, stale=True)
            self.usage_changed()
