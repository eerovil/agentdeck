"""Server-Sent Events: the dashboard's live channel.

One connection carries two named events — ``usage`` and ``sessions`` — each
whose ``data`` is a fully-rendered HTML fragment that HTMX swaps into place
(``sse-swap``). Because every event is a whole-fragment replace, coalescing
bursts and dropping intermediates is always safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..models import SessionStatus
from .deps import (
    get_accounts,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .render import (
    render_limit_bars,
    render_session_list,
    render_session_status,
    render_transcript_events,
)

router = APIRouter(dependencies=[Depends(require_access)])

HEARTBEAT_S = 15.0
TAIL_INTERVAL_S = 1.5
# Detail-page "thinking" turns off this long after the last transcript write.
THINKING_OFF_S = 3.0


def format_sse(event: str, html: str) -> str:
    # SSE data may not contain raw newlines; prefix every line with "data: ".
    body = "".join(f"data: {line}\n" for line in html.splitlines()) or "data: \n"
    return f"event: {event}\n{body}\n"


async def _stream(request: Request) -> AsyncIterator[str]:
    state = get_state(request)
    templates = get_templates(request)
    accounts = get_accounts(request)

    def render(topic: str) -> str:
        if topic == "usage":
            return format_sse("usage", render_limit_bars(templates, accounts, state))
        return format_sse("sessions", render_session_list(templates, accounts, state))

    with state.bus.subscribe("usage", "sessions") as sub:
        # Prime the client with current state on connect.
        yield render("usage")
        yield render("sessions")
        while True:
            if await request.is_disconnected():
                break
            try:
                topic, _ = await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            dirty = {topic}
            while (item := sub.get_nowait()) is not None:
                dirty.add(item[0])
            for t in ("usage", "sessions"):
                if t in dirty:
                    yield render(t)


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _session_stream(request: Request, session_key: str) -> AsyncIterator[str]:
    """Per-session live tail: appends new transcript events, and pushes a status
    fragment whenever the session flips LIVE↔IDLE *or* its thinking state
    changes. "Thinking" is driven directly off the tail here (transcript written
    within THINKING_OFF_S), so on the page you're watching it reacts within one
    poll instead of waiting for the ~10s sweep. Polls from a byte cursor, so an
    idle session costs almost nothing."""
    account, session, provider = resolve_session(request, session_key)
    templates = get_templates(request)
    state = get_state(request)
    loop = asyncio.get_event_loop()

    offset, seq = await provider.transcript_cursor(account, session)
    last_activity_t = loop.time() if session.thinking else -1e9
    last_status = None
    last_thinking = None
    while True:
        if await request.is_disconnected():
            break
        new_events, offset, seq = await provider.tail_transcript(account, session, offset, seq)
        if new_events:
            yield format_sse("transcript", render_transcript_events(templates, new_events))
            last_activity_t = loop.time()
        current = state.sessions.get(session_key) or session
        live = current.status == SessionStatus.LIVE
        thinking = live and (loop.time() - last_activity_t) < THINKING_OFF_S
        if current.status != last_status or thinking != last_thinking:
            last_status, last_thinking = current.status, thinking
            snap = replace(current, thinking=thinking)
            yield format_sse("status", render_session_status(templates, snap))
        await asyncio.sleep(TAIL_INTERVAL_S)


@router.get("/events/sessions/{session_key}")
async def session_events(request: Request, session_key: str) -> StreamingResponse:
    return StreamingResponse(
        _session_stream(request, session_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
