"""Request-scoped accessors + the auth headroom hook.

``require_access`` is a no-op in v0.x (the security model is "bind to a trusted
Tailscale/LAN interface"). Adding token or password auth later means filling in
this one dependency + a login page — no route signatures change.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..config import AppConfig
from ..models import Account
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
