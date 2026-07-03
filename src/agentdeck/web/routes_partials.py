"""HTMX fragment routes. Each returns exactly what the SSE stream pushes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .deps import get_accounts, get_state, get_templates, require_access, resolve_session
from .render import render_limit_bars, render_session_list, render_transcript_events

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


@router.get("/sessions/{session_key}/transcript", response_class=HTMLResponse)
async def transcript_earlier(request: Request, session_key: str, before: int = 0) -> HTMLResponse:
    """ "Load earlier": the transcript window ending just before ``before``."""
    account, session, provider = resolve_session(request, session_key)
    detail = await provider.load_transcript(account, session, before_seq=before or None)
    templates = get_templates(request)
    # Button first (stays at the top of the transcript), then the earlier window.
    html = ""
    if detail.earliest_seq > 1:
        html = templates.get_template("partials/load_earlier.html").render(
            session=session, before=detail.earliest_seq
        )
    html += render_transcript_events(templates, detail.events)
    return HTMLResponse(html)
