"""Full-page routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .deps import (
    get_accounts,
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
    from .render import _usage_rows, session_labels

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "rows": _usage_rows(accounts, state),
            "sessions": state.visible_sessions(),
            "labels": session_labels(accounts),
        },
    )


@router.get(
    "/sessions/{session_key}", response_class=HTMLResponse, dependencies=[Depends(require_access)]
)
async def session_detail(request: Request, session_key: str) -> HTMLResponse:
    account, session, provider = resolve_session(request, session_key)
    templates = get_templates(request)
    detail = await provider.load_transcript(account, session)
    from .render import _usage_rows

    return templates.TemplateResponse(
        request,
        "session.html",
        {
            "session": session,
            "detail": detail,
            # topbar usage bars, rendered server-side so they paint immediately
            # (the per-session SSE stream then keeps them live over one socket).
            "rows": _usage_rows(get_accounts(request), get_state(request)),
        },
    )
