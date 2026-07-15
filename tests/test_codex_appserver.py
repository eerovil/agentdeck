from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from agentdeck.models import Account
from agentdeck.providers.codex import WRITABLE_ROOTS_CONFIG_OVERRIDE
from agentdeck.providers.codex.appserver import (
    AGENTDECK_DEVELOPER_INSTRUCTIONS,
    CodexAppServer,
    _turn_input,
)


def _server(tmp_path: Path) -> CodexAppServer:
    return CodexAppServer(Account("codex:test", "codex", "test", tmp_path))


def test_turn_input_builds_text_and_local_images(tmp_path):
    images = [tmp_path / "one.png", tmp_path / "two.webp"]
    assert _turn_input("Inspect these", images) == [
        {"type": "text", "text": "Inspect these"},
        {"type": "localImage", "path": str(images[0])},
        {"type": "localImage", "path": str(images[1])},
    ]


async def test_app_server_enables_live_web_search(tmp_path):
    class IdleStdout:
        async def readline(self):
            await asyncio.Future()

    class Stdin:
        def close(self):
            pass

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdin = Stdin()
            self.stdout = IdleStdout()

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    spawned = {}

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return Process()

    server = CodexAppServer(
        Account("codex:test", "codex", "test", tmp_path),
        process_factory=factory,
    )
    server._request = AsyncMock(return_value={})
    server._notify = AsyncMock()
    server._recover_owned = AsyncMock()

    await server.start()
    assert spawned["args"] == (
        "codex",
        "app-server",
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
        "--stdio",
    )
    assert spawned["kwargs"]["env"]["CODEX_HOME"] == str(tmp_path)
    await server.stop()


async def test_request_user_input_round_trip(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._server_request(
        41,
        "item/tool/requestUserInput",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "questions": [
                {
                    "id": "database",
                    "header": "Database",
                    "question": "Which database should we use?",
                    "isOther": True,
                    "options": [
                        {"label": "Postgres", "description": "Relational"},
                        {"label": "SQLite", "description": "Embedded"},
                    ],
                }
            ],
        },
    )

    interaction = server.interaction("thread-1")
    assert interaction is not None
    assert interaction.kind == "question"
    assert interaction.questions[0].prompt == "Which database should we use?"
    assert [option.label for option in interaction.questions[0].options] == [
        "Postgres",
        "SQLite",
    ]

    writes = []

    async def write(message):
        writes.append(message)

    server._write = write
    result = await server.answer(
        "thread-1",
        interaction.id,
        answers={"database": ["Postgres"]},
    )
    assert result.accepted
    assert writes == [
        {"id": 41, "result": {"answers": {"database": {"answers": ["Postgres"]}}}}
    ]
    assert server.interaction("thread-1") is None


async def test_recover_owned_includes_vscode_persisted_agentdeck_threads(tmp_path):
    ours = tmp_path / "sessions" / "2026" / "07" / "14" / "ours.jsonl"
    theirs = tmp_path / "sessions" / "2026" / "07" / "14" / "theirs.jsonl"
    ours.parent.mkdir(parents=True)
    ours.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"originator": "agentdeck"}}
        )
        + "\n"
    )
    theirs.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"originator": "codex-tui"}}
        )
        + "\n"
    )
    server = _server(tmp_path)
    server._request = AsyncMock(
        return_value={
            "data": [
                {"id": "ours", "source": "vscode", "threadSource": None, "path": str(ours)},
                {
                    "id": "theirs",
                    "source": "vscode",
                    "threadSource": None,
                    "path": str(theirs),
                },
            ]
        }
    )

    await server._recover_owned()

    server._request.assert_awaited_once_with(
        "thread/list", {"sourceKinds": ["appServer", "vscode"], "limit": 200}
    )
    assert server.owns("ours")
    assert not server.owns("theirs")


async def test_recover_owned_rejects_transcript_outside_account(tmp_path):
    outside = tmp_path.parent / "outside-agentdeck.jsonl"
    outside.write_text(
        json.dumps(
            {"type": "session_meta", "payload": {"originator": "agentdeck"}}
        )
        + "\n"
    )
    server = _server(tmp_path)
    server._request = AsyncMock(
        return_value={"data": [{"id": "outside", "path": str(outside)}]}
    )

    await server._recover_owned()

    assert not server.owns("outside")


async def test_recover_owned_rejects_subagent_rollout(tmp_path):
    helper = tmp_path / "sessions" / "2026" / "07" / "14" / "helper.jsonl"
    helper.parent.mkdir(parents=True)
    helper.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "originator": "agentdeck",
                    "thread_source": "subagent",
                    "source": {"subagent": {"other": "guardian"}},
                },
            }
        )
        + "\n"
    )
    server = _server(tmp_path)
    server._request = AsyncMock(
        return_value={"data": [{"id": "helper", "path": str(helper)}]}
    )

    await server._recover_owned()

    assert not server.owns("helper")


