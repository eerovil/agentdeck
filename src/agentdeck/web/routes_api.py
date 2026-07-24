"""Machine-oriented local API for agents, dispatchers, and dashboard controls."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from ..message_pins import event_is_pinnable, pinned_message_from_event
from ..models import Capability, PinnedMessage
from ..providers import PROVIDERS, pending_interaction_for
from .deps import get_accounts, get_injector, get_state, require_access, resolve_session

router = APIRouter(prefix="/api", dependencies=[Depends(require_access)])


class DelegationRequest(BaseModel):
    cwd: str
    message: str
    account_key: str | None = None
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    model: str | None = None
    # Codex approval policy. Human-initiated delegations default to "on-request"
    # (Codex asks before commands it deems risky); an autonomous caller (e.g. the
    # kanban worker) passes "never" so it runs unattended without prompts.
    approval_policy: Literal["untrusted", "on-failure", "on-request", "never"] = "on-request"
    # Raw id of the session initiating this delegation (e.g. the invoking Claude
    # chat, from CLAUDE_CODE_SESSION_ID). Lets the delegated child nest under it.
    parent_session_id: str | None = None


def _delegation_account(request: Request, account_key: str | None):
    supported = [
        account
        for account in get_accounts(request)
        if PROVIDERS[account.provider_id].can_start_session(account)
    ]
    if account_key is not None:
        account = next((item for item in supported if item.key == account_key), None)
        if account is None:
            raise HTTPException(status_code=404, detail="unknown delegation account")
        return account
    # Preserve the CLI's historical account-free behavior: machine delegation
    # means Codex unless the caller explicitly chooses another provider.
    codex = [account for account in supported if account.provider_id == "codex"]
    if len(codex) == 1:
        return codex[0]
    if len(supported) != 1:
        choices = ", ".join(account.key for account in supported) or "none"
        raise HTTPException(
            status_code=422,
            detail=f"account_key is required; available accounts: {choices}",
        )
    return supported[0]


def _interaction_payload(interaction) -> dict | None:
    if interaction is None:
        return None
    return {
        "id": interaction.id,
        "kind": interaction.kind,
        "title": interaction.title,
        "message": interaction.message,
        "command": interaction.command,
        "cwd": interaction.cwd,
        "url": interaction.url,
        "decisions": list(interaction.decisions),
        "questions": [
            {
                "id": question.id,
                "header": question.header,
                "prompt": question.prompt,
                "allow_other": question.allow_other,
                "secret": question.secret,
                "options": [
                    {
                        "label": option.label,
                        "description": option.description,
                        "value": option.value,
                    }
                    for option in question.options
                ],
            }
            for question in interaction.questions
        ],
    }


def _pin_payload(pin: PinnedMessage) -> dict:
    payload = jsonable_encoder(pin)
    payload["pinned"] = True
    return payload


def _require_transcript_session(request: Request, session_key: str):
    account, session, provider = resolve_session(request, session_key)
    if Capability.TRANSCRIPT not in session.capabilities:
        raise HTTPException(status_code=404, detail="transcript unavailable")
    return account, session, provider


# --- message pins ------------------------------------------------------
# The dashboard and running agents intentionally share this one contract.
# Transcript JSON advertises the per-event URL and methods so machine callers
# can discover it without provider-specific instructions.


@router.get("/sessions/{session_key}/pins")
async def list_message_pins(request: Request, session_key: str) -> dict:
    _require_transcript_session(request, session_key)
    return {
        "session_key": session_key,
        "pins": [_pin_payload(pin) for pin in get_state(request).pins_for(session_key)],
    }


@router.put("/sessions/{session_key}/pins/{seq}")
async def put_message_pin(request: Request, session_key: str, seq: int) -> dict:
    account, session, provider = _require_transcript_session(request, session_key)
    state = get_state(request)
    existing = next((pin for pin in state.pins_for(session_key) if pin.seq == seq), None)
    if existing is not None:
        return _pin_payload(existing)

    events = await provider.read_transcript(account, session)
    event = next(
        (
            candidate
            for candidate in events
            if candidate.seq == seq and event_is_pinnable(candidate)
        ),
        None,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="pinnable message not found")
    pin = pinned_message_from_event(session_key, event)
    state.pin_message(pin)
    return _pin_payload(pin)


@router.delete("/sessions/{session_key}/pins/{seq}")
async def delete_message_pin(request: Request, session_key: str, seq: int) -> dict:
    _require_transcript_session(request, session_key)
    get_state(request).unpin_message(session_key, seq)
    return {"session_key": session_key, "seq": seq, "pinned": False}


# --- deck-owned Claude workers -------------------------------------------
# Thin TCP-surface proxy to the runtime service (which owns worker processes
# over a Unix socket), so external callers — e.g. a board poller — drive
# workers without touching the socket. Keys are opaque and travel in the body.


class WorkerDeliverRequest(BaseModel):
    key: str
    message: str
    cwd: str | None = None
    fresh: bool = False
    # Mirror the runtime's DeliverRequest so the proxy doesn't silently drop
    # images / model / permission_mode an external caller sends.
    images: list[str] = Field(default_factory=list)
    model: str | None = None
    permission_mode: str | None = None
    delivery_id: str | None = None


class WorkerKeyRequest(BaseModel):
    key: str


async def _runtime_proxy(
    request: Request, method: str, path: str, json: dict | None = None
) -> dict:
    client: httpx.AsyncClient | None = getattr(request.app.state, "runtime_http", None)
    if client is None:
        raise HTTPException(status_code=503, detail="runtime service not configured")
    try:
        response = await client.request(method, path, json=json)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"claude worker runtime unavailable: {exc}"
        ) from None
    if response.status_code >= 400:
        detail: object = response.text
        if response.headers.get("content-type", "").startswith("application/json"):
            detail = response.json().get("detail", detail)
        raise HTTPException(status_code=response.status_code, detail=detail)
    return response.json()


@router.get("/claude/accounts/{label}/workers")
async def claude_workers(request: Request, label: str) -> dict:
    return await _runtime_proxy(request, "GET", f"/claude/accounts/{label}/workers")


@router.post("/claude/accounts/{label}/deliver")
async def claude_deliver(request: Request, label: str, body: WorkerDeliverRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/deliver", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/interrupt")
async def claude_interrupt(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/interrupt", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/stop")
async def claude_stop(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/stop", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/park")
async def claude_park(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/park", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/release")
async def claude_release(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/release", json=body.model_dump()
    )


@router.post("/claude/accounts/{label}/forget")
async def claude_forget(request: Request, label: str, body: WorkerKeyRequest) -> dict:
    return await _runtime_proxy(
        request, "POST", f"/claude/accounts/{label}/forget", json=body.model_dump()
    )


@router.post("/delegations", status_code=202)
async def start_delegation(request: Request, body: DelegationRequest) -> dict:
    account = _delegation_account(request, body.account_key)
    try:
        cwd = Path(body.cwd).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=422, detail="invalid working directory") from None
    result, delegation_id = await get_injector(request).start_delegation(
        account,
        PROVIDERS[account.provider_id],
        cwd,
        body.message,
        sandbox=body.sandbox,
        model=body.model,
        approval_policy=body.approval_policy,
        parent_session_id=body.parent_session_id,
    )
    if not result.accepted or delegation_id is None:
        raise HTTPException(status_code=422, detail=result.reason)
    return {
        "id": delegation_id,
        "state": "starting",
        "status_url": f"/api/delegations/{delegation_id}",
    }


@router.get("/delegations/{delegation_id}")
async def delegation_status(request: Request, delegation_id: str) -> dict:
    status = get_injector(request).delegation_status(delegation_id)
    if status is None:
        raise HTTPException(status_code=404, detail="unknown delegation")

    state = status.state
    interaction = None
    session_url = None
    if status.session_key is not None:
        session_url = f"/sessions/{status.session_key}"
        session = get_state(request).sessions.get(status.session_key)
        if session is not None:
            interaction = pending_interaction_for(session, get_accounts(request))
            if interaction is not None and state == "running":
                state = "waiting"

    return {
        "id": status.id,
        "state": state,
        "account_key": status.account_key,
        "session_key": status.session_key,
        "session_url": session_url,
        "reason": status.reason,
        "final_message": status.final_message,
        "interaction": _interaction_payload(interaction),
    }
