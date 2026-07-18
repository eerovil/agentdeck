"""HTTP surface for deck-owned Claude workers: runtime routes + web proxy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx

from agentdeck.config import AppConfig
from agentdeck.providers.claude_code.worker import DeliverResult
from agentdeck.runtime import create_runtime_app


def _runtime_app(**worker_cfg):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, **worker_cfg},
            "accounts": [{"provider": "claude_code", "label": "main", "config_dir": "~/.claude"}],
        }
    )
    return create_runtime_app(cfg)


def test_per_account_overrides_merge_over_base():
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {
                "enabled": True,
                "max_workers": 4,
                "permission_mode": "",
                "accounts": {
                    "alt": {"permission_mode": "bypassPermissions", "max_workers": 3},
                },
            },
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": "~/.claude"},
                {"provider": "claude_code", "label": "alt", "config_dir": "~/.claude2"},
            ],
        }
    )
    base = cfg.claude_workers.for_account("main")
    assert base.permission_mode == "" and base.max_workers == 4
    alt = cfg.claude_workers.for_account("alt")
    assert alt.permission_mode == "bypassPermissions" and alt.max_workers == 3
    # untouched fields inherit the base
    assert alt.state_dir == base.state_dir and alt.enabled is True


async def test_runtime_host_applies_account_overrides(tmp_path):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {
                "enabled": True,
                "state_dir": str(tmp_path),
                "accounts": {"alt": {"permission_mode": "bypassPermissions"}},
            },
            "accounts": [
                {"provider": "claude_code", "label": "main", "config_dir": "~/.claude"},
                {"provider": "claude_code", "label": "alt", "config_dir": "~/.claude2"},
            ],
        }
    )
    app = create_runtime_app(cfg)
    assert app.state.claude_workers.host("alt").permission_mode == "bypassPermissions"
    assert app.state.claude_workers.host("main").permission_mode is None


async def test_deliver_endpoint_disabled_returns_404():
    app = create_runtime_app(AppConfig())  # claude_workers off by default
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        r = await client.post(
            "/claude/accounts/main/deliver", json={"key": "k", "message": "m", "cwd": "/tmp"}
        )
    assert r.status_code == 404


async def test_deliver_and_key_endpoints_route_to_host():
    app = _runtime_app()
    host = MagicMock()
    host.deliver = AsyncMock(return_value=DeliverResult(True, "spawned", session_id="s1"))
    host.interrupt = AsyncMock(return_value=DeliverResult(True, "interrupted"))
    host.stop_worker = AsyncMock(return_value=DeliverResult(True, "stopped"))
    host.forget = MagicMock(return_value=True)
    app.state.claude_workers.hosts["main"] = host

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        d = await client.post(
            "/claude/accounts/main/deliver",
            json={"key": "owner/repo#12", "message": "go", "cwd": "/tmp"},
        )
        i = await client.post("/claude/accounts/main/interrupt", json={"key": "owner/repo#12"})
        s = await client.post("/claude/accounts/main/stop", json={"key": "owner/repo#12"})
        f = await client.post("/claude/accounts/main/forget", json={"key": "owner/repo#12"})

    assert d.json() == {
        "accepted": True,
        "action": "spawned",
        "reason": None,
        "session_id": "s1",
    }
    # key with '#' and '/' survives because it travels in the body, not the path
    host.deliver.assert_awaited_once_with("owner/repo#12", "go", cwd="/tmp", fresh=False)
    host.interrupt.assert_awaited_once_with("owner/repo#12")
    host.stop_worker.assert_awaited_once_with("owner/repo#12")
    host.forget.assert_called_once_with("owner/repo#12")
    assert i.json()["action"] == "interrupted"
    assert s.json()["action"] == "stopped"
    assert f.json() == {"removed": True}


async def test_web_proxy_forwards_to_runtime_and_surfaces_errors(tmp_path):
    from agentdeck.web.routes_api import _runtime_proxy

    # Fake runtime app the proxy client will hit over ASGI.
    runtime = _runtime_app()
    host = MagicMock()
    host.deliver = AsyncMock(return_value=DeliverResult(False, "rejected", reason="over_budget"))
    runtime.state.claude_workers.hosts["main"] = host

    class Req:
        class app:
            class state:
                runtime_http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=runtime), base_url="http://agentdeck-runtime"
                )

    result = await _runtime_proxy(
        Req,
        "POST",
        "/claude/accounts/main/deliver",
        json={"key": "k", "message": "m", "cwd": "/tmp"},
    )
    assert result["reason"] == "over_budget"
    await Req.app.state.runtime_http.aclose()


async def test_web_proxy_503_when_runtime_unset():
    import pytest
    from fastapi import HTTPException

    from agentdeck.web.routes_api import _runtime_proxy

    class Req:
        class app:
            class state:
                pass

    with pytest.raises(HTTPException) as exc:
        await _runtime_proxy(Req, "GET", "/claude/accounts/main/workers")
    assert exc.value.status_code == 503
