"""AppState-owned Session transitions: apply_session_changes coherence."""

from __future__ import annotations

from agentdeck.models import Session, SessionStatus
from agentdeck.state import AppState


def _session(
    sid: str, status: SessionStatus = SessionStatus.IDLE, *, is_delegated: bool = False
) -> Session:
    return Session(
        key=f"claude_code:main:{sid}",
        account_key="claude_code:main",
        session_id=sid,
        status=status,
        is_delegated=is_delegated,
    )


def test_named_invalidations_own_event_topics():
    state = AppState()

    with state.bus.subscribe("sessions", "usage", "assistant") as events:
        state.sessions_changed()
        state.usage_changed()
        state.assistant_changed()

    assert events.get_nowait() == ("sessions", None)
    assert events.get_nowait() == ("usage", None)
    assert events.get_nowait() == ("assistant", None)
    assert events.get_nowait() is None


def test_successful_session_scans_advance_an_account_revision():
    state = AppState()

    with state.bus.subscribe("session_scans") as events:
        state.sessions_scanned("codex:main")
        state.sessions_scanned("codex:main")

    assert state.session_scan_revision("codex:main") == 2
    assert state.session_scan_revision("claude_code:main") == 0
    assert events.get_nowait() == ("session_scans", "codex:main")
    assert events.get_nowait() == ("session_scans", "codex:main")
    assert events.get_nowait() is None


class _FakeDb:
    """Records the writes AppState admission performs, with empty loads."""

    def __init__(self) -> None:
        self.recorded: list[tuple[str, str | None]] = []
        self.upserted: list[list[Session]] = []

    def load_delegation_parents(self) -> dict[str, str]:
        return {}

    def load_delegated_sessions(self) -> set[str]:
        return set()

    def load_generated_titles(self) -> dict:
        return {}

    def record_delegated_session(self, key: str, parent: str | None = None) -> None:
        self.recorded.append((key, parent))

    def upsert_sessions_seen(self, sessions: list[Session]) -> None:
        self.upserted.append(list(sessions))


def test_apply_session_changes_publishes_once_and_returns_changed():
    state = AppState()
    session = _session("a")
    state.sessions[session.key] = session

    def project(s: Session) -> None:
        s.status = SessionStatus.LIVE
        s.thinking = True

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes([session], project)

    # Mutation lands on the read model's own object (same identity), and the
    # transition publishes exactly once.
    assert changed == [session]
    assert changed[0] is session
    assert session.status is SessionStatus.LIVE
    assert session.thinking is True
    assert events.get_nowait() == ("sessions", None)
    assert events.get_nowait() is None


def test_apply_session_changes_is_silent_on_noop():
    state = AppState()
    session = _session("a", SessionStatus.LIVE)
    state.sessions[session.key] = session

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes([session], lambda s: None)

    # No field changed → nothing to announce.
    assert changed == []
    assert events.get_nowait() is None


def test_apply_session_changes_ignores_untracked_field_free_projection():
    # A projection that writes the same value it read is not a change: detection
    # is whole-object, so there is no per-field list to keep in sync.
    state = AppState()
    session = _session("a", SessionStatus.LIVE)
    state.sessions[session.key] = session

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes(
            [session], lambda s: setattr(s, "status", SessionStatus.LIVE)
        )

    assert changed == []
    assert events.get_nowait() is None


def test_apply_session_changes_batches_a_single_publish():
    state = AppState()
    a, b, c = _session("a"), _session("b"), _session("c")
    for s in (a, b, c):
        state.sessions[s.key] = s

    def project(s: Session) -> None:
        if s.session_id in {"a", "c"}:
            s.status = SessionStatus.LIVE

    with state.bus.subscribe("sessions") as events:
        changed = state.apply_session_changes([a, b, c], project)

    # Only the two that actually changed are reported, and the whole batch
    # publishes once rather than per changed session.
    assert changed == [a, c]
    assert b.status is SessionStatus.IDLE
    assert events.get_nowait() == ("sessions", None)
    assert events.get_nowait() is None


# --- admission (_admit): the delegation-learn + stamp rule ---------------


def test_replace_learns_and_persists_a_self_flagged_delegation():
    db = _FakeDb()
    state = AppState(db=db)

    state.replace_account_sessions("claude_code:main", [_session("a", is_delegated=True)])

    assert "claude_code:main:a" in state.delegated_session_keys
    assert db.recorded == [("claude_code:main:a", None)]
    assert state.sessions["claude_code:main:a"].is_delegated is True
    # The DB upsert receives every admitted session, not just changed keys.
    assert [s.key for s in db.upserted[-1]] == ["claude_code:main:a"]


def test_replace_stamps_a_persisted_key_arriving_unflagged():
    db = _FakeDb()
    state = AppState(db=db)
    state.delegated_session_keys.add("claude_code:main:a")

    # Session arrives without its own delegation flag; admission stamps it.
    state.replace_account_sessions("claude_code:main", [_session("a", is_delegated=False)])

    assert state.sessions["claude_code:main:a"].is_delegated is True
    # A pre-known key is not re-persisted.
    assert db.recorded == []


def test_update_session_learns_and_persists_a_self_flagged_delegation():
    db = _FakeDb()
    state = AppState(db=db)

    with state.bus.subscribe("sessions") as events:
        state.update_session(_session("a", is_delegated=True))

    assert "claude_code:main:a" in state.delegated_session_keys
    assert db.recorded == [("claude_code:main:a", None)]
    assert state.sessions["claude_code:main:a"].is_delegated is True
    assert events.get_nowait() == ("sessions", None)


def test_admission_tracks_in_memory_without_a_db():
    # NullDb path: admission still learns the delegation in memory and stamps it,
    # without touching any persistence.
    state = AppState()  # db=None
    state.replace_account_sessions("claude_code:main", [_session("a", is_delegated=True)])
    assert "claude_code:main:a" in state.delegated_session_keys
    assert state.sessions["claude_code:main:a"].is_delegated is True


def test_mark_delegated_session_is_unconditional_and_separate_from_admission():
    # mark_delegated_session flags an un-self-flagged, already-stored session and
    # does NOT route through _admit's conditional-learn path.
    db = _FakeDb()
    state = AppState(db=db)
    stored = _session("a", is_delegated=False)
    state.sessions[stored.key] = stored

    state.mark_delegated_session(stored.key, "parent-raw-id")

    assert state.sessions[stored.key].is_delegated is True
    assert state.recorded_delegation_parents[stored.key] == "parent-raw-id"
    assert db.recorded == [("claude_code:main:a", "parent-raw-id")]
