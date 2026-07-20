"""State-changing web actions."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData

from ..models import Capability
from ..providers import PROVIDERS
from .action_timing import bind_action, timing_span
from .deps import (
    get_accounts,
    get_config,
    get_db,
    get_injector,
    get_state,
    get_templates,
    require_access,
    resolve_session,
)
from .uploads import ImageUploadError, cleanup_image_files, save_uploaded_images

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_access)])


def _require_same_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="cross-site action refused")
    origin = request.headers.get("origin")
    if not origin:
        return
    parsed = urlsplit(origin)
    if parsed.scheme != request.url.scheme or parsed.netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="origin mismatch")


def _render_status(request: Request, session_key: str, status, queued_item=None) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "partials/inject_status.html",
        {
            "session_key": session_key,
            "inject_status": status,
            "queued_item": queued_item,
        },
    )


def _account_provider(request: Request, account_key: str):
    account = next((item for item in get_accounts(request) if item.key == account_key), None)
    if account is None:
        raise HTTPException(status_code=404, detail="unknown account")
    return account, PROVIDERS[account.provider_id]


def _render_new_status(request: Request, account_key: str, status) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/new_session_status.html",
        {"account_key": account_key, "new_status": status},
    )


def _render_interaction(request: Request, session_key: str, interaction) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/pending_interaction.html",
        {"session_key": session_key, "interaction": interaction},
    )


@router.post("/assistant/refresh", response_class=HTMLResponse)
async def refresh_assistant(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    assistant = request.app.state.assistant
    if not assistant.request_refresh(manual=True):
        raise HTTPException(status_code=409, detail="orchestration assistant is disabled")
    from .render import render_assistant

    return HTMLResponse(
        render_assistant(get_templates(request), assistant, request.app.state.app_state),
        status_code=202,
    )


@router.post("/assistant/handle", response_class=HTMLResponse)
async def handle_assistant_insight(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    form = await request.form()
    session_key = form.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        raise HTTPException(status_code=422, detail="session key required")
    assistant = request.app.state.assistant
    if not assistant.handle(session_key):
        raise HTTPException(status_code=404, detail="Deckhand item not found")
    from .render import render_assistant

    return HTMLResponse(
        render_assistant(get_templates(request), assistant, request.app.state.app_state)
    )


@router.post("/assistant/unhandle", response_class=HTMLResponse)
async def unhandle_assistant_insight(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    form = await request.form()
    session_key = form.get("session_key")
    if not isinstance(session_key, str) or not session_key:
        raise HTTPException(status_code=422, detail="session key required")
    assistant = request.app.state.assistant
    if not assistant.unhandle(session_key):
        raise HTTPException(status_code=404, detail="handled Deckhand item not found")
    from .render import render_assistant

    return HTMLResponse(
        render_assistant(get_templates(request), assistant, request.app.state.app_state)
    )


async def _turn_form(request: Request) -> tuple[str, list[Path], FormData]:
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        raise HTTPException(status_code=415, detail="form content type required")
    config = get_config(request).inject
    with timing_span(request, "form"):
        form = await request.form()
    raw = form.get("message")
    message = raw.strip() if isinstance(raw, str) else ""
    if not message or len(message) > config.max_message_chars:
        raise HTTPException(status_code=422, detail="invalid message")
    try:
        with timing_span(request, "uploads"):
            images = await save_uploaded_images(form, config)
    except ImageUploadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return message, images, form


@router.post("/sessions/{session_key}/inject", response_class=HTMLResponse)
async def inject_message(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_session(request, session_key)
    client_action_id = bind_action(
        request, session_key=session.key, provider=account.provider_id
    )
    config = get_config(request).inject
    injector = get_injector(request)
    if not config.enabled or (
        Capability.INJECT not in session.capabilities and not injector.can_queue(session.key)
    ):
        raise HTTPException(status_code=403, detail="message injection unavailable")
    queued_behind_turn = session.thinking or injector.can_queue(session.key)
    message, images, _ = await _turn_form(request)
    try:
        with timing_span(request, "queue"):
            result = await injector.start(
                account,
                session,
                provider,
                message,
                images,
                client_action_id=client_action_id,
            )
    except BaseException:
        cleanup_image_files(images)
        raise
    if not result.accepted:
        cleanup_image_files(images)
        raise HTTPException(status_code=403, detail=result.reason)
    status = injector.status(session.key)
    queued_item = status.items[-1] if status and status.items else None
    with timing_span(request, "render"):
        response = _render_status(request, session.key, status, queued_item)
    response.status_code = 202
    response.headers["X-AgentDeck-Action-State"] = (
        "queued" if queued_behind_turn else "accepted"
    )
    return response


@router.get("/partials/sessions/{session_key}/inject-status", response_class=HTMLResponse)
async def inject_status(request: Request, session_key: str) -> HTMLResponse:
    resolve_session(request, session_key)
    status = get_injector(request).status(session_key)
    return _render_status(request, session_key, status)


@router.post("/sessions/{session_key}/steer", response_class=HTMLResponse)
async def steer_turn(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_session(request, session_key)
    bind_action(request, session_key=session.key, provider=account.provider_id)
    config = get_config(request).inject
    if not config.enabled or Capability.STEER not in session.capabilities:
        raise HTTPException(status_code=403, detail="active-turn steering unavailable")
    message, images, _ = await _turn_form(request)
    try:
        kwargs = {"images": images} if images else {}
        with timing_span(request, "runtime"):
            result = await provider.steer(account, session, message, **kwargs)
    except BaseException:
        cleanup_image_files(images)
        raise
    if not result.accepted:
        cleanup_image_files(images)
        raise HTTPException(status_code=409, detail=result.reason)
    if images:
        get_injector(request).defer_cleanup(
            account,
            provider,
            session.session_id,
            images,
        )
    return HTMLResponse(
        '<div id="steer-result" class="inject-result complete">Sent to active turn.</div>'
    )


@router.post("/sessions/{session_key}/interrupt", response_class=HTMLResponse)
async def interrupt_turn(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_session(request, session_key)
    bind_action(request, session_key=session.key, provider=account.provider_id)
    if Capability.INTERRUPT not in session.capabilities:
        raise HTTPException(status_code=403, detail="turn interruption unavailable")
    with timing_span(request, "runtime"):
        result = await provider.interrupt(account, session)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=result.reason)
    return HTMLResponse(
        '<div id="inject-result" class="inject-result running" aria-live="polite"'
        ' aria-label="Stopping active turn"><span class="send-spinner"'
        ' aria-hidden="true"></span></div>'
    )


@router.get("/partials/sessions/{session_key}/interaction", response_class=HTMLResponse)
async def pending_interaction(request: Request, session_key: str) -> HTMLResponse:
    # The detail page renders this section server-side and keeps it live over SSE
    # (the `interaction` event, pushed only when the pending interaction actually
    # changes — see routes_sse). This endpoint is the on-demand render used by the
    # answer POST's swap target.
    account, session, provider = resolve_session(request, session_key)
    return _render_interaction(
        request,
        session_key,
        provider.pending_interaction(account, session),
    )


@router.post("/sessions/{session_key}/interaction", response_class=HTMLResponse)
async def answer_interaction(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_session(request, session_key)
    bind_action(request, session_key=session.key, provider=account.provider_id)
    interaction = provider.pending_interaction(account, session)
    if interaction is None:
        raise HTTPException(status_code=409, detail="interaction is no longer pending")
    with timing_span(request, "form"):
        form = await request.form()
    interaction_id = form.get("interaction_id")
    decision = form.get("decision")
    if interaction_id != interaction.id:
        raise HTTPException(status_code=409, detail="interaction changed")
    answers: dict[str, list[str]] = {}
    for question in interaction.questions:
        values = [
            value.strip()
            for value in form.getlist(f"answer__{question.id}")
            if isinstance(value, str) and value.strip()
        ]
        other = form.get(f"other__{question.id}")
        if isinstance(other, str) and other.strip():
            values.append(other.strip())
        if values:
            answers[question.id] = values
    if interaction.kind == "question" and any(
        question.id not in answers for question in interaction.questions
    ):
        raise HTTPException(status_code=422, detail="answer every question")
    if sum(len(value) for values in answers.values() for value in values) > get_config(
        request
    ).inject.max_message_chars:
        raise HTTPException(status_code=422, detail="answers are too long")
    with timing_span(request, "runtime"):
        result = await provider.answer_interaction(
            account,
            session,
            interaction.id,
            answers=answers,
            decision=decision if isinstance(decision, str) else None,
        )
    if not result.accepted:
        raise HTTPException(status_code=422, detail=result.reason)
    with timing_span(request, "render"):
        return _render_interaction(request, session_key, None)


@router.post("/sessions/new", response_class=HTMLResponse)
async def new_session(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    message, images, form = await _turn_form(request)
    account_key = form.get("account_key")
    raw_cwd = form.get("cwd")
    if not all(isinstance(value, str) for value in (account_key, raw_cwd)):
        cleanup_image_files(images)
        raise HTTPException(status_code=422, detail="invalid new-session request")
    try:
        account, provider = _account_provider(request, account_key)
    except BaseException:
        cleanup_image_files(images)
        raise
    try:
        cwd = Path(raw_cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        cleanup_image_files(images)
        raise HTTPException(status_code=422, detail="invalid working directory") from None
    try:
        client_action_id = bind_action(request, provider=account.provider_id)
        with timing_span(request, "queue"):
            result = await get_injector(request).start_new(
                account,
                provider,
                cwd,
                message,
                images,
                client_action_id=client_action_id,
            )
    except BaseException:
        cleanup_image_files(images)
        raise
    if not result.accepted:
        cleanup_image_files(images)
        status_code = 409 if "already starting" in (result.reason or "") else 422
        raise HTTPException(status_code=status_code, detail=result.reason)
    # This UI route is the one intentional source of the shared form default.
    # Machine delegations use /api/delegations and must not change it.
    get_db(request).record_manual_new_chat_cwd(str(cwd))
    with timing_span(request, "render"):
        response = _render_new_status(
            request,
            account.key,
            get_injector(request).new_status(account.key),
        )
    response.status_code = 202
    return response


@router.get("/partials/new-session-status", response_class=HTMLResponse)
async def new_session_status(request: Request, account_key: str) -> HTMLResponse:
    account, provider = _account_provider(request, account_key)
    status = get_injector(request).new_status(account_key)
    response = _render_new_status(
        request,
        account_key,
        status,
    )
    if status is not None and status.state == "complete" and status.session_key:
        # The periodic scan loop may not have discovered the freshly started
        # session yet, in which case its detail page 404s "unknown session"
        # until the next scan lands (issue #4). Run one targeted scan now so the
        # redirect target is resolvable the instant the browser follows it.
        state = get_state(request)
        if state.sessions.get(status.session_key) is None:
            try:
                sessions = await provider.scan_sessions(account)
                state.replace_account_sessions(account.key, sessions)
            except Exception:  # noqa: BLE001 -- the scan loop will still catch up
                log.warning(
                    "on-demand scan after new session failed for %s",
                    account.key,
                    exc_info=True,
                )
        response.headers["HX-Redirect"] = f"/sessions/{status.session_key}"
    return response
