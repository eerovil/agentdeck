"""New-Claude-chat provider wiring: worker client + start_session/inject/interrupt."""

from __future__ import annotations

import asyncio
import json
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
            "workers": {
                "chat-abc": {
                    "session_id": "sid-1",
                    "live": True,
                    "turn_active": True,
                    "stalled": False,
                }
            },
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
    host.deliver.assert_awaited_with(
        "chat-abc",
        "hi",
        cwd=None,
        fresh=False,
        images=[],
        model=None,
        permission_mode=None,
    )
    await client.stop()


async def test_client_waits_for_worker_turn_completion():
    from unittest.mock import MagicMock

    app = _runtime_app()
    active = True

    def snapshot():
        return {
            "workers": {
                "chat-abc": {
                    "session_id": "sid-1",
                    "live": True,
                    "turn_active": active,
                    "stalled": False,
                }
            }
        }

    host = MagicMock()
    host.snapshot = snapshot
    app.state.claude_workers.hosts["main"] = host
    client = _client_against(app)
    assert await client.probe()

    async def finish():
        nonlocal active
        await asyncio.sleep(0.01)
        active = False

    finishing = asyncio.create_task(finish())
    result = await client.wait_for_turn("sid-1", timeout_s=1)
    await finishing

    assert result.accepted and result.session_id == "sid-1"
    await client.stop()


class _FakeWorkerClient:
    def __init__(self):
        self.calls = []
        self._owned = {"sid-1": "chat-1"}
        self.available = True
        self._live = True
        self._active = True

    def owns(self, sid):
        return sid in self._owned

    def key_for(self, sid):
        return self._owned.get(sid)

    def turn_active(self, sid):
        return self._active and sid in self._owned

    def live(self, sid):
        return self._live and sid in self._owned

    async def deliver(
        self,
        key,
        message,
        *,
        cwd=None,
        fresh=False,
        images=None,
        model=None,
        permission_mode=None,
    ):
        from agentdeck.models import InjectResult

        self.calls.append(
            (
                "deliver",
                key,
                message,
                cwd,
                fresh,
                images or [],
                model,
                permission_mode,
            )
        )
        return InjectResult(True, session_id="sid-new")

    async def wait_for_turn(self, session_id, *, timeout_s):
        from agentdeck.models import InjectResult

        self.calls.append(("wait", session_id, timeout_s))
        return InjectResult(True, session_id=session_id)

    async def interrupt(self, key):
        from agentdeck.models import InjectResult

        self.calls.append(("interrupt", key))
        return InjectResult(True)


async def test_provider_supports_new_session():
    assert ClaudeCodeProvider.supports_new_session is True
    provider = ClaudeCodeProvider()
    assert provider.can_start_session(_account()) is False
    provider._workers[_account().key] = _FakeWorkerClient()
    assert provider.can_start_session(_account()) is True


async def test_start_session_spawns_fresh_worker():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    result = await provider.start_session(_account(), Path("/tmp"), "hello", timeout_s=1)
    assert result.accepted
    kind, key, message, cwd, fresh, images, model, permission_mode = fake.calls[0]
    assert (kind, message, cwd, fresh) == ("deliver", "hello", "/tmp", True)
    assert images == [] and model is None and permission_mode is None
    assert key.startswith("chat-")  # generated key


async def test_start_session_forwards_images_model_and_read_only_mode(tmp_path):
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    image = tmp_path / "screen.png"
    image.write_bytes(b"png")

    result = await provider.start_session(
        _account(),
        tmp_path,
        "inspect",
        timeout_s=1,
        images=[image],
        sandbox="read-only",
        model="haiku",
    )

    assert result.accepted
    assert fake.calls[0][5:] == ([str(image)], "haiku", "plan")


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
    assert ("deliver", "chat-1", "reply", None, False, [], None, None) in fake.calls
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


async def test_owned_worker_liveness_stays_visible_without_cli_registry():
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers["claude_code:main"] = fake
    session = Session(
        key="claude_code:main:sid-1",
        account_key="claude_code:main",
        session_id="sid-1",
        status=SessionStatus.IDLE,
    )

    changed = provider.sweep_liveness(_account(), [session])

    assert changed == [session]
    assert session.status is SessionStatus.LIVE
    assert session.thinking is True
    assert session.show_when_idle is True

    fake._active = False
    changed = provider.sweep_liveness(_account(), [session])
    assert changed == [session]
    assert session.status is SessionStatus.LIVE
    assert session.thinking is False
    assert session.show_when_idle is True


async def test_provider_wait_and_result_complete_claude_delegation(tmp_path):
    account = Account("claude_code:main", "claude_code", "main", tmp_path / "cfg")
    transcript_dir = account.root / "projects" / "-tmp-project"
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "sid-1.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "sid-1",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Delegation complete."}],
                },
            }
        )
        + "\n"
    )
    provider = ClaudeCodeProvider()
    fake = _FakeWorkerClient()
    provider._workers[account.key] = fake

    waited = await provider.wait_for_session(account, "sid-1", timeout_s=12)
    result = await provider.session_result(account, "sid-1")

    assert waited.accepted
    assert ("wait", "sid-1", 12) in fake.calls
    assert result == "Delegation complete."
