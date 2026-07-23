from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agentdeck.config import AccountConfig
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    TranscriptEvent,
)
from agentdeck.providers import PROVIDERS
from agentdeck.providers.codex import provider as provider_mod
from agentdeck.providers.codex import transcripts
from agentdeck.providers.codex.provider import CodexProvider
from agentdeck.state import AppState


def _line(type_: str, payload: dict, timestamp: str = "2026-07-13T10:00:00Z") -> dict:
    return {"timestamp": timestamp, "type": type_, "payload": payload}


def _message(role: str, text: str, *, phase: str | None = None) -> dict:
    block_type = "output_text" if role == "assistant" else "input_text"
    payload = {
        "type": "message",
        "role": role,
        "content": [{"type": block_type, "text": text}],
    }
    if phase is not None:
        payload["phase"] = phase
    return _line("response_item", payload)


def test_codex_user_image_is_kept_with_message_and_private_path_wrapper_is_hidden(tmp_path):
    path = tmp_path / "rollout.jsonl"
    image_url = "data:image/png;base64,iVBORw0KGgo="
    message = _message("user", "Inspect this screenshot")
    message["payload"]["content"].extend(
        [
            {
                "type": "input_text",
                "text": '<image name=[Image #1] path="/private/upload.png">',
            },
            {"type": "input_image", "image_url": image_url},
            {"type": "input_text", "text": "</image>"},
        ]
    )
    path.write_text(json.dumps(message) + "\n")

    (event,) = transcripts.read_events(path).events

    assert event.text == "Inspect this screenshot"
    assert event.image_media_types == ("image/png",)
    assert transcripts.transcript_image(path, 1, 0) == ("image/png", b"\x89PNG\r\n\x1a\n")


def test_codex_image_only_message_is_visible_and_unsafe_image_url_is_ignored(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(transcripts, "_MAX_IMAGE_URL_CHARS", 40)
    path = tmp_path / "rollout.jsonl"
    message = _message("user", "")
    message["payload"]["content"] = [
        {"type": "input_image", "image_url": "data:image/jpeg;base64,/9j/"},
        {"type": "input_image", "image_url": "https://example.com/tracker.png"},
        {"type": "input_image", "image_url": "data:image/png;base64," + "a" * 41},
    ]
    path.write_text(json.dumps(message) + "\n")

    (event,) = transcripts.read_events(path).events

    assert event.text is None
    assert event.image_media_types == ("image/jpeg",)
    assert transcripts.transcript_image(path, 1, 0) == ("image/jpeg", b"\xff\xd8\xff")


def _usage(last: dict, total: dict) -> dict:
    return _line(
        "event_msg",
        {
            "type": "token_count",
            "info": {
                "last_token_usage": last,
                "total_token_usage": total,
                "model_context_window": 258_400,
            },
            "rate_limits": None,
        },
    )


def test_recent_conversation_reads_bounded_tail_and_filters_tools(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", "old objective"),
        _line("event_msg", {"type": "agent_message_delta", "delta": "x" * 2_000}),
        _message("user", "new objective"),
        _message("assistant", "working on it"),
        _line(
            "response_item",
            {"type": "function_call_output", "call_id": "c", "output": "private output"},
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    events = transcripts.recent_conversation(path, limit=4, tail=900)

    assert [(event.role, event.text) for event in events] == [
        ("user", "new objective"),
        ("assistant", "working on it"),
    ]


def test_delegated_session_keys_requires_machine_status_tool_output(tmp_path):
    child_key = "codex:local:019f6085-5dbc-7f41-80b1-d32de9d80c14"
    quoted_key = "codex:local:019f6085-6cb1-7920-891f-9403d202a6f0"
    status = f"AgentDeck delegation: running (/sessions/{child_key})"
    quoted = f"AgentDeck delegation: running (/sessions/{quoted_key})"
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", f"A quoted example says {quoted}"),
        _line(
            "response_item",
            {"type": "custom_tool_call", "name": "exec", "input": quoted},
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "delegate",
                "output": [
                    {
                        "type": "input_text",
                        "text": f"diagnostic: {quoted}\nstarted\n{status}\n",
                    }
                ],
            },
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    assert transcripts.delegated_session_keys(path) == frozenset({child_key})


def test_delegated_session_keys_accepts_json_encoded_output_lines(tmp_path):
    path = tmp_path / "rollout.jsonl"
    child_key = "codex:codex:019f6ee6-394d-7402-80fd-b1f762ebadcd"
    line = {
        "type": "response_item",
        "payload": {
            "type": "custom_tool_call_output",
            "output": "=== 45285 ===\n"
            + json.dumps(
                {
                    "session_id": 45285,
                    "output": f"AgentDeck delegation: running (/sessions/{child_key})\\n",
                }
            ),
        },
    }
    path.write_text(json.dumps(line) + "\n")

    assert transcripts.delegated_session_keys(path) == frozenset({child_key})


async def test_machine_delegation_status_marks_legacy_child_delegated(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    child_sid = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    parent = _rollout(tmp_path, parent_sid)
    _rollout(tmp_path, child_sid)
    status = f"AgentDeck delegation: running (/sessions/codex:local:{child_sid})"
    with parent.open("a") as handle:
        handle.write(
            json.dumps(
                _line(
                    "response_item",
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "delegate",
                        "output": status,
                    },
                )
            )
            + "\n"
        )

    sessions = await CodexProvider().scan_sessions(_account(tmp_path))
    found = {session.session_id: session for session in sessions}

    assert found[parent_sid].is_delegated is False
    assert found[child_sid].is_delegated is True
    # The delegated child nests under the chat that delegated it.
    assert found[child_sid].parent_session_key == found[parent_sid].key
    assert found[parent_sid].parent_session_key is None


async def test_legacy_delegation_evidence_outside_session_cap_is_used(
    tmp_path, monkeypatch
):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    child_sid = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    parent = _rollout(tmp_path, parent_sid)
    child = _rollout(tmp_path, child_sid)
    with parent.open("a") as handle:
        handle.write(
            json.dumps(
                _line(
                    "response_item",
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "delegate",
                        "output": (
                            f"AgentDeck delegation: running (/sessions/codex:local:{child_sid})"
                        ),
                    },
                )
            )
            + "\n"
        )
    old_time = time.time() - 3600
    os.utime(parent, (old_time, old_time))
    os.utime(child, None)
    monkeypatch.setattr(provider_mod, "MAX_SESSIONS", 1)

    (session,) = await CodexProvider().scan_sessions(_account(tmp_path))

    assert session.session_id == child_sid
    assert session.is_delegated is True


def _rollout(root: Path, sid: str, *, old: bool = False) -> Path:
    directory = root / "sessions" / "2026" / "07" / "13"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-07-13T10-00-00-{sid}.jsonl"
    first_usage = {
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
        "total_tokens": 120,
    }
    second_usage = {
        "input_tokens": 80,
        "cached_input_tokens": 30,
        "output_tokens": 10,
        "reasoning_output_tokens": 2,
        "total_tokens": 90,
    }
    total_usage = {
        "input_tokens": 180,
        "cached_input_tokens": 90,
        "output_tokens": 30,
        "reasoning_output_tokens": 7,
        "total_tokens": 210,
    }
    lines = [
        _line(
            "session_meta",
            {
                "session_id": sid,
                "id": sid,
                "timestamp": "2026-07-13T10:00:00Z",
                "cwd": "/tmp/codex-project",
                "originator": "codex-tui",
                "cli_version": "0.144.3",
                "source": "cli",
                "thread_source": "user",
                "model_provider": "openai",
                "base_instructions": {},
                "history_mode": "save-all",
                "context_window": {"window_id": "window-id"},
                "git": {"branch": "main"},
            },
        ),
        _line("turn_context", {"model": "gpt-5.6-sol", "cwd": "/tmp/codex-project"}),
        _message("user", "<environment_context>synthetic metadata</environment_context>"),
        _message("user", "Build the parser safely"),
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec_command",
                "call_id": "call-1",
                "input": json.dumps({"cmd": "uv run pytest -q"}),
            },
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": "89 passed",
            },
        ),
        _usage(first_usage, first_usage),
        _message("assistant", "First update", phase="commentary"),
        _message("user", "Now finish it"),
        _message("assistant", "Implemented and tested", phase="final_answer"),
        _usage(second_usage, total_usage),
    ]
    path.write_text("".join(json.dumps(item) + "\n" for item in lines))
    if old:
        old_time = time.time() - 3600
        os.utime(path, (old_time, old_time))
    return path


