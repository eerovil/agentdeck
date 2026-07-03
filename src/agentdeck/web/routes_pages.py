"""Full-page routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..models import Capability
from .deps import (
    get_accounts,
    get_config,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    state = get_state(request)
    return JSONResponse(
        {
            "status": "ok",
            "accounts": len(get_accounts(request)),
            "sessions": len(state.sessions),
        }
    )


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_access)])
async def dashboard(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    groups = [
        {"account": acc, "sessions": state.sessions_for_account(acc.key)} for acc in accounts
    ]
    from .render import _usage_rows

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"rows": _usage_rows(accounts, state), "groups": groups},
    )


@router.get(
    "/sessions/{session_key}", response_class=HTMLResponse, dependencies=[Depends(require_access)]
)
async def session_detail(request: Request, session_key: str) -> HTMLResponse:
    account, session, provider = resolve_session(request, session_key)
    templates = get_templates(request)
    detail = await provider.load_transcript(account, session)
    can_inject = get_config(request).inject.enabled and Capability.INJECT in session.capabilities
    return templates.TemplateResponse(
        request,
        "session.html",
        {"session": session, "detail": detail, "can_inject": can_inject},
    )
