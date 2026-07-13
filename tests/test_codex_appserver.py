from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from agentdeck.models import Account
from agentdeck.providers.codex.appserver import CodexAppServer


def _server(tmp_path: Path) -> CodexAppServer:
    return CodexAppServer(Account("codex:test", "codex", "test", tmp_path))


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
    result = await server.start_thread(
        tmp_path,
        "Build it",
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
            "ephemeral": False,
            "threadSource": "agentdeck",
            "sandbox": "workspace-write",
            "model": "gpt-test",
            "approvalPolicy": "on-request",
        },
    )
    assert server.active_turn("thread-1") == "turn-1"

    result = await server.steer("thread-1", "Use SQLite instead")
    assert result.accepted
    assert calls[-1] == (
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "Use SQLite instead"}],
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
