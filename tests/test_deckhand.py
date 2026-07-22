"""Unit tests for shared Deckhand account + ordering policy."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agentdeck.deckhand import deckhand_account, most_recent_first
from agentdeck.models import Account, Session, SessionStatus


def _account(key: str, provider_id: str) -> Account:
    return Account(key=key, provider_id=provider_id, label=key.split(":")[-1], root=Path("/tmp"))


def _session(*, last_activity=None, started_at=None) -> Session:
    return Session(
        key="codex:main:s1",
        account_key="codex:main",
        session_id="s1",
        status=SessionStatus.IDLE,
        last_activity=last_activity,
        started_at=started_at,
    )


CODEX_A = _account("codex:main", "codex")
CODEX_B = _account("codex:alt", "codex")
CLAUDE = _account("claude_code:main", "claude_code")


def test_deckhand_account_prefers_configured_key():
    assert deckhand_account([CLAUDE, CODEX_A, CODEX_B], "codex:alt") is CODEX_B


def test_deckhand_account_configured_key_must_be_codex():
    # A configured key that is not a codex account resolves to None, never a
    # silent fallback to a different account.
    assert deckhand_account([CLAUDE, CODEX_A], "claude_code:main") is None
    assert deckhand_account([CODEX_A], "codex:missing") is None


def test_deckhand_account_falls_back_to_first_codex():
    assert deckhand_account([CLAUDE, CODEX_A, CODEX_B], None) is CODEX_A


def test_deckhand_account_none_when_no_codex():
    assert deckhand_account([CLAUDE], None) is None
    assert deckhand_account([], None) is None


def test_most_recent_first_prefers_last_activity_then_started_at():
    older = datetime(2020, 1, 1, tzinfo=UTC)
    newer = datetime(2024, 1, 1, tzinfo=UTC)
    by_activity = _session(last_activity=newer, started_at=older)
    by_started = _session(last_activity=None, started_at=older)
    none = _session()
    # Newest activity sorts first (smallest key), a session with neither stamp last.
    ordered = sorted([none, by_started, by_activity], key=most_recent_first)
    assert ordered == [by_activity, by_started, none]
    assert most_recent_first(none) == 0.0
    assert most_recent_first(by_activity) == -newer.timestamp()
