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


_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Tiny unicode sparkline over 0-100 percentages; '' when < 2 points."""
    if len(values) < 2:
        return ""
    return "".join(_SPARK[min(int(v / 100 * (len(_SPARK) - 1)), len(_SPARK) - 1)] for v in values)


def _usage_rows(accounts: list[Account], state: AppState) -> list[dict]:
    rows = []
    db = getattr(state, "db", None)
    for acc in accounts:
        snap = state.usage.get(acc.key)
        spark = sparkline(db.recent_five_hour(acc.key)) if db is not None else ""
        rows.append(
            {"account": acc, "usage": snap, "stale_age": reltime_age(snap), "spark": spark}
        )
    return rows


def reltime_age(snap) -> str:
    if snap is None:
        return ""
    delta = (_now() - snap.fetched_at).total_seconds()
    mins = int(delta // 60)
    return f"{mins}m" if mins else "just now"


def render_limit_bars(templates: Jinja2Templates, accounts: list[Account], state: AppState) -> str:
    return templates.get_template("partials/limit_bars.html").render(
        rows=_usage_rows(accounts, state)
    )


def render_session_list(
    templates: Jinja2Templates, accounts: list[Account], state: AppState
) -> str:
    groups = [
        {"account": acc, "sessions": state.sessions_for_account(acc.key)} for acc in accounts
    ]
    return templates.get_template("partials/session_list.html").render(groups=groups)


def render_transcript_events(templates: Jinja2Templates, events) -> str:
    tmpl = templates.get_template("partials/transcript_event.html")
    return "".join(tmpl.render(e=e) for e in events)


def render_session_status(templates: Jinja2Templates, session) -> str:
    return templates.get_template("partials/session_status.html").render(s=session)