def _account(root: Path) -> Account:
    return Account(key="codex:local", provider_id="codex", label="local", root=root)


async def test_codex_session_discovery_and_metadata(tmp_path):
    live_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    idle_sid = "019f5b22-ec93-7e52-9a15-d0cdb5c0e8c3"
    _rollout(tmp_path, live_sid)
    _rollout(tmp_path, idle_sid, old=True)

    provider = CodexProvider()
    sessions = await provider.scan_sessions(_account(tmp_path))
    found = {session.session_id: session for session in sessions}

    live = found[live_sid]
    assert live.status == SessionStatus.LIVE
    assert live.thinking is True
    assert live.title == "Build the parser safely"
    assert live.last_prompt == "Now finish it"
    assert live.last_text == "Implemented and tested"
    assert live.last_role == "agent"
    assert live.cwd == Path("/tmp/codex-project")
    assert live.model == "gpt-5.6-sol"
    assert live.kind is None
    assert live.started_at.isoformat() == "2026-07-13T10:00:00+00:00"
    assert live.context_tokens == 80
    assert live.tokens.input_tokens == 90  # 180 input includes 90 cached
    assert live.tokens.output_tokens == 30
    assert live.tokens.cache_read_tokens == 90
    assert live.tokens.total == 210
    assert live.capabilities == frozenset({Capability.TRANSCRIPT})
    assert live.deep_link is None
    assert live.show_when_idle is True
    assert found[idle_sid].status == SessionStatus.IDLE
    assert found[idle_sid].thinking is False
    assert found[idle_sid].show_when_idle is True


async def test_approval_review_rollout_cannot_replace_parent_session(tmp_path):
    parent_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    reviewer_file_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c07"
    _rollout(tmp_path, parent_sid)
    reviewer = _rollout(tmp_path, reviewer_file_sid)

    lines = [json.loads(line) for line in reviewer.read_text().splitlines()]
    lines[0]["payload"]["session_id"] = parent_sid
    lines[0]["payload"]["id"] = parent_sid
    lines[0]["payload"]["base_instructions"] = {
        "text": "You are judging one planned coding-agent action.\nAssess its risk."
    }
    lines[3] = _message("user", "Internal approval assessment prompt")
    reviewer.write_text("".join(json.dumps(item) + "\n" for item in lines))

    assert transcripts.transcript_meta(reviewer).is_approval_review is True
    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    assert session.session_id == parent_sid
    assert session.title == "Build the parser safely"


