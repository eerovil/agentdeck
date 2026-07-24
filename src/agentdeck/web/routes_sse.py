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

from ..models import Capability, SessionStatus, transcript_event_is_progress
from .deps import (
    get_accounts,
    get_injector,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .render import (
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
    resolve_activity_label,
)

router = APIRouter(dependencies=[Depends(require_access)])

HEARTBEAT_S = 15.0
# Tail cadence for the per-session stream. An open/active turn is tailed snappily;
# an idle session (no turn in progress) backs off to cut wakeups, disk reads, and —
# on a live PWA socket — battery, at the cost of up to TAIL_IDLE_INTERVAL_S latency
# before a fresh turn or incoming question shows on the watched page.
TAIL_INTERVAL_S = 1.5
TAIL_IDLE_INTERVAL_S = 4.0
# Brief recency fallback for ambiguous detail-page progress events.
THINKING_OFF_S = 3.0
# Re-render the usage bars at least this often so the "updated Nm ago" time
# ticks and the bars re-sync between the (slower) usage polls.
USAGE_REFRESH_S = 30.0


def _progress_age_s(event) -> float:
    """Wall-clock age of one progress event; untimestamped live events are new."""
    if event.ts is None:
        return 0.0
    timestamp = event.ts if event.ts.tzinfo is not None else event.ts.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - timestamp).total_seconds())


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


def format_sse(event: str, html: str, *, event_id: int | None = None) -> str:
    # SSE data may not contain raw newlines; prefix every line with "data: ".
    body = "".join(f"data: {line}\n" for line in html.splitlines()) or "data: \n"
    identity = f"id: {event_id}\n" if event_id is not None else ""
    return f"event: {event}\n{identity}{body}\n"


async def _stream(request: Request) -> AsyncIterator[str]:
    state = get_state(request)
    templates = get_templates(request)
    accounts = get_accounts(request)
    injector = get_injector(request)

    def render(topic: str, presentation=None) -> str:
        if topic == "usage":
            return format_sse("usage", render_limit_bars(templates, accounts, state))
        assert presentation is not None
        if topic == "assistant":
            return format_sse(
                "assistant",
                render_assistant(templates, request.app.state.assistant, presentation),
            )
        return format_sse(
            "sessions",
            render_session_list(
                templates,
                accounts,
                presentation,
                injector=injector,
                assistant=request.app.state.assistant,
            ),
        )

    loop = asyncio.get_event_loop()
    with state.bus.subscribe("usage", "sessions", "assistant") as sub:
        # Prime the client with current state on connect.
        presentation = state.session_presentation()
        yield render("usage")
        yield render("sessions", presentation)
        yield render("assistant", presentation)
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
            presentation = (
                state.session_presentation()
                if {"sessions", "assistant"} & dirty
                else None
            )
            for t in ("usage", "sessions", "assistant"):
                if t in dirty:
                    yield render(t, presentation)
            if "usage" in dirty:
                last_usage_push = loop.time()


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _session_stream(
    request: Request, session_key: str, *, after_seq: int | None = None
) -> AsyncIterator[str]:
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
    backfill = []
    if after_seq is not None:
        read_after = after_seq if seq >= after_seq else 0
        events = await provider.read_transcript(account, session, after_seq=read_after)
        backfill = [event for event in events if event.seq <= seq]
    last_ev = await provider.last_event(account, session)
    # Anchor the progress clock to normalized Turn Progress, so presentation
    # bookkeeping cannot postpone a real stall.
    progress_at = session.last_progress or session.last_activity
    init_age = (
        (datetime.now(UTC) - progress_at).total_seconds()
        if progress_at
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
        presentation = state.session_presentation()
        yield format_sse("usage", render_limit_bars(templates, accounts, state))
        yield format_sse(
            "sessions",
            render_session_list(
                templates,
                accounts,
                presentation,
                selected_session_key=session_key,
                injector=injector,
                assistant=request.app.state.assistant,
            ),
        )
        yield format_sse(
            "assistant",
            render_assistant(templates, request.app.state.assistant, presentation),
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
        if backfill:
            observed_messages = injector.observe_transcript(session_key, backfill)
            yield format_sse(
                "transcript",
                render_transcript_events(
                    templates,
                    backfill,
                    session_key=session_key,
                    observed_messages=observed_messages,
                ),
                event_id=backfill[-1].seq,
            )
            progress_events = [
                event for event in backfill if transcript_event_is_progress(event)
            ]
            if progress_events:
                last_ev = progress_events[-1]
                last_activity_t = loop.time() - _progress_age_s(last_ev)
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
                observed_messages = injector.observe_transcript(session_key, new_events)
                yield format_sse(
                    "transcript",
                    render_transcript_events(
                        templates,
                        new_events,
                        session_key=session_key,
                        observed_messages=observed_messages,
                    ),
                    event_id=new_events[-1].seq,
                )
                progress_events = [
                    event for event in new_events if transcript_event_is_progress(event)
                ]
                if progress_events:
                    last_ev = progress_events[-1]
                    last_activity_t = loop.time() - _progress_age_s(last_ev)
            current = state.sessions.get(session_key) or session
            presentation = state.session_presentation()
            active_child_sessions = presentation.active_child_sessions(current)
            live = current.status == SessionStatus.LIVE
            age = loop.time() - last_activity_t
            streaming = live and age < THINKING_OFF_S
            # "Busy" (pulsing dot + activity marker) tracks the open turn, not just
            # recent writes — so a long tool run or slow first token stays busy.
            label = resolve_activity_label(
                has_question=bool(current.question),
                live=live,
                streaming=streaming,
                last_event=last_ev,
                age_s=age,
                has_working_subagent=presentation.has_working_subagent(current),
                lifecycle_active=current.lifecycle_active,
            )
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
            subagents = (current.subagents, active_child_sessions)
            if subagents != last_subagents:
                last_subagents = subagents
                yield format_sse(
                    "subagents",
                    render_subagent_activity(
                        templates, current, active_child_sessions
                    ),
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
            interaction = provider.pending_interaction(account, current)
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
                        presentation,
                        selected_session_key=session_key,
                        injector=injector,
                        assistant=request.app.state.assistant,
                    ),
                )
            if "assistant" in sidebar_dirty:
                yield format_sse(
                    "assistant",
                    render_assistant(
                        templates, request.app.state.assistant, presentation
                    ),
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
async def session_events(
    request: Request, session_key: str, after_seq: int | None = None
) -> StreamingResponse:
    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            after_seq = int(last_event_id)
        except ValueError:
            pass
    return StreamingResponse(
        _session_stream(request, session_key, after_seq=after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
