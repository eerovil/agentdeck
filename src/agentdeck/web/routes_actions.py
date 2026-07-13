"""State-changing web actions."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from ..models import Capability
from ..providers import PROVIDERS
from .deps import (
    get_accounts,
    get_config,
    get_injector,
    get_templates,
    require_access,
    resolve_session,
)

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


def _render_status(request: Request, session_key: str, status) -> HTMLResponse:
    templates = get_templates(request)
    return templates.TemplateResponse(
        request,
        "partials/inject_status.html",
        {"session_key": session_key, "inject_status": status},
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


def _render_owned_controls(request: Request, session_key: str, session) -> HTMLResponse:
    return get_templates(request).TemplateResponse(
        request,
        "partials/owned_controls.html",
        {
            "session_key": session_key,
            "can_steer": Capability.STEER in session.capabilities,
            "can_interrupt": Capability.INTERRUPT in session.capabilities,
        },
    )


@router.post("/sessions/{session_key}/inject", response_class=HTMLResponse)
async def inject_message(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        raise HTTPException(status_code=415, detail="form content type required")
    account, session, provider = resolve_session(request, session_key)
    config = get_config(request).inject
    injector = get_injector(request)
    if not config.enabled or (
        Capability.INJECT not in session.capabilities and not injector.can_queue(session.key)
    ):
        raise HTTPException(status_code=403, detail="message injection unavailable")
    form = await request.form()
    raw = form.get("message")
    message = raw.strip() if isinstance(raw, str) else ""
    if not message or len(message) > config.max_message_chars:
        raise HTTPException(status_code=422, detail="invalid message")

    result = await injector.start(account, session, provider, message)
    if not result.accepted:
        raise HTTPException(status_code=403, detail=result.reason)
    response = _render_status(request, session.key, injector.status(session.key))
    response.status_code = 202
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
    if Capability.STEER not in session.capabilities:
        raise HTTPException(status_code=403, detail="active-turn steering unavailable")
    form = await request.form()
    raw = form.get("message")
    message = raw.strip() if isinstance(raw, str) else ""
    if not message or len(message) > get_config(request).inject.max_message_chars:
        raise HTTPException(status_code=422, detail="invalid message")
    result = await provider.steer(account, session, message)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=result.reason)
    return HTMLResponse(
        '<div id="steer-result" class="inject-result complete">Sent to active turn.</div>'
    )


@router.post("/sessions/{session_key}/interrupt", response_class=HTMLResponse)
async def interrupt_turn(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_session(request, session_key)
    if Capability.INTERRUPT not in session.capabilities:
        raise HTTPException(status_code=403, detail="turn interruption unavailable")
    result = await provider.interrupt(account, session)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=result.reason)
    return HTMLResponse(
        '<div id="steer-result" class="inject-result">Stopping Codex…</div>'
    )


@router.get("/partials/sessions/{session_key}/interaction", response_class=HTMLResponse)
async def pending_interaction(request: Request, session_key: str) -> HTMLResponse:
    account, session, provider = resolve_session(request, session_key)
    return _render_interaction(
        request,
        session_key,
        provider.pending_interaction(account, session),
    )


@router.get("/partials/sessions/{session_key}/controls", response_class=HTMLResponse)
async def owned_controls(request: Request, session_key: str) -> HTMLResponse:
    account, session, provider = resolve_session(request, session_key)
    if not provider.owns_session(account, session):
        return HTMLResponse('<div id="owned-controls"></div>')
    return _render_owned_controls(request, session_key, session)


@router.post("/sessions/{session_key}/interaction", response_class=HTMLResponse)
async def answer_interaction(request: Request, session_key: str) -> HTMLResponse:
    _require_same_origin(request)
    account, session, provider = resolve_session(request, session_key)
    interaction = provider.pending_interaction(account, session)
    if interaction is None:
        raise HTTPException(status_code=409, detail="interaction is no longer pending")
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
    result = await provider.answer_interaction(
        account,
        session,
        interaction.id,
        answers=answers,
        decision=decision if isinstance(decision, str) else None,
    )
    if not result.accepted:
        raise HTTPException(status_code=422, detail=result.reason)
    return _render_interaction(request, session_key, None)


@router.post("/sessions/new", response_class=HTMLResponse)
async def new_session(request: Request) -> HTMLResponse:
    _require_same_origin(request)
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        raise HTTPException(status_code=415, detail="form content type required")
    form = await request.form()
    account_key = form.get("account_key")
    raw_cwd = form.get("cwd")
    raw_message = form.get("message")
    if not all(isinstance(value, str) for value in (account_key, raw_cwd, raw_message)):
        raise HTTPException(status_code=422, detail="invalid new-session request")
    account, provider = _account_provider(request, account_key)
    try:
        cwd = Path(raw_cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=422, detail="invalid working directory") from None
    result = await get_injector(request).start_new(account, provider, cwd, raw_message)
    if not result.accepted:
        status_code = 409 if "already starting" in (result.reason or "") else 422
        raise HTTPException(status_code=status_code, detail=result.reason)
    response = _render_new_status(
        request,
        account.key,
        get_injector(request).new_status(account.key),
    )
    response.status_code = 202
    return response


@router.get("/partials/new-session-status", response_class=HTMLResponse)
async def new_session_status(request: Request, account_key: str) -> HTMLResponse:
    _account_provider(request, account_key)
    status = get_injector(request).new_status(account_key)
    response = _render_new_status(
        request,
        account_key,
        status,
    )
    if status is not None and status.state == "complete" and status.session_key:
        response.headers["HX-Redirect"] = f"/sessions/{status.session_key}"
    return response
