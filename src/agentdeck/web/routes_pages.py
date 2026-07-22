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


def _session_list_controls_context(request: Request, accounts, state) -> dict[str, object]:
    """Build the controls that accompany every full session list."""
    from ..providers import PROVIDERS

    new_chat_accounts = [
        account
        for account in accounts
        if PROVIDERS[account.provider_id].can_start_session(account)
    ]
    new_chat_account_keys = {account.key for account in new_chat_accounts}
    # Model options for each provider present in the create form; the picker JS
    # shows only those matching the selected account's provider.
    new_chat_models = [
        {"provider_id": provider_id, "value": choice.value, "label": choice.label}
        for provider_id in sorted({account.provider_id for account in new_chat_accounts})
        for choice in PROVIDERS[provider_id].selectable_models
    ]
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

    return {
        "new_chat_accounts": new_chat_accounts,
        "new_chat_enabled": request.app.state.config.inject.enabled,
        "new_chat_cwds": cwd_options,
        "new_chat_default_cwd": default_cwd,
        "new_chat_models": new_chat_models,
    }


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
    from .render import (
        _usage_rows,
        session_deckhand_status,
        session_labels,
        session_queue_summaries,
    )

    presentation = state.session_presentation()
    resp = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "rows": _usage_rows(accounts, state),
            "host": state.host_stats,
            "sessions": presentation.top_level,
            "children_of": presentation.children_of,
            "labels": session_labels(accounts),
            "queue_summaries": session_queue_summaries(
                presentation.visible, request.app.state.injector
            ),
            **_session_list_controls_context(request, accounts, state),
            "assistant": request.app.state.assistant,
            "assistant_sessions": state.sessions,
            "deckhand_status": session_deckhand_status(request.app.state.assistant),
            "working_count": presentation.working_count,
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
    observed_messages = request.app.state.injector.observe_transcript(
        session.key, detail.events
    )
    from datetime import UTC, datetime

    from ..models import Capability, SessionStatus
    from .render import (
        _usage_rows,
        assistant_insights_for_session,
        pending_injection_messages,
        resolve_activity_label,
        session_deckhand_status,
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
    presentation = state.session_presentation()
    label = resolve_activity_label(
        has_question=bool(session.question),
        live=live,
        streaming=bool(session.thinking),
        last_event=last_ev,
        age_s=age,
        has_working_subagent=presentation.has_working_subagent(session),
    )
    effective_session = presentation.display(session)
    labels = session_labels(accounts)
    account_label = labels.get(session.account_key)
    assistant = request.app.state.assistant
    git_context = await assistant.ensure_session_context(
        session, transcript_context=_pr_reference_text(detail.events)
    )
    resp = templates.TemplateResponse(
        request,
        "session.html",
        {
            "session": effective_session,
            "detail": detail,
            "transcript_after_seq": max(
                (event.seq for event in detail.events), default=0
            ),
            # Desktop keeps the live session list beside the selected chat.
            "sessions": presentation.top_level,
            "children_of": presentation.children_of,
            "labels": labels,
            "deckhand_status": session_deckhand_status(assistant),
            "selected_session_key": session.key,
            "queue_summaries": session_queue_summaries(
                presentation.visible, request.app.state.injector
            ),
            "observed_messages": observed_messages,
            "pending_messages": pending_injection_messages(
                request.app.state.injector.status(session.key)
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
            "pending_interaction": (
                provider.pending_interaction(account, session)
                if Capability.INTERACT in session.capabilities
                else None
            ),
            "assistant": assistant,
            "assistant_sessions": state.sessions,
            "working_count": presentation.working_count,
            "assistant_insights": assistant_insights_for_session(
                assistant, session.key
            ),
            "assistant_handled": assistant.handled_insight(session.key),
            "session_handled": assistant.is_handled(session.key),
            "session_waiting": session.is_waiting,
            "git_context": git_context,
            # topbar usage bars, rendered server-side so they paint immediately
            # (the per-session SSE stream then keeps them live over one socket).
            "rows": _usage_rows(accounts, state),
            "host": state.host_stats,
            **_session_list_controls_context(request, accounts, state),
        },
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp
