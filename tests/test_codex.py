from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agentdeck.config import AccountConfig
from agentdeck.models import Account, Capability, SessionStatus
from agentdeck.providers import PROVIDERS
from agentdeck.providers.codex import provider as provider_mod
from agentdeck.providers.codex import transcripts
from agentdeck.providers.codex.provider import CodexProvider


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
    usage_events = [event for event in detail.events if event.usage]
    assert usage_events[-1].usage["cached_input_tokens"] == 30
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
    assert meta.last_prompt == "Fix it"
    assert [event.text for event in visible] == ["Fix it"]


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
    assert found[exec_sid].kind == "exec"


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

    with path.open("a") as handle:
        handle.write(json.dumps(_message("user", "One more change")) + "\n")
    changed = provider.sweep_liveness(account, [session])
    assert changed == [session]
    assert session.status == SessionStatus.LIVE
    assert session.thinking is True
    assert session.last_prompt == "One more change"
    assert session.last_role == "user"


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
