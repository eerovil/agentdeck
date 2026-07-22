"""Shared Deckhand policy: how the two Deckhand services agree.

``AssistantService`` (attention triage) and ``TitleService`` both run on
``config.assistant`` and ``config.build_accounts()`` — identical inputs — so the
decisions they share must resolve identically. Keeping them here stops the two
services silently diverging (e.g. generating titles on a different Codex account
than the one that triages attention, or ordering their working sets differently).
"""

from __future__ import annotations

from .models import Account, Session


def deckhand_account(accounts: list[Account], account_key: str | None) -> Account | None:
    """The Codex account Deckhand's LLM calls run on.

    Prefer the explicitly configured ``account_key`` when set (returning None if
    that key is not a codex account); otherwise fall back to the first codex
    account, or None when no codex account is configured.
    """
    codex = [account for account in accounts if account.provider_id == "codex"]
    if account_key:
        return next((account for account in codex if account.key == account_key), None)
    return codex[0] if codex else None


def most_recent_first(session: Session) -> float:
    """Sort key ordering sessions most-recently-active first.

    Uses ``last_activity`` when known, else ``started_at``; a session with
    neither timestamp sorts last. Negated so ascending ``sort`` puts the newest
    activity at the front.
    """
    stamp = session.last_activity or session.started_at
    return -(stamp.timestamp() if stamp else 0.0)
