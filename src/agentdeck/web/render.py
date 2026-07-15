"""Shared fragment rendering — used by both the HTMX partial routes and the
SSE stream, so a partial fetched over HTTP and one pushed over SSE are byte
-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from ..inject import InjectionService
from ..models import Account
from ..models import activity_label as activity_label  # single impl lives in models
from ..state import AppState


def _now() -> datetime:
    return datetime.now(UTC)


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


def _usage_rows(accounts: list[Account], state: AppState) -> list[dict]:
    rows = []
    for acc in accounts:
        snap = state.usage.get(acc.key)
        rows.append(
            {
                "account": acc,
                "usage": snap,
                "stale_age": reltime_age(snap),
                # epoch seconds of the fetch, so the client can tick "updated Ns
                # ago" every second without waiting on a server re-render.
                "fetched_epoch": (snap.fetched_at.timestamp() if snap else ""),
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


def render_assistant(templates: Jinja2Templates, assistant, state: AppState) -> str:
    return templates.get_template("partials/assistant_panel.html").render(
        assistant=assistant,
        assistant_sessions=state.sessions,
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
        pending = [item for item in status.items if item.state in ("queued", "running")]
        if pending:
            summaries[session.key] = {
                "count": len(pending),
                "text": pending[-1].text,
            }
    return summaries


def pending_injection_messages(status, events) -> list:
    """Pending app-level turns that are not in the provider transcript yet."""
    if status is None:
        return []
    transcript_users = [event for event in events if event.role == "user" and event.text]
    matched_events: set[int] = set()
    pending = []
    for item in status.items:
        if item.state not in ("queued", "running"):
            continue
        match = next(
            (
                index
                for index, event in enumerate(transcript_users)
                if index not in matched_events
                and event.text == item.text
                and event.ts is not None
                and event.ts >= item.created_at
            ),
            None,
        )
        if match is not None:
            matched_events.add(match)
            continue
        pending.append(item)
    return pending


def render_session_list(
    templates: Jinja2Templates,
    accounts: list[Account],
    state: AppState,
    *,
    selected_session_key: str | None = None,
    injector: InjectionService | None = None,
) -> str:
    sessions = state.visible_sessions()
    return templates.get_template("partials/session_list.html").render(
        sessions=sessions,
        labels=session_labels(accounts),
        selected_session_key=selected_session_key,
        queue_summaries=(session_queue_summaries(sessions, injector) if injector else {}),
    )


def render_transcript_events(templates: Jinja2Templates, events) -> str:
    tmpl = templates.get_template("partials/transcript_event.html")
    return "".join(tmpl.render(e=e) for e in events)


def render_session_status(templates: Jinja2Templates, session) -> str:
    return templates.get_template("partials/session_status.html").render(s=session)


def render_tool_activity(
    templates: Jinja2Templates, label: str | None, elapsed_s: float = 0.0
) -> str:
    return templates.get_template("partials/tool_activity.html").render(
        label=label, elapsed_s=max(0, int(elapsed_s))
    )