async def test_subagent_rollout_cannot_replace_parent_session(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    helper_sid = "019f6089-5b05-76d2-b480-f5cb5b793bfb"
    parent = _rollout(tmp_path, parent_sid)
    helper = _rollout(tmp_path, helper_sid)

    lines = [json.loads(line) for line in helper.read_text().splitlines()]
    # This is the real structured shape emitted by spawned agents and the
    # guardian/auto-review helper: a distinct file id but the parent's session.
    lines[0]["payload"].update(
        {
            "session_id": parent_sid,
            "source": {"subagent": {"other": "guardian"}},
            "thread_source": "subagent",
            "originator": "agentdeck",
        }
    )
    lines[3] = _message("user", "Internal guardian assessment prompt")
    helper.write_text("".join(json.dumps(item) + "\n" for item in lines))

    meta = transcripts.transcript_meta(helper)
    assert meta.session_id == parent_sid
    assert meta.is_subagent is True
    assert meta.is_spawned_subagent is False

    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    assert session.session_id == parent_sid
    assert session.title == "Build the parser safely"

    # The fallback path lookup must apply the same filter; otherwise losing the
    # in-memory path cache can temporarily bring the helper transcript back.
    provider._paths.clear()
    assert provider._transcript_path(_account(tmp_path), session) == parent


async def test_running_spawned_agents_are_counted_on_parent_session(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    _rollout(tmp_path, parent_sid, old=True)

    def helper(sid: str, source: dict, boundaries: list[str]) -> Path:
        path = _rollout(tmp_path, sid)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        lines[0]["payload"].update(
            {
                "session_id": parent_sid,
                "source": {"subagent": source},
                "thread_source": "subagent",
                "agent_nickname": "Faraday" if "thread_spawn" in source else None,
                "agent_role": "scout" if "thread_spawn" in source else None,
            }
        )
        lines.extend(_line("event_msg", {"type": boundary}) for boundary in boundaries)
        path.write_text("".join(json.dumps(item) + "\n" for item in lines))
        return path

    active = helper(
        "019f6085-5dbc-7f41-80b1-d32de9d80c14",
        {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}},
        ["task_started"],
    )
    completed = helper(
        "019f6085-6cb1-7920-891f-9403d202a6f0",
        {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}},
        ["task_started", "task_complete"],
    )
    expired = helper(
        "019f6085-7db2-7031-8be2-a51f6aa6b9bd",
        {"thread_spawn": {"parent_thread_id": parent_sid, "depth": 1}},
        ["task_started", "task_complete"],
    )
    helper(
        "019f6089-5b05-76d2-b480-f5cb5b793bfb",
        {"other": "guardian"},
        ["task_started"],
    )

    assert transcripts.transcript_meta(active).is_spawned_subagent is True
    assert transcripts.transcript_meta(active).task_active is True
    assert transcripts.transcript_meta(completed).task_active is False

    # Durable task boundaries outrank a quiet file: a long-running subagent must
    # remain visible instead of disappearing after the generic 30-second window.
    # Quiet for eight minutes is still an Active Turn; it stalls at ten.
    old_time = time.time() - 8 * 60
    os.utime(active, (old_time, old_time))
    expired_time = time.time() - 60 * 60
    os.utime(expired, (expired_time, expired_time))

    provider = CodexProvider()
    scanned = await provider.scan_sessions(_account(tmp_path))
    # The parent stays top-level; spawned subagents are now also child sessions
    # nested under it (parent_session_key set), while still counted on the parent.
    (session,) = [s for s in scanned if s.parent_session_key is None]
    assert session.session_id == parent_sid
    assert session.status == SessionStatus.IDLE
    assert session.thinking is False
    assert session.activity is None
    assert session.subagent_count == 1
    assert len(session.subagents) == 2
    assert session.subagents[0].nickname == "Faraday"
    assert session.subagents[0].role == "scout"
    assert session.subagents[0].status == "quiet"
    assert session.subagents[0].task == "Build the parser safely"
    assert session.subagents[1].status == "finished"
    assert session.subagents[1].result == "Implemented and tested"
    assert provider._subagent_names["019f6085-7db2-7031-8be2-a51f6aa6b9bd"] == "Faraday"
    # The recent spawned subagents surface as child sessions under the parent.
    children = [s for s in scanned if s.parent_session_key == session.key]
    assert {s.session_id for s in children} == {
        "019f6085-5dbc-7f41-80b1-d32de9d80c14",  # active
        "019f6085-6cb1-7920-891f-9403d202a6f0",  # completed (recent)
    }
    # A cheap liveness sweep only reads the quiet parent rollout; it must not
    # invent provider-local activity for work represented by the child.
    assert provider.sweep_liveness(_account(tmp_path), [session], AppState()) == []
    assert session.thinking is False


def test_codex_compacts_subagent_notifications_without_clobbering_last_prompt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    notification = (
        "<subagent_notification>\n"
        + json.dumps(
            {
                "agent_path": "019f6085-5dbc-7f41-80b1-d32de9d80c14",
                "status": {
                    "completed": "Read-only audit complete.\n\nFound one parser gap."
                },
            }
        )
        + "\n</subagent_notification>"
    )
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "Review the UX"),
                _message("user", notification),
            ]
        )
    )

    meta = transcripts.transcript_meta(path)
    events = transcripts.read_events(path).events

    assert meta.last_prompt == "Review the UX"
    assert len(events) == 2
    update = events[1]
    assert update.role == "system"
    assert update.subagent_status == "finished"
    assert update.subagent_id == "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    assert update.tool_summary == "Read-only audit complete."
    assert update.text == "Read-only audit complete.\n\nFound one parser gap."


