"""AppState-owned Session transitions: apply_session_changes coherence."""

from __future__ import annotations

from agentdeck.models import Session, SessionStatus
from agentdeck.state import AppState


def _session(sid: str, status: SessionStatus = SessionStatus.IDLE) -> Session:
    return Session(
        key=f"claude_code:main:{sid}",
        account_key="claude_code:main",
        session_id=sid,
        status=status,
    )


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
