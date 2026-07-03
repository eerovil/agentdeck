"""Server-Sent Events: the dashboard's live channel.

One connection carries two named events — ``usage`` and ``sessions`` — each
whose ``data`` is a fully-rendered HTML fragment that HTMX swaps into place
(``sse-swap``). Because every event is a whole-fragment replace, coalescing
bursts and dropping intermediates is always safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .deps import get_accounts, get_state, get_templates, require_access
from .render import render_limit_bars, render_session_list

router = APIRouter(dependencies=[Depends(require_access)])

HEARTBEAT_S = 15.0


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