def test_codex_compacts_turn_aborted_without_clobbering_last_prompt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    detail = (
        "The user interrupted the previous turn on purpose. Any running commands "
        "may have partially executed."
    )
    aborted = f"<turn_aborted>\n{detail}\n</turn_aborted>"
    path.write_text(
        "".join(
            json.dumps(item) + "\n"
            for item in [
                _message("user", "Review the UX"),
                _message("user", aborted),
            ]
        )
    )

    meta = transcripts.transcript_meta(path)
    events = transcripts.read_events(path).events

    assert meta.last_prompt == "Review the UX"
    assert len(events) == 2
    update = events[1]
    assert update.role == "system"
    assert update.text is None
    assert update.tool_name == "turn_aborted"
    assert update.tool_display_name == "Turn aborted"
    assert update.tool_summary == "Interrupted by user"
    assert update.tool_detail == detail
    assert update.turn_continues is False


def test_codex_labels_wrapped_subagent_operations(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": (
                    "const a = await tools.multi_agent_v1__spawn_agent("
                    "{agent_type:\"scout\"});"
                ),
            },
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": "const r = await tools.multi_agent_v1__wait_agent({targets:[\"id\"]});",
            },
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    spawn, wait = transcripts.read_events(path).events
    assert spawn.tool_display_name == "Start agents"
    assert wait.tool_display_name == "Wait for agents"


def test_codex_finds_latest_task_boundary_outside_bounded_meta_windows(tmp_path):
    path = tmp_path / "large-rollout.jsonl"
    lines = [
        _line("session_meta", {"session_id": "parent", "id": "child"}),
        _line(
            "event_msg",
            {"type": "task_started"},
            timestamp="2026-07-17T06:28:00Z",
        ),
        _message("assistant", "x" * (160 * 1024)),
        _line(
            "event_msg",
            {"type": "task_complete", "last_agent_message": "done"},
            timestamp="2026-07-17T06:33:00Z",
        ),
        _message("assistant", "y" * (300 * 1024)),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    meta = transcripts.transcript_meta(path)

    assert meta.task_active is False
    assert meta.task_status == "finished"
    assert meta.task_started_at.isoformat() == "2026-07-17T06:28:00+00:00"
    assert meta.task_finished_at.isoformat() == "2026-07-17T06:33:00+00:00"


async def test_subagent_roster_stays_scoped_to_latest_completed_parent_turn(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    parent = _rollout(tmp_path, parent_sid)
    with parent.open("a") as handle:
        for item in [
            _line("event_msg", {"type": "task_started"}, "2026-07-17T10:01:00Z"),
            _line("event_msg", {"type": "task_complete"}, "2026-07-17T10:02:00Z"),
            _line("event_msg", {"type": "task_started"}, "2026-07-17T10:03:00Z"),
            _line("event_msg", {"type": "task_complete"}, "2026-07-17T10:04:00Z"),
        ]:
            handle.write(json.dumps(item) + "\n")

    def completed_child(sid: str, nickname: str, started: str, finished: str) -> None:
        path = _rollout(tmp_path, sid)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        lines[0]["timestamp"] = started
        lines[0]["payload"].update(
            {
                "session_id": parent_sid,
                "id": sid,
                "timestamp": started,
                "source": {
                    "subagent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_sid,
                            "agent_nickname": nickname,
                            "agent_role": "scout",
                        }
                    }
                },
                "thread_source": "subagent",
            }
        )
        lines.extend(
            [
                _line("event_msg", {"type": "task_started"}, started),
                _line("event_msg", {"type": "task_complete"}, finished),
            ]
        )
        path.write_text("".join(json.dumps(item) + "\n" for item in lines))

    completed_child(
        "019f6085-5dbc-7f41-80b1-d32de9d80c14",
        "OldAgent",
        "2026-07-17T10:01:10Z",
        "2026-07-17T10:01:50Z",
    )
    completed_child(
        "019f6085-6cb1-7920-891f-9403d202a6f0",
        "CurrentAgent",
        "2026-07-17T10:03:10Z",
        "2026-07-17T10:03:50Z",
    )

    provider = CodexProvider()
    scanned = await provider.scan_sessions(_account(tmp_path))
    (session,) = [s for s in scanned if s.parent_session_key is None]

    assert session.subagent_count == 0
    assert [agent.nickname for agent in session.subagents] == ["CurrentAgent"]


async def test_spawn_output_names_fast_subagent_notification_before_periodic_scan(tmp_path):
    parent_sid = "019f6073-17bd-7251-9db1-383bfe24c143"
    path = _rollout(tmp_path, parent_sid)
    agent_id = "019f6085-5dbc-7f41-80b1-d32de9d80c14"
    notification = (
        "<subagent_notification>\n"
        + json.dumps(
            {"agent_path": agent_id, "status": {"completed": "Audit complete."}}
        )
        + "\n</subagent_notification>"
    )
    with path.open("a") as handle:
        for item in [
            _line(
                "response_item",
                {
                    "type": "custom_tool_call_output",
                    "output": [
                        {"type": "input_text", "text": "Script completed"},
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                {"agent_id": agent_id, "nickname": "Faraday"}
                            ),
                        },
                    ],
                },
            ),
            _message("user", notification),
        ]:
            handle.write(json.dumps(item) + "\n")

    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    detail = await provider.load_transcript(_account(tmp_path), session)
    update = next(event for event in detail.events if event.subagent_status)

    assert update.subagent_name == "Faraday"


