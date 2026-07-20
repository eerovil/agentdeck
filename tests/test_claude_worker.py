from __future__ import annotations

import asyncio
import json
from datetime import datetime
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
        session_id = f"sid-auto-{len(spawned)}"
        asyncio.get_running_loop().call_soon(
            process.stdout.emit,
            {"type": "system", "subtype": "init", "session_id": session_id},
        )
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
    assert result.session_id == "sid-auto-1"
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


async def test_delivery_id_replay_is_deduplicated_across_restart(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1"
    )
    replay = await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1"
    )

    assert first.accepted and replay == first
    assert len(spawned) == 1
    assert len(spawned[0]["process"].stdin.lines) == 1
    await host.stop()

    reloaded, spawned_after_restart = _host(tmp_path)
    replay_after_restart = await reloaded.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1"
    )
    assert replay_after_restart == first
    assert spawned_after_restart == []


async def test_delivery_id_rejects_conflicting_payload(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver(
        "issue-1", "start", cwd=str(tmp_path), delivery_id="attempt-1"
    )

    conflict = await host.deliver(
        "issue-1", "different", cwd=str(tmp_path), delivery_id="attempt-1"
    )

    assert not conflict.accepted
    assert conflict.reason == "delivery_id_conflict"
    assert len(spawned[0]["process"].stdin.lines) == 1
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


async def test_failed_resume_falls_back_to_fresh_spawn(tmp_path):
    original, spawned = _host(tmp_path)
    await original.deliver("issue-1", "start", cwd=str(tmp_path))
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-old"}
    )
    await _settle()
    await original.stop()

    attempts = []

    async def factory(*args, **kwargs):
        process = FakeProcess()
        attempts.append({"args": args, "process": process})
        if "--resume" in args:
            asyncio.get_running_loop().call_soon(process.stdout.queue.put_nowait, b"")
        else:
            asyncio.get_running_loop().call_soon(
                process.stdout.emit,
                {"type": "system", "subtype": "init", "session_id": "sid-fresh"},
            )
        return process

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    result = await host.deliver("issue-1", "recover")

    assert result.accepted and result.action == "spawned"
    assert len(attempts) == 2
    assert "--resume" in attempts[0]["args"]
    assert "--resume" not in attempts[1]["args"]
    assert result.session_id == "sid-fresh"
    await host.stop()


async def test_fresh_spawns_without_resume(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    spawned[0]["process"].stdout.emit(
        {"type": "system", "subtype": "init", "session_id": "sid-abc"}
    )
    await _settle()
    result = await host.deliver("issue-1", "clean slate", cwd=str(tmp_path), fresh=True)
    assert result.accepted and result.action == "spawned"
    assert "--resume" not in spawned[1]["args"]
    assert spawned[0]["process"].returncode == 0
    assert host.snapshot()["live_count"] == 1
    await host.stop()


async def test_image_delivery_encodes_anthropic_content_block(tmp_path):
    host, spawned = _host(tmp_path)
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    result = await host.deliver(
        "issue-1", "inspect it", cwd=str(tmp_path), images=[str(image)]
    )

    assert result.accepted
    content = spawned[0]["process"].stdin.lines[0]["message"]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[1] == {"type": "text", "text": "inspect it"}
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


async def test_idle_worker_is_evicted_to_admit_new_chat(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    # issue-1's turn finishes: its process stays alive but is now idle, so a
    # second chat must be admitted by reclaiming that idle slot, not rejected.
    spawned[0]["process"].stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["turn_active"] is False

    admitted = await host.deliver("issue-2", "start", cwd=str(tmp_path))
    assert admitted.accepted and admitted.action == "spawned"
    assert len(spawned) == 2
    snap = host.snapshot()
    assert snap["live_count"] == 1
    assert snap["workers"]["issue-1"]["live"] is False  # evicted, record kept
    assert spawned[0]["process"].returncode == 0
    await host.stop()


async def test_capacity_rejects_when_all_workers_mid_turn(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    # issue-1 never emits a result, so its turn stays active and there is no
    # idle slot to reclaim — a new chat is still rejected.
    rejected = await host.deliver("issue-2", "start", cwd=str(tmp_path))
    assert not rejected.accepted and rejected.reason == "at_capacity"
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


async def test_interrupt_returns_at_once_when_worker_exits_before_ack(tmp_path):
    # Regression: if the process exits before a control_response arrives, the
    # interrupt waiter must be resolved by the reader's teardown rather than
    # hanging for the full INTERRUPT_TIMEOUT_S.
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]

    async def exit_before_ack():
        while not any(l.get("type") == "control_request" for l in proc.stdin.lines):
            await asyncio.sleep(0)
        proc.returncode = 0
        proc.stdout.queue.put_nowait(b"")  # EOF ends the read loop -> teardown

    task = asyncio.create_task(exit_before_ack())
    result = await asyncio.wait_for(host.interrupt("issue-1"), timeout=1.0)
    await task
    assert not result.accepted and result.reason == "worker_exited"
    await host.stop()


async def test_snapshot_excludes_exited_worker_before_reader_cleanup(tmp_path):
    # Regression: an exited process still lingering in _live (reader not yet
    # drained) must read as live=False / live_count=0, not live=True.
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]
    proc.returncode = 0  # exited, but no EOF emitted yet -> still in _live
    snap = host.snapshot()
    assert snap["workers"]["issue-1"]["live"] is False
    assert snap["workers"]["issue-1"]["turn_active"] is False
    assert snap["live_count"] == 0
    proc.stdout.queue.put_nowait(b"")  # let the reader finish
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


async def test_park_frees_capacity_and_next_delivery_revives(tmp_path):
    host, spawned = _host(tmp_path, max_workers=1)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))

    parked = await host.park_worker("issue-1")
    parked_again = await host.park_worker("issue-1")

    assert parked.accepted and parked.action == "parked"
    assert parked_again.accepted
    assert host.snapshot()["live_count"] == 0
    assert "issue-1" in host.snapshot()["workers"]

    revived = await host.deliver("issue-1", "continue")
    assert revived.accepted and revived.action == "revived"
    assert "--resume" in spawned[1]["args"]
    await host.stop()


