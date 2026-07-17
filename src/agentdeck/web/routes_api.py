"""Machine-oriented delegation API for local agent-to-agent callers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..providers import PROVIDERS
from .deps import get_accounts, get_injector, get_state, require_access

router = APIRouter(prefix="/api", dependencies=[Depends(require_access)])


class DelegationRequest(BaseModel):
    cwd: str
    message: str
    account_key: str | None = None
    sandbox: Literal["read-only", "workspace-write"] = "workspace-write"
    model: str | None = None


def _delegation_account(request: Request, account_key: str | None):
    supported = [
        account
        for account in get_accounts(request)
        if PROVIDERS[account.provider_id].supports_new_session
    ]
    if account_key is not None:
        account = next((item for item in supported if item.key == account_key), None)
        if account is None:
            raise HTTPException(status_code=404, detail="unknown delegation account")
        return account
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


# --- deck-owned Claude workers -------------------------------------------
# Thin TCP-surface proxy to the runtime service (which owns worker processes
# over a Unix socket), so external callers — e.g. a board poller — drive
# workers without touching the socket. Keys are opaque and travel in the body.


class WorkerDeliverRequest(BaseModel):
    key: str
    message: str
    cwd: str | None = None
    fresh: bool = False


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
            account = next(
                (item for item in get_accounts(request) if item.key == session.account_key),
                None,
            )
            if account is not None:
                interaction = PROVIDERS[account.provider_id].pending_interaction(account, session)
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
