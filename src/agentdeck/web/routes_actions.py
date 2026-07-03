"""Write actions: message injection (v0.3). Guarded by the [inject] kill-switch
and, per request, by the provider's spawn-time safety interlocks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from .deps import get_config, get_templates, require_access, resolve_session

router = APIRouter(dependencies=[Depends(require_access)])


@router.post("/sessions/{session_key}/inject", response_class=HTMLResponse)
async def inject(request: Request, session_key: str, message: str = Form(...)) -> HTMLResponse:
    config = get_config(request)
    if not config.inject.enabled:
        raise HTTPException(status_code=403, detail="message injection is disabled")
    account, session, provider = resolve_session(request, session_key)
    result = await provider.inject(account, session, message, timeout_s=config.inject.timeout_s)
    templates = get_templates(request)
    return HTMLResponse(templates.get_template("partials/inject_result.html").render(result=result))
