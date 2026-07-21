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
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..models import Capability, SessionStatus, detailed_activity_label
from .deps import (
    get_accounts,
    get_injector,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .render import (
    activity_label,
    render_assistant,
    render_assistant_session,
    render_composer_controls,
    render_limit_bars,
    render_pending_interaction,
    render_session_done,
    render_session_list,
    render_session_status,
    render_subagent_activity,
    render_tool_activity,
    render_transcript_events,
)

router = APIRouter(dependencies=[Depends(require_access)])

HEARTBEAT_S = 15.0
# Tail cadence for the per-session stream. An open/active turn is tailed snappily;
# an idle session (no turn in progress) backs off to cut wakeups, disk reads, and —
# on a live PWA socket — battery, at the cost of up to TAIL_IDLE_INTERVAL_S latency
# before a fresh turn or incoming question shows on the watched page.
TAIL_INTERVAL_S = 1.5
TAIL_IDLE_INTERVAL_S = 4.0
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
    host = state.host_stats
    out.append(("host", host.sampled_at if host else None))
    return tuple(out)


def format_sse(event: str, html: str) -> str:
    # SSE data may not contain raw newlines; prefix every line with "data: ".
    body = "".join(f"data: {line}\n" for line in html.splitlines()) or "data: \n"
    return f"event: {event}\n{body}\n"


async def _stream(request: Request) -> AsyncIterator[str]:
    state = get_state(request)
    templates = get_templates(request)
    accounts = get_accounts(request)
    injector = get_injector(request)

    def render(topic: str) -> str:
        if topic == "usage":
            return format_sse("usage", render_limit_bars(templates, accounts, state))
        if topic == "assistant":
            return format_sse(
                "assistant",
                render_assistant(templates, request.app.state.assistant, state),
            )
        return format_sse(
            "sessions",
            render_session_list(
                templates,
                accounts,
                state,
                injector=injector,
                assistant=request.app.state.assistant,
            ),
        )

    loop = asyncio.get_event_loop()
    with state.bus.subscribe("usage", "sessions", "assistant") as sub:
        # Prime the client with current state on connect.
        yield render("usage")
        yield render("sessions")
        yield render("assistant")
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
            # A Deckhand verdict change moves the per-session pills, so re-render
            # the list whenever the assistant view does.
            if "assistant" in dirty:
                dirty.add("sessions")
            if not dirty:
                yield ": ping\n\n"
                continue
            for t in ("usage", "sessions", "assistant"):
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
    injector = get_injector(request)
    loop = asyncio.get_event_loop()

    offset, seq = await provider.transcript_cursor(account, session)
    last_ev = await provider.last_event(account, session)
    # Anchor the write-clock to the transcript's real age, so a page opened
    # mid-turn starts from the true last-write time (not "never wrote").
    init_age = (
        (datetime.now(UTC) - session.last_activity).total_seconds()
        if session.last_activity
        else 1e9
    )
    last_activity_t = loop.time() - init_age
    last_status = None
    last_busy = None
    last_subagent_count = None
    last_subagents = None
    last_label = None
    last_can_interrupt = None
    # The topbar, Deckhand, and desktop session list ride this same stream (see
    # session.html), so the page still holds one socket. Each fragment is
    # re-pushed only when its underlying state changes.
    with state.bus.subscribe("sessions", "assistant") as sidebar_sub:
        yield format_sse("usage", render_limit_bars(templates, accounts, state))
        yield format_sse(
            "sessions",
            render_session_list(
                templates,
                accounts,
                state,
                selected_session_key=session_key,
                injector=injector,
                assistant=request.app.state.assistant,
            ),
        )
        yield format_sse(
            "assistant",
            render_assistant(templates, request.app.state.assistant, state),
        )
        yield format_sse(
            "assistant-session",
            render_assistant_session(
                templates, request.app.state.assistant, session_key
            ),
        )
        yield format_sse(
            "assistant-session-done",
            render_session_done(templates, request.app.state.assistant, session_key),
        )
        # The pending-interaction widget is server-rendered on page load and kept
        # live here — pushed ONLY when the interaction actually changes (a new
        # question, or the current one answered/cancelled). Never re-pushing an
        # unchanged interaction is what lets the user select radios/checkboxes and
        # type an answer without a refresh wiping it mid-interaction. Seed from the
        # already-rendered state so we don't clobber it on connect/reconnect.
        _pending = provider.pending_interaction(account, session)
        last_interaction_id = _pending.id if _pending is not None else None
        last_usage_sig = _usage_sig(accounts, state)
        last_usage_push = loop.time()
        while True:
            if await request.is_disconnected():
                break
            new_events, offset, seq = await provider.tail_transcript(
                account, session, offset, seq
            )
            if new_events:
                yield format_sse("transcript", render_transcript_events(templates, new_events))
                last_activity_t = loop.time()
                last_ev = new_events[-1]
            current = state.sessions.get(session_key) or session
            live = current.status == SessionStatus.LIVE
            age = loop.time() - last_activity_t
            streaming = live and age < THINKING_OFF_S
            # "Busy" (pulsing dot + activity marker) tracks the open turn, not just
            # recent writes — so a long tool run or slow first token stays busy.
            label = None if current.question else activity_label(live, streaming, last_ev, age)
            label = detailed_activity_label(label, last_ev)
            if label is None and state.has_working_subagent(current):
                label = "Working"
            busy = label is not None
            if (
                current.status != last_status
                or busy != last_busy
                or current.subagent_count != last_subagent_count
            ):
                last_status, last_busy = current.status, busy
                last_subagent_count = current.subagent_count
                snap = replace(current, thinking=busy)
                yield format_sse("status", render_session_status(templates, snap))
            if current.subagents != last_subagents:
                last_subagents = current.subagents
                yield format_sse(
                    "subagents", render_subagent_activity(templates, current)
                )
            can_interrupt = Capability.INTERRUPT in current.capabilities
            if can_interrupt != last_can_interrupt:
                last_can_interrupt = can_interrupt
                yield format_sse(
                    "composer-controls", render_composer_controls(templates, current)
                )
            if label != last_label:
                last_label = label
                yield format_sse("tools", render_tool_activity(templates, label, age))
            interaction = provider.pending_interaction(account, session)
            interaction_id = interaction.id if interaction is not None else None
            if interaction_id != last_interaction_id:
                last_interaction_id = interaction_id
                yield format_sse(
                    "interaction",
                    render_pending_interaction(templates, session_key, interaction),
                )
            sig = _usage_sig(accounts, state)
            # push on a new snapshot, or on the fixed cadence so "updated" keeps ticking
            if sig != last_usage_sig or (loop.time() - last_usage_push) >= USAGE_REFRESH_S:
                last_usage_sig = sig
                last_usage_push = loop.time()
                yield format_sse("usage", render_limit_bars(templates, accounts, state))
            sidebar_dirty = set()
            while (item := sidebar_sub.get_nowait()) is not None:
                sidebar_dirty.add(item[0])
            if "sessions" in sidebar_dirty or "assistant" in sidebar_dirty:
                yield format_sse(
                    "sessions",
                    render_session_list(
                        templates,
                        accounts,
                        state,
                        selected_session_key=session_key,
                        injector=injector,
                        assistant=request.app.state.assistant,
                    ),
                )
            if "assistant" in sidebar_dirty:
                yield format_sse(
                    "assistant",
                    render_assistant(templates, request.app.state.assistant, state),
                )
                yield format_sse(
                    "assistant-session",
                    render_assistant_session(
                        templates, request.app.state.assistant, session_key
                    ),
                )
                yield format_sse(
                    "assistant-session-done",
                    render_session_done(
                        templates, request.app.state.assistant, session_key
                    ),
                )
            # Snappy while a turn is open/active; back off when idle to save
            # battery (see TAIL_IDLE_INTERVAL_S). `busy` tracks the open turn and
            # `live` the LIVE status, so a starting turn is caught within one idle
            # tick, then this tightens automatically.
            await asyncio.sleep(TAIL_INTERVAL_S if (live or busy) else TAIL_IDLE_INTERVAL_S)


@router.get("/events/sessions/{session_key}")
async def session_events(request: Request, session_key: str) -> StreamingResponse:
    return StreamingResponse(
        _session_stream(request, session_key),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