async def test_completed_exec_session_is_injectable(tmp_path):
    sid = "019f5b5b-6281-7a00-a197-d020a1243d2d"
    directory = tmp_path / "sessions" / "2026" / "07" / "13"
    directory.mkdir(parents=True)
    path = directory / f"rollout-2026-07-13T10-00-00-{sid}.jsonl"
    lines = [
        _line(
            "session_meta",
            {"session_id": sid, "cwd": str(tmp_path), "source": "exec"},
        ),
        _line("event_msg", {"type": "task_started"}),
        _line("event_msg", {"type": "task_complete"}),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    old_time = time.time() - 3600
    os.utime(path, (old_time, old_time))

    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    assert session.kind == "exec"
    assert Capability.INJECT in session.capabilities
    assert Capability.DEEPLINK not in session.capabilities


async def test_codex_transcript_parsing_and_token_totals(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    (session,) = await provider.scan_sessions(_account(tmp_path))
    detail = await provider.load_transcript(_account(tmp_path), session)

    visible = [event for event in detail.events if event.text or event.tool_name]
    assert [event.role for event in visible] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    assert visible[0].text == "Build the parser safely"
    assert visible[1].tool_name == "exec_command"
    assert visible[1].tool_summary == "cmd: uv run pytest -q"
    assert visible[1].model == "gpt-5.6-sol"
    assert visible[2].text == "89 passed"
    assert visible[-1].text == "Implemented and tested"
    assert visible[-1].model == "gpt-5.6-sol"
    assert detail.tokens.input_tokens == 90
    assert detail.tokens.cache_read_tokens == 90
    assert detail.tokens.output_tokens == 30
    assert detail.tokens.total == 210
    assert detail.model == "gpt-5.6-sol"
    assert detail.skipped == 0
    token_events = [event for event in detail.events if event.tokens]
    assert token_events[-1].tokens.cache_read_tokens == 30
    assert (await provider.last_event(_account(tmp_path), session)).text == (
        "Implemented and tested"
    )

    offset, seq = transcripts.transcript_cursor(path)
    assert offset == path.stat().st_size
    assert seq == 11


def test_codex_partial_and_malformed_lines_resume(tmp_path):
    path = tmp_path / "rollout.jsonl"
    first = _message("user", "hello")
    partial = '{"timestamp":"2026-07-13T10:00:01Z","type":"response_item"'
    path.write_text(json.dumps(first) + "\n" + "{not json}\n" + partial)

    read = transcripts.read_events(path)
    assert [event.text for event in read.events] == ["hello"]
    assert read.skipped == 1
    assert read.seq == 2
    assert read.byte_offset < path.stat().st_size

    with path.open("a") as handle:
        handle.write(
            ',"payload":{"type":"message","role":"assistant",'
            '"content":[{"type":"output_text","text":"done"}]}}\n'
        )
    tail = transcripts.read_events(path, byte_offset=read.byte_offset, seq=read.seq)
    assert [event.text for event in tail.events] == ["done"]
    assert tail.seq == 3
    assert tail.byte_offset == path.stat().st_size


def test_codex_extracts_edited_file_from_apply_patch(tmp_path):
    path = tmp_path / "rollout.jsonl"
    call = _line(
        "response_item",
        {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** Update File: src/agentdeck/app.py\n@@\n*** End Patch",
        },
    )
    path.write_text(json.dumps(call) + "\n")

    (event,) = transcripts.read_events(path).events
    assert event.tool_name == "apply_patch"
    assert event.tool_summary == "path: src/agentdeck/app.py"
    assert "*** Update File: src/agentdeck/app.py" in event.tool_detail


def test_codex_summarizes_wrapped_tools_and_keeps_private_reasoning_heartbeat(tmp_path):
    path = tmp_path / "rollout.jsonl"
    lines = [
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "exec",
                "input": (
                    'const r = await tools.exec_command({cmd:"uv run pytest -q",'
                    'workdir:"/tmp"}); text(r.output)'
                ),
            },
        ),
        _line(
            "response_item",
            {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": (
                    'const patch = "*** Begin Patch\\n*** Update File: src/app.py\\n'
                    '@@\\n-old\\n+new\\n*** End Patch"; tools.apply_patch(patch)'
                ),
            },
        ),
        _line("response_item", {"type": "reasoning", "summary": []}),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    tool, edit, heartbeat = transcripts.read_events(path).events
    assert tool.tool_name == "exec"
    assert tool.tool_summary == "cmd: uv run pytest -q"
    assert tool.tool_detail == (
        "Command\nuv run pytest -q\n\nWorking directory\n/tmp"
    )
    assert edit.tool_summary == "path: src/app.py"
    assert edit.tool_detail.startswith("*** Begin Patch\n*** Update File: src/app.py")
    assert heartbeat.role == "system"
    assert heartbeat.tool_name == "reasoning"
    assert heartbeat.tool_summary == "Thinking"
    assert heartbeat.text is None


def test_codex_parses_json_quoted_exec_wrapper_fields(tmp_path):
    path = tmp_path / "rollout.jsonl"
    call = _line(
        "response_item",
        {
            "type": "custom_tool_call",
            "name": "exec",
            "input": (
                'const r = await tools.exec_command({"cmd":"sed -n \'1,20p\' app.py",'
                '"workdir":"/srv/app","yield_time_ms":10000,'
                '"max_output_tokens":12000}); text(r.output);'
            ),
        },
    )
    path.write_text(json.dumps(call) + "\n")

    (event,) = transcripts.read_events(path).events
    assert event.tool_summary == "cmd: sed -n '1,20p' app.py"
    assert event.tool_detail == (
        "Command\nsed -n '1,20p' app.py\n\n"
        "Working directory\n/srv/app\n\n"
        "Wait before update\n10 seconds\n\n"
        "Output limit\n12,000 tokens"
    )


