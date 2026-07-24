"""Shared fragment rendering — used by both the HTMX partial routes and the
SSE stream, so a partial fetched over HTTP and one pushed over SSE are byte
-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from ..inject import InjectionService
from ..models import Account, Capability, TranscriptEvent, detailed_activity_label
from ..models import activity_label as activity_label  # single impl lives in models
from ..state import AppState, SessionPresentation


def _now() -> datetime:
    return datetime.now(UTC)


def resolve_activity_label(
    *,
    has_question: bool,
    live: bool,
    streaming: bool,
    last_event: TranscriptEvent | None,
    age_s: float,
    has_working_subagent: bool,
    lifecycle_active: bool | None = None,
) -> str | None:
    """The session's activity badge text, or None when it should show nothing.

    One home for the pipeline the initial page render and the live SSE tail must
    agree on: a surfaced question suppresses the badge, then the open-turn label
    is derived and refined, and a still-blank label finally falls back to
    "Working" when a nested subagent is active. Each caller supplies its own
    ``streaming``/``age_s`` because they read different clocks (the sweep's
    ``thinking`` flag at page load, the SSE write-clock while tailing); only the
    combination logic is shared, so the two paths cannot drift apart.
    """
    if has_question:
        return None
    label = detailed_activity_label(
        activity_label(
            live,
            streaming,
            last_event,
            age_s,
            lifecycle_active=lifecycle_active,
        ),
        last_event,
    )
    if label is None and has_working_subagent:
        return "Working"
    return label


def reltime(value: datetime | None) -> str:
    """Human 'resets in 2h 10m' / 'reset' rendering; '' when unknown."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = (value - _now()).total_seconds()
    if delta <= 0:
        return "now"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def reltime_ago(value: datetime | None) -> str:
    """Human 'past' rendering: '2h 10m ago' / 'just now'; '' when unknown."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = (_now() - value).total_seconds()
    if delta < 60:
        return "just now"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def chat_time(value: datetime | None) -> str:
    """Compact local timestamp for transcript rows."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    local = value.astimezone()
    now = _now().astimezone()
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%b %d, %H:%M")
    return local.strftime("%b %d, %Y %H:%M")


def chat_time_title(value: datetime | None) -> str:
    """Full local timestamp exposed by transcript time elements."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def ktok(value) -> str:
    """Compact token count: 940 -> '940', 1200 -> '1.2k', 47000 -> '47k'."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n < 1000:
        return str(n)
    k = n / 1000
    return f"{k:.1f}k" if k < 10 else f"{k:.0f}k"


# Context-size colour thresholds, referenced to the 1M-token (beta) window most
# sessions run on: amber past the halfway mark, red in the near-full zone where
# auto-compaction bites.
CTX_WARN_TOKENS = 500_000
CTX_CRIT_TOKENS = 800_000


