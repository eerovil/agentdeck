"""State-changing web actions."""

from __future__ import annotations

import logging
from pathlib import Path
from shlex import quote
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData

from ..images import media_type_for_suffix, sniff_suffix, suffix_for_media_type
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
from .uploads import (
    ImageUploadError,
    cleanup_image_files,
    save_uploaded_images,
)

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


def _render_new_status(
    request: Request,
    account_key: str,
    status,
    *,
    result_id: str = "new-session-result",
    status_url: str = "/partials/new-session-status",
) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/new_session_status.html",
        {
            "account_key": account_key,
            "new_status": status,
            "result_id": result_id,
            "status_url": status_url,
        },
    )


def _render_interaction(request: Request, session_key: str, interaction) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/pending_interaction.html",
        {"session_key": session_key, "interaction": interaction},
    )


@router.get("/sessions/{session_key}/transcript-images/{seq}/{image_index}")
async def transcript_image(
    request: Request, session_key: str, seq: int, image_index: int
) -> Response:
    """Serve one bounded embedded transcript image without inlining it in HTML."""
    account, session, provider = resolve_session(request, session_key)
    image = await provider.transcript_image(account, session, seq, image_index)
    if image is None:
        raise HTTPException(status_code=404, detail="transcript image not found")
    media_type, content = image
    expected_extension = suffix_for_media_type(media_type)
    if (
        expected_extension is None
        or len(content) > get_config(request).inject.max_image_bytes
        or sniff_suffix(content) != expected_extension
    ):
        raise HTTPException(status_code=404, detail="transcript image not found")
    return Response(content, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.get("/sessions/{session_key}/pending-images/{item_id}/{image_index}")
async def pending_message_image(
    request: Request, session_key: str, item_id: int, image_index: int
) -> Response:
    """Serve one validated upload while its message is still pending."""
    resolve_session(request, session_key)
    status = get_injector(request).status(session_key)
    item = next(
        (
            queued
            for queued in status.items
            if queued.id == item_id
            and queued.is_pending
        ),
        None,
    ) if status else None
    if item is None or image_index < 0 or image_index >= len(item.images):
        raise HTTPException(status_code=404, detail="pending image not found")
    image = item.images[image_index]
    media_type = media_type_for_suffix(image.suffix)
    if media_type is None:
        raise HTTPException(status_code=404, detail="pending image not found")
    try:
        content = image.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="pending image not found") from None
    return Response(content, media_type=media_type, headers={"Cache-Control": "private, no-store"})


@router.post("/assistant/refresh", response_class=HTMLResponse)
async def refresh_assistant(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    assistant = request.app.state.assistant
    if not assistant.request_refresh(manual=True):
        raise HTTPException(status_code=409, detail="orchestration assistant is disabled")
    from .render import render_assistant

    presentation = request.app.state.app_state.session_presentation()
    return HTMLResponse(
        render_assistant(get_templates(request), assistant, presentation),
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

    presentation = request.app.state.app_state.session_presentation()
    return HTMLResponse(
        render_assistant(get_templates(request), assistant, presentation)
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

    presentation = request.app.state.app_state.session_presentation()
    return HTMLResponse(
        render_assistant(get_templates(request), assistant, presentation)
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
    client_action_id = bind_action(
        request, session_key=session.key, provider=account.provider_id
    )
    config = get_config(request).inject
    if not config.enabled or Capability.STEER not in session.capabilities:
        raise HTTPException(status_code=403, detail="active-turn steering unavailable")
    message, images, _ = await _turn_form(request)
    with timing_span(request, "runtime"):
        receipt = await get_injector(request).deliver_now(
            account, session, provider, message, images, client_action_id=client_action_id
        )
    if not receipt.result.accepted:
        raise HTTPException(status_code=409, detail=receipt.result.reason)
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
    if Capability.INTERACT not in session.capabilities:
        raise HTTPException(status_code=403, detail="interaction unavailable")
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
    # Blank = provider/account default (no --model). A non-blank value must be one
    # the provider actually offers, so an unknown slug is a 422 rather than a raw
    # arg handed to a CLI/app-server.
    raw_model = form.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    if model and not provider.is_valid_model(model):
        cleanup_image_files(images)
        raise HTTPException(status_code=422, detail="invalid model")
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
                model=model or None,
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


@router.post("/sessions/{session_key}/continue", response_class=HTMLResponse)
async def continue_session(request: Request, session_key: str) -> HTMLResponse:
    """Start a new-account chat whose first turn fetches this transcript."""
    _require_same_origin(request)
    _, source_session, _ = resolve_session(request, session_key)
    if Capability.TRANSCRIPT not in source_session.capabilities:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    if source_session.cwd is None:
        raise HTTPException(status_code=422, detail="working directory unavailable")
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        raise HTTPException(status_code=415, detail="form content type required")
    form = await request.form()
    account_key = form.get("account_key")
    if not isinstance(account_key, str):
        raise HTTPException(status_code=422, detail="target account required")
    if account_key == source_session.account_key:
        raise HTTPException(status_code=422, detail="choose another account")
    account, provider = _account_provider(request, account_key)
    raw_model = form.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    if model and not provider.is_valid_model(model):
        raise HTTPException(status_code=422, detail="invalid model")
    transcript_url = str(
        request.url_for("session_transcript", session_key=source_session.key)
    )
    message = (
        "Continue the work from this AgentDeck chat in the new session:\n"
        f"{transcript_url}\n\n"
        "Before taking any substantive action, fetch and read the complete transcript "
        "with:\n"
        f"curl --fail --silent --show-error {quote(transcript_url)}\n\n"
        "Treat it as the prior conversation context and continue from where it ended."
    )
    client_action_id = bind_action(request, provider=account.provider_id)
    with timing_span(request, "queue"):
        result = await get_injector(request).start_new(
            account,
            provider,
            source_session.cwd,
            message,
            model=model or None,
            client_action_id=client_action_id,
        )
    if not result.accepted:
        status_code = 409 if "already starting" in (result.reason or "") else 422
        raise HTTPException(status_code=status_code, detail=result.reason)
    response = _render_new_status(
        request,
        account.key,
        get_injector(request).new_status(account.key),
        result_id="continue-session-result",
        status_url="/partials/continue-session-status",
    )
    response.status_code = 202
    return response


async def _new_session_status_response(
    request: Request,
    account_key: str,
    *,
    result_id: str = "new-session-result",
    status_url: str = "/partials/new-session-status",
) -> HTMLResponse:
    account, provider = _account_provider(request, account_key)
    status = get_injector(request).new_status(account_key)
    response = _render_new_status(
        request,
        account_key,
        status,
        result_id=result_id,
        status_url=status_url,
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


@router.get("/partials/new-session-status", response_class=HTMLResponse)
async def new_session_status(request: Request, account_key: str) -> HTMLResponse:
    return await _new_session_status_response(request, account_key)


@router.get("/partials/continue-session-status", response_class=HTMLResponse)
async def continue_session_status(request: Request, account_key: str) -> HTMLResponse:
    return await _new_session_status_response(
        request,
        account_key,
        result_id="continue-session-result",
        status_url="/partials/continue-session-status",
    )
