"""Full-page routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .deps import (
    get_accounts,
    get_db,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)

router = APIRouter()


def _pr_reference_text(events) -> str | None:
    """PR-bearing conversation fragments from the visible history.

    System context and tool traffic can contain unrelated repository history,
    including PRs from injected memories or rendered AgentDeck pages. Only
    references stated in the user/assistant conversation belong to this chat.
    """
    fragments = []
    for event in events:
        if event.role not in {"user", "assistant"}:
            continue
        for value in (event.text, event.question):
            if value and (
                "github.com/" in value.lower()
                or "pr #" in value.lower()
                or "pull request" in value.lower()
            ):
                fragments.append(value)
    return "\n".join(fragments) or None


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    state = get_state(request)
    return JSONResponse(
        {
            "status": "ok",
            "accounts": len(get_accounts(request)),
            "sessions": len(state.sessions),
        }
    )


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_access)])
async def dashboard(request: Request) -> HTMLResponse:
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    from ..providers import PROVIDERS
    from .render import _usage_rows, session_labels, session_queue_summaries

    new_chat_accounts = [
        account
        for account in accounts
        if PROVIDERS[account.provider_id].supports_new_session
    ]
    new_chat_account_keys = {account.key for account in new_chat_accounts}
    cwd_options = sorted(
        {
            str(session.cwd)
            for session in state.all_sessions()
            if session.account_key in new_chat_account_keys and session.cwd is not None
        }
    )
    manual_cwd = get_db(request).load_manual_new_chat_cwd()
    if manual_cwd and manual_cwd not in cwd_options:
        cwd_options.insert(0, manual_cwd)
    # A configured default always wins over the remembered last-used cwd.
    configured_default = request.app.state.config.inject.new_chat_default_cwd
    default_cwd = configured_default or manual_cwd or (cwd_options[0] if cwd_options else "")
    if default_cwd and default_cwd not in cwd_options:
        cwd_options.insert(0, default_cwd)

    resp = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "rows": _usage_rows(accounts, state),
            "host": state.host_stats,
            "sessions": state.visible_sessions(),
            "labels": session_labels(accounts),
            "queue_summaries": session_queue_summaries(
                state.visible_sessions(), request.app.state.injector
            ),
            "new_chat_accounts": new_chat_accounts,
            "new_chat_enabled": request.app.state.config.inject.enabled,
            "new_chat_cwds": cwd_options,
            "new_chat_default_cwd": default_cwd,
            "assistant": request.app.state.assistant,
            "assistant_sessions": state.sessions,
            "working_count": sum(1 for s in state.visible_sessions() if s.thinking),
        },
    )
    # Live dashboard — always revalidate so a deploy's HTML (and the inline
    # SSE-recovery JS) reaches the phone instead of a stale browser-cached copy.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@router.get(
    "/sessions/{session_key}", response_class=HTMLResponse, dependencies=[Depends(require_access)]
)
async def session_detail(request: Request, session_key: str) -> HTMLResponse:
    account, session, provider = resolve_session(request, session_key)
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    detail = await provider.load_transcript(account, session)
    from datetime import UTC, datetime

    from ..models import Capability, SessionStatus, detailed_activity_label
    from .render import (
        _usage_rows,
        activity_label,
        assistant_insights_for_session,
        pending_injection_messages,
        session_labels,
        session_queue_summaries,
    )

    last_ev = detail.events[-1] if detail.events else None
    live = session.status == SessionStatus.LIVE
    age = (
        (datetime.now(UTC) - session.last_activity).total_seconds()
        if session.last_activity
        else 1e9
    )
    label = activity_label(live, bool(session.thinking), last_ev, age)
    label = detailed_activity_label(label, last_ev)
    labels = session_labels(accounts)
    account_label = labels.get(session.account_key)
    owned_session = provider.owns_session(account, session)
    assistant = request.app.state.assistant
    git_context = await assistant.ensure_session_context(
        session, transcript_context=_pr_reference_text(detail.events)
    )

    resp = templates.TemplateResponse(
        request,
        "session.html",
        {
            "session": session,
            "detail": detail,
            # Desktop keeps the live session list beside the selected chat.
            "sessions": state.visible_sessions(),
            "labels": labels,
            "selected_session_key": session.key,
            "queue_summaries": session_queue_summaries(
                state.visible_sessions(), request.app.state.injector
            ),
            "pending_messages": pending_injection_messages(
                request.app.state.injector.status(session.key), detail.events
            ),
            # the initial activity marker; the SSE stream refines it within ~1.5s
            "activity_label": label,
            "activity_elapsed": age,
            # which account (main/alt) this session belongs to
            "account_label": account_label,
            "can_inject": (
                request.app.state.config.inject.enabled
                and (
                    Capability.INJECT in session.capabilities
                    or request.app.state.injector.can_queue(session.key)
                )
            ),
            "inject_max_chars": request.app.state.config.inject.max_message_chars,
            "can_interrupt": Capability.INTERRUPT in session.capabilities,
            "owned_session": owned_session,
            "pending_interaction": provider.pending_interaction(account, session),
            "assistant": assistant,
            "assistant_sessions": state.sessions,
            "working_count": sum(1 for s in state.visible_sessions() if s.thinking),
            "assistant_insights": assistant_insights_for_session(
                assistant, session.key
            ),
            "git_context": git_context,
            # topbar usage bars, rendered server-side so they paint immediately
            # (the per-session SSE stream then keeps them live over one socket).
            "rows": _usage_rows(accounts, state),
            "host": state.host_stats,
        },
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp
