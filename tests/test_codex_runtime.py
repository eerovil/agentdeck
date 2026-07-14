from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

import httpx

from agentdeck.config import AccountConfig, AppConfig
from agentdeck.models import Account, PendingInteraction
from agentdeck.providers.codex.runtime_client import CodexRuntimeClient
from agentdeck.runtime import CodexRuntime


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