async def test_command_approval_and_permission_response(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    writes = []

    async def write(message):
        writes.append(message)

    server._write = write
    server._server_request(
        "approval-1",
        "item/commandExecution/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-1",
            "startedAtMs": 1,
            "command": "uv run pytest -q",
            "cwd": str(tmp_path),
        },
    )
    interaction = server.interaction("thread-1")
    assert interaction is not None
    assert interaction.command == "uv run pytest -q"
    result = await server.answer(
        "thread-1", interaction.id, answers={}, decision="acceptForSession"
    )
    assert result.accepted
    assert writes[-1] == {
        "id": "approval-1",
        "result": {"decision": "acceptForSession"},
    }

    requested = {"network": {"enabled": True}}
    server._server_request(
        42,
        "item/permissions/requestApproval",
        {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "itemId": "item-2",
            "startedAtMs": 2,
            "cwd": str(tmp_path),
            "permissions": requested,
        },
    )
    interaction = server.interaction("thread-1")
    assert interaction is not None
    result = await server.answer(
        "thread-1", interaction.id, answers={}, decision="accept"
    )
    assert result.accepted
    assert writes[-1] == {
        "id": 42,
        "result": {"permissions": requested, "scope": "turn"},
    }


async def test_owned_thread_start_steer_and_interrupt(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        return {}

    server._request = request
    image = tmp_path / "screen.png"
    result = await server.start_thread(
        tmp_path,
        "Build it",
        images=[image],
        sandbox="workspace-write",
        model="gpt-test",
        approval_policy="on-request",
    )
    assert result.accepted
    assert result.session_id == "thread-1"
    assert calls[0] == (
        "thread/start",
        {
            "cwd": str(tmp_path),
            "developerInstructions": AGENTDECK_DEVELOPER_INSTRUCTIONS,
            "ephemeral": False,
            "threadSource": "agentdeck",
            "sandbox": "workspace-write",
            "model": "gpt-test",
            "approvalPolicy": "on-request",
        },
    )
    assert server.active_turn("thread-1") == "turn-1"
    assert calls[1] == (
        "turn/start",
        {
            "threadId": "thread-1",
            "input": [
                {"type": "text", "text": "Build it"},
                {"type": "localImage", "path": str(image)},
            ],
        },
    )

    result = await server.steer("thread-1", "Use SQLite instead", images=[image])
    assert result.accepted
    assert calls[-1] == (
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-1",
            "input": [
                {"type": "text", "text": "Use SQLite instead"},
                {"type": "localImage", "path": str(image)},
            ],
        },
    )

    result = await server.interrupt("thread-1")
    assert result.accepted
    assert calls[-1] == (
        "turn/interrupt",
        {"threadId": "thread-1", "turnId": "turn-1"},
    )


async def test_wait_for_owned_thread_completion(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._active_turn["thread-1"] = "turn-1"
    server._completed_turns["turn-1"] = {"id": "turn-1", "status": "completed"}

    result = await server.wait_for_thread("thread-1")

    assert result.accepted


async def test_queued_turn_is_acknowledged_when_followup_starts(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    server._active_turn["thread-1"] = "turn-old"

    async def request(method, params, **kwargs):
        assert method == "turn/start"
        assert params["threadId"] == "thread-1"
        return {"turn": {"id": "turn-new", "status": "inProgress"}}

    server._request = request
    queued = asyncio.create_task(server.queue_turn("thread-1", "Next message"))
    await asyncio.sleep(0)
    assert not queued.done()

    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-old", "status": "completed"},
        },
    )
    result = await asyncio.wait_for(queued, timeout=1)

    assert result.accepted
    assert server.active_turn("thread-1") == "turn-new"
    # No turn/completed notification for turn-new was needed: acceptance of
    # turn/start is the point where the message has finished sending.


async def test_compact_waits_for_current_turn_and_compaction_completion(tmp_path):
    server = _server(tmp_path)
    server.start = AsyncMock()
    server._owned.add("thread-1")
    server._loaded.add("thread-1")
    server._active_turn["thread-1"] = "turn-old"
    requested = asyncio.Event()
    calls = []

    async def request(method, params, **kwargs):
        calls.append((method, params))
        requested.set()
        return {}

    server._request = request
    compacting = asyncio.create_task(server.compact("thread-1"))
    await asyncio.sleep(0)
    assert calls == []

    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-old", "status": "completed"},
        },
    )
    await requested.wait()
    assert calls == [("thread/compact/start", {"threadId": "thread-1"})]
    assert not compacting.done()

    server._notification(
        "turn/started",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-compact", "status": "inProgress"},
        },
    )
    server._notification(
        "turn/completed",
        {
            "threadId": "thread-1",
            "turn": {"id": "turn-compact", "status": "completed"},
        },
    )

    result = await asyncio.wait_for(compacting, timeout=1)
    assert result.accepted
    assert server.active_turn("thread-1") is None


def test_unsafe_mcp_url_is_not_exposed(tmp_path):
    server = _server(tmp_path)
    server._owned.add("thread-1")
    server._server_request(
        43,
        "mcpServer/elicitation/request",
        {
            "threadId": "thread-1",
            "serverName": "example",
            "mode": "url",
            "message": "Open this",
            "url": "javascript:alert(1)",
            "elicitationId": "e-1",
        },
    )
    interaction = server.interaction("thread-1")
    assert interaction is not None
    assert interaction.url is None
