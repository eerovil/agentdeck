"""ClaudeWorkerClient: the runtime-socket behaviors it inherits from
RuntimeSocketClient — cancel-close + reopen, and client_action_id forwarding."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from agentdeck.action_context import client_action_context
from agentdeck.models import Account
from agentdeck.providers.claude_code.worker_client import ClaudeWorkerClient


def _account(tmp_path: Path) -> Account:
    return Account("claude_code:main", "claude_code", "main", tmp_path)


async def test_worker_client_reopens_after_cancellation_close(tmp_path):
    # A web deploy cancels an in-flight deliver; the base closes the socket so
    # uvicorn can shut down. A still-running web process must reopen it on the
    # next read, or the owned map freezes and finished sessions pin as "working".
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deliver"):
            started.set()
            await asyncio.Future()  # hang until cancelled
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.deliver("key-1", "hello"))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert client._http.is_closed

    # refresh() transparently rebuilds the closed client rather than dying.
    await client.refresh()
    assert not client._http.is_closed
    assert client.available


async def test_worker_client_forwards_client_action_id(tmp_path):
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deliver"):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"accepted": True, "session_id": "s-1"})
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    with client_action_context("act-42"):
        result = await client.deliver("key-1", "hello")

    assert result.accepted
    assert result.session_id == "s-1"
    assert seen["payload"]["client_action_id"] == "act-42"


async def test_worker_client_omits_client_action_id_without_a_context(tmp_path):
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/deliver"):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/workers"):
            return httpx.Response(200, json={"workers": {}})
        raise AssertionError(request.url.path)

    client = ClaudeWorkerClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    await client.deliver("key-1", "hello")
    assert "client_action_id" not in seen["payload"]
