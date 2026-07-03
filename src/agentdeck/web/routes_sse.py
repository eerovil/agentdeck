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
    activity_label,
    render_limit_bars,
    render_session_list,
    render_session_status,
    render_tool_activity,
    render_transcript_events,
)

router = APIRouter(dependencies=[Depends(require_access)])

HEARTBEAT_S = 15.0
TAIL_INTERVAL_S = 1.5
# Detail-page "thinking" turns off this long after the last transcript write.
THINKING_OFF_S = 3.0
# Re-render the usage bars at least this often so the "updated Nm ago" time
# ticks and the bars re-sync between the (slower) usage polls.
USAGE_REFRESH_S = 30.0


def _usage_sig(accounts, state) -> tuple:
    """Cheap change-detector for the usage snapshots (keyed by fetch time), so
    the session stream re-renders the topbar only when a poll lands."""
    out = []
    for a in accounts:
        snap = state.usage.get(a.key)
        out.append((a.key, snap.fetched_at if snap else None))
    return tuple(out)


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

    loop = asyncio.get_event_loop()
    with state.bus.subscribe("usage", "sessions") as sub:
        # Prime the client with current state on connect.
        yield render("usage")
        yield render("sessions")
        last_usage_push = loop.time()
        while True:
            if await request.is_disconnected():
                break
            try:
                topic, _ = await asyncio.wait_for(sub.get(), timeout=HEARTBEAT_S)
                dirty = {topic}
                while (item := sub.get_nowait()) is not None:
                    dirty.add(item[0])
            except TimeoutError:
                dirty = set()
            # Refresh usage on a fixed cadence so the "updated" time keeps ticking
            # even while sessions churn (which would otherwise starve the timeout).
            if loop.time() - last_usage_push >= USAGE_REFRESH_S:
                dirty.add("usage")
            if not dirty:
                yield ": ping\n\n"
                continue
            for t in ("usage", "sessions"):
                if t in dirty:
                    yield render(t)
            if "usage" in dirty:
                last_usage_push = loop.time()


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
    accounts = get_accounts(request)
    loop = asyncio.get_event_loop()

    offset, seq = await provider.transcript_cursor(account, session)
    last_ev = await provider.last_event(account, session)
    last_activity_t = loop.time() if session.thinking else -1e9
    last_status = None
    last_busy = None
    last_label = None
    # The topbar usage bars ride this same stream (see session.html), so the
    # session page holds one socket, not two. Re-push only when a snapshot
    # actually changes (~every usage poll), not every tail.
    yield format_sse("usage", render_limit_bars(templates, accounts, state))
    last_usage_sig = _usage_sig(accounts, state)
    last_usage_push = loop.time()
    while True:
        if await request.is_disconnected():
            break
        new_events, offset, seq = await provider.tail_transcript(account, session, offset, seq)
        if new_events:
            yield format_sse("transcript", render_transcript_events(templates, new_events))
            last_activity_t = loop.time()
            last_ev = new_events[-1]
        current = state.sessions.get(session_key) or session
        live = current.status == SessionStatus.LIVE
        streaming = live and (loop.time() - last_activity_t) < THINKING_OFF_S
        # "Busy" (pulsing dot + activity marker) tracks the open turn, not just
        # recent writes — so a long tool run or slow first token stays busy.
        label = activity_label(live, streaming, last_ev)
        busy = label is not None
        if current.status != last_status or busy != last_busy:
            last_status, last_busy = current.status, busy
            snap = replace(current, thinking=busy)
            yield format_sse("status", render_session_status(templates, snap))
        if label != last_label:
            last_label = label
            yield format_sse("tools", render_tool_activity(templates, label))
        sig = _usage_sig(accounts, state)
        # push on a new snapshot, or on the fixed cadence so "updated" keeps ticking
        if sig != last_usage_sig or (loop.time() - last_usage_push) >= USAGE_REFRESH_S:
            last_usage_sig = sig
            last_usage_push = loop.time()
            yield format_sse("usage", render_limit_bars(templates, accounts, state))
        await asyncio.sleep(TAIL_INTERVAL_S)


@router.get("/events/sessions/{session_key}")
async def session_events(request: Request, session_key: str) -> StreamingResponse:
    return StreamingResponse(
        _session_stream(request, session_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
