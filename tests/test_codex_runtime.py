from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx

from agentdeck.action_context import client_action_context
from agentdeck.config import AccountConfig, AppConfig
from agentdeck.models import Account, InjectResult, PendingInteraction
from agentdeck.providers.codex.runtime_client import CodexRuntimeClient
from agentdeck.runtime import CodexRuntime, create_runtime_app


def _account(tmp_path: Path) -> Account:
    return Account("codex:local", "codex", "local", tmp_path)


async def test_runtime_client_reconnects_to_shared_backend_state(tmp_path):
    state = {
        "thread-1": {
            "active_turn": "turn-1",
            "status": "active",
            "interaction": {
                "id": "question-1",
                "kind": "question",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "title": "Codex needs your answer",
                "message": None,
                "questions": [
                    {
                        "id": "database",
                        "header": "Database",
                        "prompt": "Which database?",
                        "options": [
                            {"label": "SQLite", "description": "Local", "value": None}
                        ],
                        "allow_other": False,
                        "secret": False,
                    }
                ],
                "command": None,
                "cwd": None,
                "url": None,
                "decisions": [],
            },
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": state})
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    first = CodexRuntimeClient(_account(tmp_path), transport=transport)
    await first.start()
    assert first.owns("thread-1")
    assert first.active_turn("thread-1") == "turn-1"
    assert first.interaction("thread-1").questions[0].prompt == "Which database?"
    await first.stop()

    # A fresh web-side client sees the state that stayed in the runtime.
    second = CodexRuntimeClient(_account(tmp_path), transport=transport)
    await second.start()
    assert second.active_turn("thread-1") == "turn-1"
    assert second.interaction("thread-1").id == "question-1"
    await second.stop()


async def test_cancelled_runtime_request_closes_web_side_socket(tmp_path):
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/queue":
            started.set()
            await asyncio.Future()
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(_account(tmp_path), transport=httpx.MockTransport(handler))
    task = asyncio.create_task(client.queue_turn("thread-1", "Queued work"))
    await started.wait()

    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)

    assert isinstance(result[0], asyncio.CancelledError)
    assert client._http.is_closed


async def test_runtime_activity_reports_codex_and_claude_active_turns():
    app = create_runtime_app(AppConfig())
    codex = MagicMock()
    codex.owned_threads.return_value = {"busy", "idle"}
    codex.active_turn.side_effect = lambda thread_id: "turn-1" if thread_id == "busy" else None
    app.state.runtime.clients["local"] = codex
    claude = MagicMock()
    claude.snapshot.return_value = {
        "workers": {
            "busy": {"turn_active": True},
            "idle": {"turn_active": False},
        }
    }
    app.state.claude_workers.hosts["main"] = claude

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.get("/activity")

    assert response.json() == {
        "active": True,
        "active_turns": {"codex": 1, "claude": 1},
    }


async def test_runtime_client_sends_compact_action(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/compact":
            requests.append(request)
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": {"thread-1": {}}})
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    result = await client.compact("thread-1")

    assert result.accepted
    assert json.loads(requests[0].content) == {"thread_id": "thread-1"}
    await client.stop()


async def test_runtime_client_forwards_browser_action_id(tmp_path):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/local/queue":
            requests.append(request)
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/accounts/local/state":
            return httpx.Response(200, json={"threads": {"thread-1": {}}})
        raise AssertionError(request.url.path)

    client = CodexRuntimeClient(
        _account(tmp_path), transport=httpx.MockTransport(handler)
    )
    with client_action_context("browser-action-123"):
        result = await client.queue_turn("thread-1", "Queued work")

    assert result.accepted
    assert json.loads(requests[0].content)["client_action_id"] == "browser-action-123"
    await client.stop()


async def test_runtime_exposes_compact_endpoint():
    app = create_runtime_app(AppConfig())
    runtime_client = MagicMock()
    runtime_client.compact = AsyncMock(return_value=InjectResult(True))
    app.state.runtime.clients["local"] = runtime_client

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/accounts/local/compact", json={"thread_id": "thread-1"}
        )

    assert response.status_code == 200
    assert response.json() == {"accepted": True, "reason": None, "session_id": None}
    runtime_client.compact.assert_awaited_once_with("thread-1")


def test_runtime_snapshot_serializes_owned_turn_and_interaction(tmp_path):
    class FakeClient:
        def owned_threads(self):
            return frozenset({"thread-1"})

        def active_turn(self, thread_id):
            assert thread_id == "thread-1"
            return "turn-1"

        def thread_status(self, thread_id):
            return "active"

        def interaction(self, thread_id):
            return PendingInteraction(
                id="approval-1",
                kind="command_approval",
                thread_id=thread_id,
                turn_id="turn-1",
                title="Approve command?",
                command="uv run pytest -q",
                decisions=("accept", "decline"),
            )

    runtime = CodexRuntime(
        AppConfig(
            accounts=[
                AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
            ]
        )
    )
    runtime.clients = {"local": FakeClient()}

    snapshot = runtime.snapshot("local")

    assert snapshot["threads"]["thread-1"] == {
        "active_turn": "turn-1",
        "status": "active",
        "interaction": asdict(FakeClient().interaction("thread-1")),
    }


async def test_runtime_keeps_upload_copy_until_accepted_turn_finishes(tmp_path):
    runtime = CodexRuntime(AppConfig())
    release = asyncio.Event()

    class FakeClient:
        async def wait_for_thread(self, thread_id):
            assert thread_id == "thread-1"
            await release.wait()

    upload_root = tmp_path / "preserved-upload"
    upload_root.mkdir()
    (upload_root / "image.png").write_bytes(b"image")

    runtime.defer_upload_cleanup(FakeClient(), "thread-1", upload_root)
    await asyncio.sleep(0)
    assert upload_root.exists()

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not upload_root.exists():
            break

    assert not upload_root.exists()
    await asyncio.sleep(0)  # let the task's discard callback run
    assert not runtime._cleanup_tasks


def test_systemd_services_keep_web_and_codex_in_separate_control_groups():
    root = Path(__file__).parents[1]
    web = (root / "systemd" / "agentdeck.service").read_text()
    runtime = (root / "systemd" / "agentdeck-codex.service").read_text()

    assert "Wants=agentdeck-codex.service" in web
    assert "agentdeck codex-runtime" not in web
    assert ".venv/bin/agentdeck" in web
    assert ".venv/bin/agentdeck codex-runtime" in runtime
    assert "uv run" not in web + runtime
    assert "PartOf=agentdeck.service" not in runtime
