"""HTMX fragment routes. Each returns exactly what the SSE stream pushes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .deps import get_accounts, get_state, get_templates, require_access
from .render import render_limit_bars, render_session_list

router = APIRouter(prefix="/partials", dependencies=[Depends(require_access)])


@router.get("/limit-bars", response_class=HTMLResponse)
async def limit_bars(request: Request) -> HTMLResponse:
    return HTMLResponse(
        render_limit_bars(get_templates(request), get_accounts(request), get_state(request))
    )


@router.get("/sessions", response_class=HTMLResponse)
async def sessions(request: Request) -> HTMLResponse:
    return HTMLResponse(
        render_session_list(get_templates(request), get_accounts(request), get_state(request))
    )
