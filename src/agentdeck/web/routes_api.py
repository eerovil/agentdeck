"""Machine-oriented delegation API for local agent-to-agent callers."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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
