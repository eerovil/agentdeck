"""Synthetic fixtures — a fake CLAUDE_CONFIG_DIR + fake /proc tree.

No real Claude data is ever read or copied; everything here is hand-built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _proc_stat_line(pid: int, starttime: str) -> str:
    """A /proc/<pid>/stat line whose field 22 (starttime) == ``starttime``.

    registry.proc_starttime splits after the last ')', so index 19 of the
    remainder is field 22. We build 30 placeholder fields and pin index 19.
    """
    fields = [str(i) for i in range(30)]
    fields[0] = "S"  # field 3: state
    fields[19] = starttime  # field 22: starttime
    return f"{pid} (claude) " + " ".join(fields)


@pytest.fixture
def make_proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()

    def _make(pid: int, starttime: str) -> None:
        d = root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(_proc_stat_line(pid, starttime))

    _make.root = root  # type: ignore[attr-defined]
    return _make


@pytest.fixture
def fake_config_dir(tmp_path):
    """Build a minimal synthetic $CLAUDE_CONFIG_DIR and return its path.

    Contains: one live-registered session, one idle transcript-only session,
    a history.jsonl with titles, and a project transcript file.
    """
    cfg = tmp_path / "cfg"
    (cfg / "sessions").mkdir(parents=True)
    (cfg / "projects" / "-tmp-proj").mkdir(parents=True)

    live_sid = "11111111-1111-1111-1111-111111111111"
    idle_sid = "22222222-2222-2222-2222-222222222222"

    # live registry entry (pid 4242, starttime "19")
    (cfg / "sessions" / "4242.json").write_text(
        json.dumps(
            {
                "pid": 4242,
                "sessionId": live_sid,
                "cwd": "/tmp/proj",
                "startedAt": "2026-07-03T08:00:00Z",
                "procStart": "19",
                "version": "2.1.198",
                "kind": "interactive",
                "entrypoint": "sdk-cli",
            }
        )
    )
    # a malformed registry file (must be skipped, never fatal)
    (cfg / "sessions" / "bad.json").write_text("{not json")

    # transcripts for both sessions
    for sid in (live_sid, idle_sid):
        line = {"type": "user", "sessionId": sid, "message": {"role": "user", "content": "hi"}}
        (cfg / "projects" / "-tmp-proj" / f"{sid}.jsonl").write_text(json.dumps(line) + "\n")

    # history.jsonl: title = first prompt, last_prompt = most recent
    lines = [
        {"display": "First prompt", "project": "/tmp/proj", "sessionId": live_sid, "timestamp": 1},
        {"display": "Second prompt", "project": "/tmp/proj", "sessionId": live_sid, "timestamp": 2},
        {"display": "Idle title", "project": "/tmp/proj", "sessionId": idle_sid, "timestamp": 3},
        "{ malformed",
    ]
    (cfg / "history.jsonl").write_text(
        "\n".join(x if isinstance(x, str) else json.dumps(x) for x in lines)
    )

    cfg_obj = type("Cfg", (), {})()
    cfg_obj.path = cfg
    cfg_obj.live_sid = live_sid
    cfg_obj.idle_sid = idle_sid
    return cfg_obj


@pytest.fixture
def write_credentials():
    def _write(config_dir: Path, token: str) -> None:
        (config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"accessToken": token}})
        )

    return _write
