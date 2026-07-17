from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agentdeck.models import Account
from agentdeck.providers.claude_code.worker import ClaudeWorkerHost


class FakeStdin:
    def __init__(self):
        self.lines: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.lines.append(json.loads(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeStdout:
    def __init__(self):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    def emit(self, event: dict) -> None:
        self.queue.put_nowait(json.dumps(event).encode() + b"\n")

    async def readline(self) -> bytes:
        return await self.queue.get()


class FakeProcess:
    def __init__(self):
        self.returncode: int | None = None
        self.stdin = FakeStdin()
        self.stdout = FakeStdout()

    def terminate(self) -> None:
        self.returncode = 0
        self.stdout.queue.put_nowait(b"")

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def _host(tmp_path: Path, **kwargs) -> tuple[ClaudeWorkerHost, list]:
    spawned: list[dict] = []

    async def factory(*args, **fkwargs):
        process = FakeProcess()
        spawned.append({"args": args, "kwargs": fkwargs, "process": process})
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
        **kwargs,
    )
    return host, spawned


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_deliver_requires_cwd_for_new_key(tmp_path):
    host, _ = _host(tmp_path)
    result = await host.deliver("issue-1", "work it")
    assert not result.accepted
    assert result.reason == "cwd_required"


async def test_deliver_spawns_and_captures_session_id(tmp_path):
    host, spawned = _host(tmp_path)
    result = await host.deliver("issue-1", "work it", cwd=str(tmp_path))
    assert result.accepted and result.action == "spawned"
    proc = spawned[0]["process"]
    assert spawned[0]["kwargs"]["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")
    assert spawned[0]["kwargs"]["cwd"] == str(tmp_path)
    assert proc.stdin.lines[0]["message"]["content"][0]["text"] == "work it"

    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-123"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["session_id"] == "sid-123"
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is True

    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is False
    await host.stop()


async def test_deliver_to_live_worker_steers_without_new_spawn(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    result = await host.deliver("issue-1", "change course")
    assert result.accepted and result.action == "steered"
    assert len(spawned) == 1
    assert spawned[0]["process"].stdin.lines[1]["message"]["content"][0]["text"] == "change course"
    await host.stop()


async def test_deliver_after_exit_revives_with_resume(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid-abc"})
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")  # EOF → reader drops the live entry
    await _settle()

    result = await host.deliver("issue-1", "follow-up")
    assert result.accepted and result.action == "revived"
    assert "--resume" in spawned[1]["args"]
    assert spawned[1]["args"][spawned[1]["args"].index("--resume") + 1] == "sid-abc"
    await host.stop()


async def test_fresh_spawns_without_resume(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-abc"}
    )
    await _settle()
    result = await host.deliver("issue-1", "clean slate", cwd=str(tmp_path), fresh=True)
    assert result.accepted and result.action == "spawned"
    assert "--resume" not in spawned[1]["args"]
    await host.stop()


async def test_capacity_rejects_new_spawns_but_not_steering(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    rejected = await host.deliver("issue-2", "start", cwd=str(tmp_path))
    assert not rejected.accepted and rejected.reason == "at_capacity"
    steered = await host.deliver("issue-1", "still fine")
    assert steered.accepted
    assert len(spawned) == 1
    await host.stop()


async def test_interrupt_round_trip(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]

    async def respond():
        while not any(line.get("type") == "control_request" for line in proc.stdin.lines):
            await asyncio.sleep(0)
        req = next(line for line in proc.stdin.lines if line["type"] == "control_request")
        proc.stdout.emit(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": req["request_id"]},
            }
        )

    responder = asyncio.create_task(respond())
    result = await host.interrupt("issue-1")
    await responder
    assert result.accepted and result.action == "interrupted"
    assert proc.stdin.lines[-1]["request"]["subtype"] == "interrupt"
    await host.stop()


async def test_state_persists_across_host_restarts(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-xyz"}
    )
    await _settle()
    await host.stop()

    reloaded, spawned2 = _host(tmp_path)
    result = await reloaded.deliver("issue-1", "resume me")
    assert result.accepted and result.action == "revived"
    assert "--resume" in spawned2[0]["args"]
    await reloaded.stop()


async def test_worker_command_includes_configured_flags(tmp_path):
    host, spawned = _host(tmp_path, permission_mode="acceptEdits", model="haiku")
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    args = spawned[0]["args"]
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--model") + 1] == "haiku"
    await host.stop()


async def test_forget_drops_finished_records_only(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    assert host.forget("issue-1") is False  # still live
    proc = spawned[0]["process"]
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")
    await _settle()
    assert host.forget("issue-1") is True
    assert "issue-1" not in host.snapshot()["workers"]
    await host.stop()