def test_codex_filters_only_internal_system_preamble(tmp_path):
    path = tmp_path / "rollout.jsonl"
    messages = [
        _message("developer", "<permissions instructions>internal sandbox policy"),
        _message(
            "system",
            "You are /root, the primary agent in a team of agents collaborating here",
        ),
        _message("developer", "<multi_agent_mode>Do not delegate</multi_agent_mode>"),
        _message("system", "A genuine system-visible conversation message"),
        _message("user", "<permissions instructions>please explain this tag"),
    ]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages))

    visible = transcripts.read_events(path).events

    assert [(event.role, event.text) for event in visible] == [
        ("system", "A genuine system-visible conversation message"),
        ("user", "<permissions instructions>please explain this tag"),
    ]


def test_codex_strips_internal_memory_citations_from_assistant_text(tmp_path):
    path = tmp_path / "rollout.jsonl"
    answer = "Implemented and tested."
    citation = """<oai-mem-citation>
<citation_entries>
MEMORY.md:40-52|note=[internal provenance]
</citation_entries>
<rollout_ids>
019f5d1e-a09f-77a3-a062-c36b844c7675
</rollout_ids>
</oai-mem-citation>"""
    path.write_text(json.dumps(_message("assistant", f"{answer}\n\n{citation}")) + "\n")

    events = transcripts.read_events(path).events
    meta = transcripts.transcript_meta(path)

    assert [event.text for event in events] == [answer]
    assert meta.last_text == answer


def test_codex_drops_truncated_internal_memory_citation(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        json.dumps(_message("assistant", "Visible answer\n<oai-mem-citation>partial")) + "\n"
    )

    assert [event.text for event in transcripts.read_events(path).events] == ["Visible answer"]


def test_codex_skips_repository_instruction_preambles_for_title(tmp_path):
    path = tmp_path / "rollout.jsonl"
    preambles = [
        "# AGENTS.md instructions for /tmp/worktree\n\n<INSTRUCTIONS>rules</INSTRUCTIONS>",
        "<INSTRUCTIONS>\nrepository rules\n</INSTRUCTIONS>",
        "@.cursorrules\n@.cursor/memory/project-notes.mdc\n\n# Codebase map",
        "@.cursor/memory/project-notes.mdc\n\n# Project notes",
    ]
    messages = [*(_message("user", text) for text in preambles), _message("user", "Fix it")]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages))

    meta = transcripts.transcript_meta(path)
    visible = transcripts.read_events(path).events

    assert meta.title == "Fix it"
    assert meta.first_prompt == "Fix it"
    assert meta.last_prompt == "Fix it"
    assert [event.text for event in visible] == ["Fix it"]


def test_codex_hides_new_chat_orchestration_preamble_and_titles_from_prompt(tmp_path):
    path = tmp_path / "rollout.jsonl"
    messages = [
        _message("developer", "<permissions instructions>\ninternal sandbox policy"),
        _message(
            "developer",
            "You are `/root`, the primary agent in a team of agents "
            "collaborating to fulfill the user's goals.",
        ),
        _message(
            "developer",
            "<multi_agent_mode>Do not spawn sub-agents.</multi_agent_mode>",
        ),
        _message(
            "user",
            "<recommended_plugins>\nHere is a list of plugins that are available "
            "but not installed.</recommended_plugins>",
        ),
        _message("user", "Build an assistant inside AgentDeck"),
    ]
    path.write_text("".join(json.dumps(message) + "\n" for message in messages))

    meta = transcripts.transcript_meta(path)
    visible = transcripts.read_events(path).events

    assert meta.title == "Build an assistant inside AgentDeck"
    assert meta.first_prompt == "Build an assistant inside AgentDeck"
    assert meta.last_prompt == "Build an assistant inside AgentDeck"
    assert [(event.role, event.text) for event in visible] == [
        ("user", "Build an assistant inside AgentDeck")
    ]


async def test_codex_hides_launcher_source_but_preserves_exec_kind(tmp_path):
    vscode_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c07"
    exec_sid = "019f5b2b-c830-7922-a1ce-8c9c69526c08"
    vscode_path = _rollout(tmp_path, vscode_sid)
    exec_path = _rollout(tmp_path, exec_sid)
    for path, source in ((vscode_path, "vscode"), (exec_path, "exec")):
        lines = path.read_text().splitlines()
        meta = json.loads(lines[0])
        meta["payload"]["source"] = source
        lines[0] = json.dumps(meta)
        path.write_text("\n".join(lines) + "\n")

    provider = CodexProvider()
    sessions = await provider.scan_sessions(_account(tmp_path))
    found = {session.session_id: session for session in sessions}

    assert transcripts.transcript_meta(vscode_path).kind == "vscode"
    assert found[vscode_sid].kind is None
    assert found[vscode_sid].is_delegated is False
    assert found[exec_sid].kind == "exec"
    assert found[exec_sid].is_delegated is True