async def test_park_unknown_key_is_idempotently_accepted(tmp_path):
    host, _ = _host(tmp_path)
    result = await host.park_worker("already-gone")
    assert result.accepted and result.action == "parked"


async def test_release_is_idempotent_and_forgets_lineage(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))

    released = await host.release_worker("issue-1")
    released_again = await host.release_worker("issue-1")

    assert released.accepted and released.action == "released"
    assert released_again.accepted and released_again.action == "released"
    assert spawned[0]["process"].returncode == 0
    assert "issue-1" not in host.snapshot()["workers"]


async def test_worker_command_includes_configured_flags(tmp_path):
    host, spawned = _host(tmp_path, permission_mode="acceptEdits", model="haiku")
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    args = spawned[0]["args"]
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--model") + 1] == "haiku"
    await host.stop()


async def test_over_budget_rejects_spawn_but_not_steer(tmp_path):
    host, spawned = _host(tmp_path, usage_ceiling_pct=90.0, usage_reader=lambda: 95.0)
    # live worker first (spawned while a lower reader would allow) — flip after.
    host._usage_reader = lambda: 10.0
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    host._usage_reader = lambda: 95.0  # now over ceiling
    # steering the live worker is exempt
    steered = await host.deliver("issue-1", "keep going")
    assert steered.accepted
    # a NEW key spawn is rejected
    rejected = await host.deliver("issue-2", "start", cwd=str(tmp_path))
    assert not rejected.accepted and rejected.reason == "over_budget"
    assert len(spawned) == 1
    await host.stop()


async def test_over_budget_fresh_replacement_keeps_existing_worker(tmp_path):
    host, spawned = _host(tmp_path, usage_ceiling_pct=90.0, usage_reader=lambda: 10.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    host._usage_reader = lambda: 95.0

    rejected = await host.deliver("issue-1", "replace", fresh=True)

    assert not rejected.accepted and rejected.reason == "over_budget"
    assert spawned[0]["process"].returncode is None
    assert host.snapshot()["live_count"] == 1
    await host.stop()


async def test_under_ceiling_allows_spawn(tmp_path):
    host, spawned = _host(tmp_path, usage_ceiling_pct=90.0, usage_reader=lambda: 42.0)
    result = await host.deliver("issue-1", "start", cwd=str(tmp_path))
    assert result.accepted and result.action == "spawned"
    snap = host.snapshot()
    assert snap["usage_pct"] == 42.0 and snap["over_budget"] is False
    await host.stop()


async def test_unknown_usage_does_not_block(tmp_path):
    host, spawned = _host(tmp_path, usage_ceiling_pct=90.0, usage_reader=lambda: None)
    result = await host.deliver("issue-1", "start", cwd=str(tmp_path))
    assert result.accepted
    assert host.snapshot()["over_budget"] is False
    await host.stop()


async def test_reads_published_usage_file_with_staleness(tmp_path):
    import json as _json
    import time as _time

    cache = tmp_path / "usage-cache"
    cache.mkdir()
    host, _ = _host(tmp_path, usage_ceiling_pct=90.0, usage_cache_dir=cache)
    host._usage_reader = host._read_published_usage  # exercise the real reader

    def write(pct, age_s):
        (cache / "usage-test.json").write_text(
            _json.dumps(
                {
                    "fetched_at": datetime.fromtimestamp(_time.time() - age_s).isoformat(),
                    "five_hour_pct": pct,
                    "seven_day_pct": 1.0,
                }
            )
        )

    write(95.0, age_s=10)
    assert host._read_published_usage() == 95.0
    assert host._over_budget() is True
    write(95.0, age_s=9999)  # stale → unknown → not blocking
    assert host._read_published_usage() is None
    assert host._over_budget() is False


async def test_stalled_flag_after_threshold(tmp_path):
    host, spawned = _host(tmp_path, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    # backdate the delivery so the live turn looks silent
    host._records["issue-1"].last_delivery_at -= 200
    host._live["issue-1"].last_event_at -= 200
    assert host.snapshot()["workers"]["issue-1"]["stalled"] is True
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