def ctx_level(value) -> str:
    """Traffic-light class for a context size: '' (green) / 'warn' / 'crit' —
    same modifier vocabulary as the usage bars."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n >= CTX_CRIT_TOKENS:
        return "crit"
    if n >= CTX_WARN_TOKENS:
        return "warn"
    return ""


def register_filters(templates: Jinja2Templates) -> None:
    templates.env.filters["reltime"] = reltime
    templates.env.filters["reltime_ago"] = reltime_ago
    templates.env.filters["chat_time"] = chat_time
    templates.env.filters["chat_time_title"] = chat_time_title
    templates.env.filters["ktok"] = ktok
    templates.env.filters["ctx_level"] = ctx_level
    templates.env.filters["tool_label"] = tool_label


def tool_label(value: str | None) -> str:
    """Short, readable names for provider-native tool identifiers."""
    name = (value or "tool").rsplit("__", 1)[-1].replace("_", " ").strip()
    return {
        "exec": "Shell",
        "exec command": "Shell",
        "apply patch": "Edit files",
        "wait": "Wait",
        "write stdin": "Command input",
        "view image": "View image",
    }.get(name.casefold(), name.title())


def _is_stale(snap, stale_after_s: float) -> bool:
    """Age-based staleness (issue #6): the displayed numbers are old, not just
    that the last poll attempt was rate-limited. Fresh data stays un-stale even
    when the endpoint is 429-ing."""
    if snap is None:
        return False
    return (_now() - snap.fetched_at).total_seconds() > stale_after_s


def _usage_rows(accounts: list[Account], state: AppState) -> list[dict]:
    stale_after = state.usage_stale_after_s
    rows = []
    for acc in accounts:
        snap = state.usage.get(acc.key)
        rows.append(
            {
                "account": acc,
                "usage": snap,
                "stale": _is_stale(snap, stale_after),
                "stale_age": reltime_age(snap),
                # epoch seconds of the fetch + the stale threshold, so the client
                # ticks "updated Ns ago" and flips the stale badge every second
                # without waiting on a server re-render.
                "fetched_epoch": (snap.fetched_at.timestamp() if snap else ""),
                "stale_after": stale_after,
            }
        )
    return rows


def reltime_age(snap) -> str:
    if snap is None:
        return ""
    return _fmt_age((_now() - snap.fetched_at).total_seconds())


def _fmt_age(delta: float) -> str:
    """Seconds-granular 'X ago' — matches the client-side ticker in base.html."""
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    mins = int(delta // 60)
    if mins < 60:
        return f"{mins}m ago"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def render_limit_bars(templates: Jinja2Templates, accounts: list[Account], state: AppState) -> str:
    return templates.get_template("partials/limit_bars.html").render(
        rows=_usage_rows(accounts, state), host=state.host_stats
    )


def render_assistant(
    templates: Jinja2Templates, assistant, presentation: SessionPresentation
) -> str:
    return templates.get_template("partials/assistant_panel.html").render(
        assistant=assistant,
        assistant_session_titles=assistant_session_titles(assistant),
        working_count=presentation.working_count,
    )


def assistant_session_titles(assistant) -> dict[str, str]:
    """Current canonical display title for each Deckhand-visible session."""
    return {
        key: session.display_title for key, session in assistant.state.sessions.items()
    }


def assistant_insights_for_session(assistant, session_key: str) -> tuple:
    return tuple(
        insight for insight in assistant.view.insights if insight.session_key == session_key
    )


def render_assistant_session(templates: Jinja2Templates, assistant, session_key: str) -> str:
    return templates.get_template("partials/assistant_session_details.html").render(
        assistant_insights=assistant_insights_for_session(assistant, session_key),
        assistant_handled=assistant.handled_insight(session_key),
        assistant_session_title=assistant_session_titles(assistant).get(session_key),
        git_context=assistant.contexts.get(session_key),
    )


def render_session_done(templates: Jinja2Templates, assistant, session_key: str) -> str:
    """The bottom-of-transcript Deckhand done/undo control. Shares the same state
    as ``render_assistant_session`` so both surfaces toggle together over SSE."""
    session = assistant.state.sessions.get(session_key)
    return templates.get_template("partials/session_done_button.html").render(
        assistant_insights=assistant_insights_for_session(assistant, session_key),
        session_handled=assistant.is_handled(session_key),
        session_waiting=session is not None and session.is_waiting,
        session_key=session_key,
    )


def session_labels(accounts: list[Account]) -> dict[str, str]:
    return {acc.key: acc.label for acc in accounts}


def session_queue_summaries(sessions, injector: InjectionService) -> dict[str, dict]:
    """Pending turn summaries rendered on cards after leaving a chat page."""
    summaries = {}
    for session in sessions:
        status = injector.status(session.key)
        if status is None:
            continue
        pending = [item for item in status.items if item.is_pending]
        if pending:
            summaries[session.key] = {
                "count": len(pending),
                "text": pending[-1].text,
            }
    return summaries


def pending_injection_messages(status) -> list:
    """App-level turns not yet linked to a durable transcript event."""
    if status is None:
        return []
    return [item for item in status.items if item.is_pending]


def session_list_context(
    accounts: list[Account],
    presentation: SessionPresentation,
    *,
    injector: InjectionService,
    assistant,
    selected_session_key: str | None = None,
) -> dict[str, object]:
    """Canonical template context for the session list and its surrounding shell."""
    return {
        "sessions": presentation.top_level,
        "children_of": presentation.children_of,
        "labels": session_labels(accounts),
        "selected_session_key": selected_session_key,
        "deckhand_status": assistant.deckhand_statuses(presentation.visible),
        "queue_summaries": session_queue_summaries(presentation.visible, injector),
        "assistant": assistant,
        "assistant_sessions": assistant.state.sessions,
        "assistant_session_titles": assistant_session_titles(assistant),
        "working_count": presentation.working_count,
    }


def render_session_list(
    templates: Jinja2Templates,
    accounts: list[Account],
    presentation: SessionPresentation,
    *,
    selected_session_key: str | None = None,
    injector: InjectionService,
    assistant,
) -> str:
    return templates.get_template("partials/session_list.html").render(
        **session_list_context(
            accounts,
            presentation,
            injector=injector,
            assistant=assistant,
            selected_session_key=selected_session_key,
        )
    )


def render_transcript_events(
    templates: Jinja2Templates,
    events,
    *,
    session_key: str | None = None,
    observed_messages: dict | None = None,
    pinned_seqs: set[int] | None = None,
) -> str:
    tmpl = templates.get_template("partials/transcript_event.html")
    observed_messages = observed_messages or {}
    pinned_seqs = pinned_seqs or set()
    return "".join(
        tmpl.render(
            e=e,
            session_key=session_key,
            observed_message=observed_messages.get(e.seq),
            is_pinned=e.seq in pinned_seqs,
        )
        for e in events
    )


def render_pinned_messages(templates: Jinja2Templates, pins) -> str:
    """Render only the stable ``<details>`` contents used by page load and SSE."""
    return templates.get_template("partials/pinned_messages.html").render(pins=pins)


def render_session_status(templates: Jinja2Templates, session) -> str:
    return templates.get_template("partials/session_status.html").render(s=session)


def render_subagent_activity(
    templates: Jinja2Templates, session, active_child_sessions=()
) -> str:
    return templates.get_template("partials/subagent_activity.html").render(
        s=session,
        active_subagent_sessions=active_child_sessions,
    )


def render_composer_controls(templates: Jinja2Templates, session) -> str:
    return templates.get_template("partials/composer_controls.html").render(
        session_key=session.key,
        can_interrupt=Capability.INTERRUPT in session.capabilities,
    )


def render_tool_activity(
    templates: Jinja2Templates, label: str | None, elapsed_s: float = 0.0
) -> str:
    return templates.get_template("partials/tool_activity.html").render(
        label=label, elapsed_s=max(0, int(elapsed_s))
    )


def render_pending_interaction(
    templates: Jinja2Templates, session_key: str, interaction
) -> str:
    return templates.get_template("partials/pending_interaction.html").render(
        session_key=session_key, interaction=interaction
    )