def test_codex_final_message_prefers_task_complete(tmp_path):
    # A delegation's final message must be Codex's canonical turn result
    # (task_complete.last_agent_message), NOT the last assistant item — otherwise
    # approval/escalation or other intermediate noise can leak in as the reported
    # result (observed: a delegation returning `{"outcome":"allow"}`).
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", "run the thing"),
        _message("assistant", "intermediate progress note"),
        _line(
            "event_msg",
            {"type": "task_complete", "last_agent_message": "THE CANONICAL ANSWER"},
        ),
        _message("assistant", ""),  # trailing noise must not win
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    meta = transcripts.transcript_meta(path)
    assert meta.last_agent_message == "THE CANONICAL ANSWER"
    # It overrides the last assistant item, which session_result falls back to.
    assert meta.last_text == "intermediate progress note"


def test_codex_final_message_falls_back_without_task_complete(tmp_path):
    # No completed turn -> fall back to the last assistant item.
    path = tmp_path / "rollout.jsonl"
    lines = [
        _message("user", "run the thing"),
        _message("assistant", "partial answer so far"),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    meta = transcripts.transcript_meta(path)
    assert meta.last_agent_message is None
    assert meta.last_text == "partial answer so far"


def test_codex_preserves_long_conversation_messages(tmp_path):
    path = tmp_path / "rollout.jsonl"
    long_user_text = "begin\n" + ("user content " * 500) + "\nuser end"
    long_assistant_text = "begin\n" + ("assistant content " * 500) + "\nassistant end"
    lines = [
        _message("user", long_user_text),
        _message("assistant", long_assistant_text),
        _line(
            "response_item",
            {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": long_assistant_text}],
            },
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))

    events = transcripts.read_events(path).events

    assert [event.text for event in events] == [
        long_user_text,
        long_assistant_text,
        long_assistant_text,
    ]


async def test_codex_sweep_refreshes_status_and_tail_metadata(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid, old=True)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    assert session.status == SessionStatus.IDLE

    new_prompt = _message("user", "One more change")
    new_prompt["timestamp"] = datetime.now(UTC).isoformat()
    with path.open("a") as handle:
        handle.write(json.dumps(new_prompt) + "\n")
    changed = provider.sweep_liveness(account, [session], AppState())
    assert changed == [session]
    assert session.status == SessionStatus.LIVE
    assert session.thinking is True
    assert session.last_prompt == "One more change"
    assert session.initial_prompt is not None
    assert session.last_role == "user"


async def test_owned_runtime_projection_matches_scan_callback_and_sweep(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    provider._clients[account.key] = client

    (session,) = await provider.scan_sessions(account)
    projected_fields = (
        session.status,
        session.thinking,
        session.activity,
        session.question,
        session.kind,
        session.capabilities,
    )

    session.status = SessionStatus.IDLE
    session.thinking = False
    session.activity = None
    session.question = "stale question"
    session.kind = None
    session.capabilities = frozenset()
    state = AppState()
    state.sessions = {session.key: session}
    provider._runtime_changed(account, state, sid)
    callback_fields = (
        session.status,
        session.thinking,
        session.activity,
        session.question,
        session.kind,
        session.capabilities,
    )

    session.status = SessionStatus.IDLE
    session.thinking = False
    session.activity = None
    session.question = "stale question"
    session.kind = None
    session.capabilities = frozenset()
    provider.sweep_liveness(account, [session], state)
    sweep_fields = (
        session.status,
        session.thinking,
        session.activity,
        session.question,
        session.kind,
        session.capabilities,
    )

    assert projected_fields == callback_fields == sweep_fields


async def test_owned_runtime_projection_uses_canonical_activity_policy(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    provider._clients[account.key] = client
    provider._cached_last_event = MagicMock(
        return_value=TranscriptEvent(
            seq=1,
            role="assistant",
            tool_name="AskUserQuestion",
        )
    )

    (session,) = await provider.scan_sessions(account)

    # A question tool is waiting, not working. The owned-runtime projection
    # must preserve the canonical activity_label verdict instead of inventing
    # a provider-local "Using AskUserQuestion" label.
    assert session.status == SessionStatus.LIVE
    assert session.activity is None


async def test_owned_runtime_projection_suppresses_controls_during_outage(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock(side_effect=OSError("runtime unavailable"))
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    provider._clients[account.key] = client

    (unavailable,) = await provider.scan_sessions(account)
    assert unavailable.capabilities == frozenset({Capability.TRANSCRIPT})

    client.refresh = AsyncMock()
    (recovered,) = await provider.scan_sessions(account)
    assert Capability.INJECT in recovered.capabilities
    assert Capability.STEER in recovered.capabilities
    assert Capability.INTERRUPT in recovered.capabilities


def test_watch_health_edges_reproject_stored_runtime_sessions(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    interaction = PendingInteraction(
        id="question-1",
        kind="question",
        thread_id="thread-1",
        turn_id="turn-1",
        title="Choose a database",
    )
    client.interaction.return_value = interaction
    provider._clients[account.key] = client
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.LIVE,
        kind="appServer",
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )
    state = AppState()
    state.sessions = {session.key: session}
    assert provider.pending_interaction(account, session) is None

    provider._refresh_ok[account.key] = False
    provider._reproject_runtime_sessions(account, state, client)
    assert session.capabilities == frozenset({Capability.TRANSCRIPT})
    assert provider.pending_interaction(account, session) is None

    provider._refresh_ok[account.key] = True
    provider._reproject_runtime_sessions(account, state, client)
    assert Capability.INJECT in session.capabilities
    assert Capability.STEER in session.capabilities
    assert Capability.INTERRUPT in session.capabilities
    assert Capability.INTERACT in session.capabilities
    assert provider.pending_interaction(account, session) == interaction


async def test_runtime_projection_is_withdrawn_when_ownership_disappears(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.refresh = AsyncMock()
    client.owns.return_value = True
    client.active_turn.return_value = "turn-1"
    client.interaction.return_value = None
    provider._clients[account.key] = client
    (session,) = await provider.scan_sessions(account)
    assert session.kind == "appServer"

    client.owns.return_value = False
    session.question = "stale runtime question"
    state = AppState()
    state.sessions = {session.key: session}
    provider._runtime_changed(account, state, sid)

    assert session.kind is None
    assert session.question is None
    assert Capability.INJECT not in session.capabilities
    assert Capability.STEER not in session.capabilities
    assert Capability.INTERRUPT not in session.capabilities

    session.kind = "appServer"
    session.capabilities = frozenset({Capability.TRANSCRIPT, Capability.INJECT})
    provider.sweep_liveness(account, [session], state)
    assert session.kind is None
    assert session.capabilities == frozenset({Capability.TRANSCRIPT})


def test_transcriptless_runtime_projection_is_withdrawn_during_sweep(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    client = MagicMock()
    client.owns.return_value = False
    provider._clients[account.key] = client
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.LIVE,
        thinking=True,
        activity="Working",
        question="stale runtime question",
        kind="appServer",
        capabilities=frozenset(
            {
                Capability.TRANSCRIPT,
                Capability.INJECT,
                Capability.STEER,
                Capability.INTERRUPT,
            }
        ),
    )

    changed = provider.sweep_liveness(account, [session], AppState())

    assert changed == [session]
    assert session.status is SessionStatus.IDLE
    assert session.thinking is False
    assert session.activity is None
    assert session.question is None
    assert session.kind is None
    assert session.capabilities == frozenset()


async def test_codex_incremental_tail_and_truncation_recovery(tmp_path):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    offset, seq = await provider.transcript_cursor(account, session)

    with path.open("a") as handle:
        handle.write(json.dumps(_message("assistant", "A live update")) + "\n")
    events, new_offset, new_seq = await provider.tail_transcript(account, session, offset, seq)
    assert [event.text for event in events] == ["A live update"]
    assert new_offset == path.stat().st_size
    assert new_seq == seq + 1

    path.write_text(json.dumps(_message("user", "After rotation")) + "\n")
    events, reset_offset, reset_seq = await provider.tail_transcript(
        account, session, new_offset, new_seq
    )
    assert [event.text for event in events] == ["After rotation"]
    assert reset_offset == path.stat().st_size
    assert reset_seq == 1


async def test_codex_full_reads_run_in_thread(tmp_path, monkeypatch):
    sid = "019f5b2b-c830-7922-a1ce-8c9c69526c06"
    path = _rollout(tmp_path, sid)
    provider = CodexProvider()
    account = _account(tmp_path)
    (session,) = await provider.scan_sessions(account)
    direct_detail = provider._load_transcript_file(path, None)
    calls: list[str] = []
    original_to_thread = provider_mod.asyncio.to_thread

    async def tracking_to_thread(func, /, *args, **kwargs):
        calls.append(func.__name__)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(provider_mod.asyncio, "to_thread", tracking_to_thread)
    assert await provider.transcript_cursor(account, session) == transcripts.transcript_cursor(path)
    direct_events = [event for event in transcripts.read_events(path).events if event.seq > 8]
    provider_events = await provider.read_transcript(account, session, after_seq=8)
    assert provider_events == provider._apply_model(direct_events, "gpt-5.6-sol")
    assert await provider.load_transcript(account, session) == direct_detail
    assert calls == ["transcript_cursor", "_read_transcript_file", "_load_transcript_file"]


def test_codex_usage_poller_and_registration(tmp_path):
    provider = CodexProvider()
    assert provider.make_usage_poller(_account(tmp_path), object(), object()) is not None
    assert PROVIDERS["codex"].provider_id == "codex"
    config = AccountConfig(provider="codex", label="local", config_dir=str(tmp_path))
    assert config.root == tmp_path


async def test_codex_compact_command_uses_owned_runtime(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.IDLE,
    )
    client = MagicMock()
    client.compact = AsyncMock(return_value=InjectResult(True))
    client.queue_turn = AsyncMock()
    client.owns.return_value = True
    provider._clients[account.key] = client

    result = await provider.inject(
        account,
        session,
        "/compact",
        timeout_s=30,
    )

    assert result.accepted
    assert not result.transcript_expected
    client.compact.assert_awaited_once_with("thread-1")
    client.queue_turn.assert_not_awaited()


async def test_codex_compact_rejects_args_images_and_unowned_sessions(tmp_path):
    provider = CodexProvider()
    account = _account(tmp_path)
    session = Session(
        key=f"{account.key}:thread-1",
        account_key=account.key,
        session_id="thread-1",
        status=SessionStatus.IDLE,
    )
    client = MagicMock()
    client.compact = AsyncMock()
    client.owns.return_value = True
    provider._clients[account.key] = client

    with_args = await provider.inject(
        account, session, "/compact now", timeout_s=30
    )
    with_image = await provider.inject(
        account,
        session,
        "/compact",
        timeout_s=30,
        images=[tmp_path / "screen.png"],
    )
    client.owns.return_value = False
    unowned = await provider.inject(account, session, "/compact", timeout_s=30)

    assert with_args.reason == "usage: /compact"
    assert with_image.reason == "/compact does not accept image attachments"
    assert "AgentDeck-owned" in unowned.reason
    client.compact.assert_not_awaited()
