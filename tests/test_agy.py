from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentdeck.models import Account, Capability
from agentdeck.providers.agy import AgyProvider, transcripts


def test_clean_user_content():
    raw = (
        "<USER_REQUEST>\nlook at agentdeck\n</USER_REQUEST>\n"
        "<ADDITIONAL_METADATA>\nsome data\n</ADDITIONAL_METADATA>"
    )
    cleaned = transcripts.clean_user_content(raw)
    assert cleaned == "look at agentdeck"


def test_clean_user_content_fallback():
    raw = "hello world"
    assert transcripts.clean_user_content(raw) == "hello world"


@pytest.mark.asyncio
async def test_agy_scan_sessions(tmp_path: Path):
    root = tmp_path / "antigravity-cli"
    conv_dir = root / "brain" / "test-conv-123" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True)

    transcript_file = conv_dir / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-07-24T04:06:32Z",
            "content": "<USER_REQUEST>\nTest prompt for AGY\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-07-24T04:06:33Z",
            "content": "I am working on it.",
            "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
        },
    ]
    with transcript_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    account = Account(key="agy:main", provider_id="agy", label="main", root=root)
    provider = AgyProvider()

    sessions = await provider.scan_sessions(account)
    assert len(sessions) == 1

    session = sessions[0]
    assert session.session_id == "test-conv-123"
    assert session.account_key == "agy:main"
    assert session.initial_prompt == "Test prompt for AGY"
    assert session.last_text == "I am working on it."
    assert Capability.TRANSCRIPT in session.capabilities


@pytest.mark.asyncio
async def test_agy_read_transcript(tmp_path: Path):
    root = tmp_path / "antigravity-cli"
    conv_dir = root / "brain" / "test-conv-456" / ".system_generated" / "logs"
    conv_dir.mkdir(parents=True)

    transcript_file = conv_dir / "transcript.jsonl"
    lines = [
        {
            "step_index": 0,
            "source": "USER_EXPLICIT",
            "type": "USER_INPUT",
            "status": "DONE",
            "created_at": "2026-07-24T04:06:32Z",
            "content": "First message",
        },
        {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-07-24T04:06:33Z",
            "content": "Response text",
        },
    ]
    with transcript_file.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item) + "\n")

    account = Account(key="agy:main", provider_id="agy", label="main", root=root)
    provider = AgyProvider()

    sessions = await provider.scan_sessions(account)
    events = await provider.read_transcript(account, sessions[0])

    assert len(events) == 2
    assert events[0].role == "user"
    assert events[0].text == "First message"
    assert events[1].role == "assistant"
    assert events[1].text == "Response text"
