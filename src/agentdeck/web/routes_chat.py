"""Interactive chat routes (v0.3). Gated by the same [inject] kill-switch — a
chat child is a write path into a session, just a long-lived one."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ..providers.claude_code.chat import ChatRefused
from .deps import (
    get_chat_manager,
    get_config,
    get_templates,
    require_access,
    resolve_session,
)
from .render import render_chat_event
from .routes_sse import HEARTBEAT_S, format_sse

router = APIRouter(dependencies=[Depends(require_access)])


def _require_enabled(request: Request) -> None:
    if not get_config(request).inject.enabled:
        raise HTTPException(status_code=403, detail="interactive chat is disabled")


@router.get("/sessions/{session_key}/chat", response_class=HTMLResponse)
async def chat_page(request: Request, session_key: str) -> HTMLResponse:
    _require_enabled(request)
    account, session, provider = resolve_session(request, session_key)
    manager = get_chat_manager(request)
    templates = get_templates(request)
    error = None
    try:
        await manager.get_or_open(session_key, account, session, provider)
    except ChatRefused as exc:
        error = str(exc)
    return templates.TemplateResponse(
        request, "chat.html", {"session": session, "error": error}
    )


@router.post("/sessions/{session_key}/chat/send", response_class=HTMLResponse)
async def chat_send(request: Request, session_key: str, message: str = Form(...)) -> HTMLResponse:
    _require_enabled(request)
    manager = get_chat_manager(request)
    cs = manager.get(session_key)
    if cs is None or cs.closed:
        raise HTTPException(status_code=409, detail="chat is not open")
    manager.touch(session_key)
    await cs.send(message)
    return HTMLResponse("")  # the echoed bubble arrives over SSE


@router.post("/sessions/{session_key}/chat/stop", response_class=HTMLResponse)
async def chat_stop(request: Request, session_key: str) -> HTMLResponse:
    manager = get_chat_manager(request)
    await manager.stop(session_key)
    return HTMLResponse('<div class="chat-closed">chat stopped</div>')


async def _chat_stream(request: Request, session_key: str) -> AsyncIterator[str]:
    manager = get_chat_manager(request)
    templates = get_templates(request)
    cs = manager.get(session_key)
    if cs is None:
        yield format_sse("chat", '<div class="chat-closed">chat is not open</div>')
        return
    agen = cs.stream(0)
    while True:
        if await request.is_disconnected():
            break
        try:
            _, event = await asyncio.wait_for(agen.__anext__(), timeout=HEARTBEAT_S)
        except TimeoutError:
            yield ": ping\n\n"
            continue
        except StopAsyncIteration:
            break
        manager.touch(session_key)
        html = render_chat_event(templates, event)
        if html:
            yield format_sse("chat", html)


@router.get("/events/sessions/{session_key}/chat")
async def chat_events(request: Request, session_key: str) -> StreamingResponse:
    return StreamingResponse(
        _chat_stream(request, session_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
