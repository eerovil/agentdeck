"""Full-page routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from ..message_pins import event_is_pinnable
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
        session_list_context,
    )

    presentation = state.session_presentation()
    session_context = session_list_context(
        accounts,
        presentation,
        injector=request.app.state.injector,
        assistant=request.app.state.assistant,
    )
    resp = templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "rows": _usage_rows(accounts, state),
            "host": state.host_stats,
            **session_context,
            **_session_list_controls_context(request, accounts, state),
        },
    )
    # Live dashboard — always revalidate so a deploy's HTML (and the inline
    # SSE-recovery JS) reaches the phone instead of a stale browser-cached copy.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@router.get(
    "/sessions/{session_key}/transcript.json",
    response_class=JSONResponse,
    dependencies=[Depends(require_access)],
)
async def session_transcript(request: Request, session_key: str) -> JSONResponse:
    """Expose the complete normalized transcript for agent-to-agent handoffs."""
    account, session, provider = resolve_session(request, session_key)
    from ..models import Capability

    if Capability.TRANSCRIPT not in session.capabilities:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    events = await provider.read_transcript(account, session)
    pinned_seqs = {pin.seq for pin in get_state(request).pins_for(session.key)}
    encoded_events = []
    for event in events:
        encoded = jsonable_encoder(event)
        encoded["image_urls"] = [
            str(
                request.url_for(
                    "transcript_image",
                    session_key=session.key,
                    seq=event.seq,
                    image_index=index,
                )
            )
            for index in range(len(event.image_media_types))
        ]
        if event_is_pinnable(event):
            encoded["pin_action"] = {
                "pinned": event.seq in pinned_seqs,
                "url": str(
                    request.url_for(
                        "put_message_pin", session_key=session.key, seq=event.seq
                    )
                ),
                "pin_method": "PUT",
                "unpin_method": "DELETE",
            }
        encoded_events.append(encoded)
    response = JSONResponse(
        {
            "session_key": session.key,
            "account_key": session.account_key,
            "working_directory": str(session.cwd) if session.cwd else None,
            "session_url": str(
                request.url_for("session_detail", session_key=session.key)
            ),
            "pins_url": str(
                request.url_for("list_message_pins", session_key=session.key)
            ),
            "events": encoded_events,
        }
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.get(
    "/sessions/{session_key}", response_class=HTMLResponse, dependencies=[Depends(require_access)]
)
async def session_detail(request: Request, session_key: str) -> HTMLResponse:
    account, session, provider = resolve_session(request, session_key)
    templates = get_templates(request)
    accounts = get_accounts(request)
    state = get_state(request)
    detail = await provider.load_transcript(account, session)
    pins = state.pins_for(session.key)
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
        session_labels,
        session_list_context,
    )

    last_ev = detail.events[-1] if detail.events else None
    live = session.status == SessionStatus.LIVE
    progress_at = session.last_progress or session.last_activity
    age = (
        (datetime.now(UTC) - progress_at).total_seconds()
        if progress_at
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
        lifecycle_active=session.lifecycle_active,
    )
    effective_session = presentation.display(session)
    labels = session_labels(accounts)
    account_label = labels.get(session.account_key)
    assistant = request.app.state.assistant
    session_context = session_list_context(
        accounts,
        presentation,
        injector=request.app.state.injector,
        assistant=assistant,
        selected_session_key=session.key,
    )
    git_context = await assistant.ensure_session_context(
        session, transcript_context=_pr_reference_text(detail.events)
    )
    controls_context = _session_list_controls_context(request, accounts, state)
    continue_chat_accounts = [
        candidate
        for candidate in controls_context["new_chat_accounts"]
        if candidate.key != session.account_key
    ]
    resp = templates.TemplateResponse(
        request,
        "session.html",
        {
            "session": effective_session,
            "active_subagent_sessions": presentation.active_child_sessions(session),
            "detail": detail,
            "pins": pins,
            "pinned_seqs": {pin.seq for pin in pins},
            "transcript_after_seq": max(
                (event.seq for event in detail.events), default=0
            ),
            # Desktop keeps the live session list beside the selected chat.
            **session_context,
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
            "pending_interaction": provider.pending_interaction(account, session),
            "assistant_insights": assistant_insights_for_session(
                assistant, session.key
            ),
            "assistant_handled": assistant.handled_insight(session.key),
            "assistant_session_title": effective_session.display_title,
            "session_handled": assistant.is_handled(session.key),
            "session_waiting": session.is_waiting,
            "continue_chat_accounts": (
                continue_chat_accounts
                if controls_context["new_chat_enabled"]
                and Capability.TRANSCRIPT in session.capabilities
                and session.cwd is not None
                else []
            ),
            "git_context": git_context,
            # topbar usage bars, rendered server-side so they paint immediately
            # (the per-session SSE stream then keeps them live over one socket).
            "rows": _usage_rows(accounts, state),
            "host": state.host_stats,
            **controls_context,
        },
    )
    resp.headers["Cache-Control"] = "no-cache"
    return resp
