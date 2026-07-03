"""Shared fragment rendering — used by both the HTMX partial routes and the
SSE stream, so a partial fetched over HTTP and one pushed over SSE are byte
-identical.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.templating import Jinja2Templates

from ..models import Account
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


def register_filters(templates: Jinja2Templates) -> None:
    templates.env.filters["reltime"] = reltime
    templates.env.filters["reltime_ago"] = reltime_ago


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
        rows=_usage_rows(accounts, state)
    )


def session_labels(accounts: list[Account]) -> dict[str, str]:
    return {acc.key: acc.label for acc in accounts}


def render_session_list(
    templates: Jinja2Templates, accounts: list[Account], state: AppState
) -> str:
    return templates.get_template("partials/session_list.html").render(
        sessions=state.visible_sessions(), labels=session_labels(accounts)
    )


def render_transcript_events(templates: Jinja2Templates, events) -> str:
    tmpl = templates.get_template("partials/transcript_event.html")
    return "".join(tmpl.render(e=e) for e in events)


def render_session_status(templates: Jinja2Templates, session) -> str:
    return templates.get_template("partials/session_status.html").render(s=session)


def activity_label(live: bool, streaming: bool, last_ev) -> str | None:
    """What the agent is doing right now, or None when idle/dead.

    Keyed off the *open turn*, not just recent writes, so a long tool run or a
    slow first token doesn't read as idle:
    - last line is a tool call / tool result → "Using tools" (persists through
      long tools, where the transcript is quiet for the tool's whole duration);
    - last line is an unanswered user/queued prompt → "Working";
    - actively writing (recent transcript write) → "Working";
    - otherwise (LIVE but quiet, last line a finished reply) → None (idle)."""
    if not live:
        return None
    if last_ev is not None:
        if last_ev.role == "tool" or last_ev.tool_name:
            return "Using tools"
        if last_ev.role == "user":
            return "Working"
    if streaming:
        return "Working"
    return None


def render_tool_activity(templates: Jinja2Templates, label: str | None) -> str:
    return templates.get_template("partials/tool_activity.html").render(label=label)
