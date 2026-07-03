"""Request-scoped accessors + the auth headroom hook.

``require_access`` is a no-op in v0.x (the security model is "bind to a trusted
Tailscale/LAN interface"). Adding token or password auth later means filling in
this one dependency + a login page — no route signatures change.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..config import AppConfig
from ..models import Account, Session
from ..providers import PROVIDERS, SessionProvider
from ..state import AppState


async def require_access(request: Request) -> None:
    """Placeholder access gate. No-op in v0.x; the seam for future auth."""
    return None


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def get_config(request: Request) -> AppConfig:
    return request.app.state.config


def get_accounts(request: Request) -> list[Account]:
    return request.app.state.accounts


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_db(request: Request):
    return request.app.state.db


def resolve_session(request: Request, session_key: str) -> tuple[Account, Session, SessionProvider]:
    """Look up a session by key → (account, session, provider), or 404."""
    state = get_state(request)
    session = state.sessions.get(session_key)
    if session is None:
        raise HTTPException(status_code=404, detail="unknown session")
    account = next(
        (a for a in get_accounts(request) if a.key == session.account_key),
        None,
    )
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    provider = PROVIDERS[account.provider_id]
    return account, session, provider
