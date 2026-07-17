"""New-Claude-chat provider wiring: worker client + start_session/inject/interrupt."""

from __future__ import annotations

from pathlib import Path

import httpx

from agentdeck.config import AppConfig
from agentdeck.models import Account, Session, SessionStatus
from agentdeck.providers.claude_code.provider import ClaudeCodeProvider
from agentdeck.providers.claude_code.worker import DeliverResult
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient
from agentdeck.runtime import create_runtime_app


def _account() -> Account:
    return Account("claude_code:main", "claude_code", "main", Path("/tmp/cfg"))


def _runtime_app(**worker_cfg):
    cfg = AppConfig.model_validate(
        {
            "claude_workers": {"enabled": True, **worker_cfg},
            "accounts": [{"provider": "claude_code", "label": "main", "config_dir": "~/.claude"}],
        }
    )
    return create_runtime_app(cfg)


def _client_against(app) -> ClaudeWorkerClient:
    return ClaudeWorkerClient(
        _account(), transport=httpx.ASGITransport(app=app)
    )


async def test_probe_false_when_workers_disabled():
    app = create_runtime_app(AppConfig())  # workers off → runtime returns 404
    client = _client_against(app)
    assert await client.probe() is False
    await client.stop()


async def test_client_maps_sessions_and_delivers():
    from unittest.mock import AsyncMock, MagicMock

    app = _runtime_app()
    host = MagicMock()
    host.snapshot = MagicMock(
        return_value={
            "workers": {"chat-abc": {"session_id": "sid-1", "turn_active": True, "stalled": False}},
            "live_count": 1,
        }
    )
    host.deliver = AsyncMock(return_value=DeliverResult(True, "steered", session_id="sid-1"))
    app.state.claude_workers.hosts["main"] = host

    client = _client_against(app)
    assert await client.probe() is True
    assert client.owns("sid-1") and client.key_for("sid-1") == "chat-abc"
    assert client.turn_active("sid-1") is True

    result = await client.deliver("chat-abc", "hi")
    assert result.accepted
    host.deliver.assert_awaited_with("chat-abc", "hi", cwd=None, fresh=False)
    await client.stop()


class _FakeWorkerClient:
    def __init__(self):
        self.calls = []
        self._owned = {"sid-1": "chat-1"}

    def owns(self, sid):
        return sid in self._owned

    def key_for(self, sid):
        return self._owned.get(sid)

    def turn_active(self, sid):
        return True

    async def deliver(self, key, message, *, cwd=None, fresh=False):
        from agentdeck.models import InjectResult

        self.calls.append(("deliver", key, message, cwd, fresh))
        return InjectResult(True, session_id="sid-new")

    async def interrupt(self, key):
        from agentdeck.models import InjectResult

        self.calls.append(("interrupt", key))
        return InjectResult(True)


async def test_provider_supports_new_session():
    assert ClaudeCodeProvider.supports_new_session is True


async def test_start_session_spawns_fresh_worker():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    result = await provider.start_session(_account(), Path("/tmp"), "hello", timeout_s=1)
    assert result.accepted
    kind, key, message, cwd, fresh = fake.calls[0]
    assert (kind, message, cwd, fresh) == ("deliver", "hello", "/tmp", True)
    assert key.startswith("chat-")  # generated key


async def test_start_session_errors_when_workers_absent():
    provider = ClaudeCodeProvider()
    result = await provider.start_session(_account(), Path("/tmp"), "hello", timeout_s=1)
    assert not result.accepted and "not enabled" in result.reason


async def test_inject_and_interrupt_resolve_session_to_key():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.LIVE,
    )
    r1 = await provider.inject(_account(), session, "reply", timeout_s=1)
    r2 = await provider.interrupt(_account(), session)
    assert r1.accepted and r2.accepted
    assert ("deliver", "chat-1", "reply", None, False) in fake.calls
    assert ("interrupt", "chat-1") in fake.calls


async def test_inject_rejects_unowned_session():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:other",
        account_key="claude_code:main",
        session_id="other",
        status=SessionStatus.IDLE,
    )
    result = await provider.inject(_account(), session, "reply", timeout_s=1)
    assert not result.accepted and "not a deck-owned worker" in result.reason
