from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

from agentdeck.models import Account
from agentdeck.providers.claude_code.worker import ClaudeWorkerHost
from agentdeck.providers.instructions import FILE_PRESENTATION_INSTRUCTIONS


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
        while not any(line.get("type") == "control_request" for line in proc.stdin.lines):
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


async def test_command_enables_stdio_permission_prompt(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    args = spawned[0]["args"]
    assert "--permission-prompt-tool" in args
    assert args[args.index("--permission-prompt-tool") + 1] == "stdio"
    await host.stop()


async def test_command_explains_file_preview_and_download_links(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    args = spawned[0]["args"]
    assert args[args.index("--append-system-prompt") + 1] == (
        FILE_PRESENTATION_INSTRUCTIONS
    )
    await host.stop()


async def test_askuserquestion_control_request_becomes_pending_and_answers(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-42",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "display_name": "AskUserQuestion",
                "requires_user_interaction": True,
                "input": {
                    "questions": [
                        {
                            "question": "Pick a fruit",
                            "header": "Fruit",
                            "multiSelect": False,
                            "options": [
                                {"label": "apple", "description": "a"},
                                {"label": "banana", "description": "b"},
                            ],
                        }
                    ]
                },
            },
        }
    )
    await _settle()
    # snapshot ships the normalized (provider-neutral) interaction, not raw wire
    pending = host.snapshot()["workers"]["issue-1"]["pending_interaction"]
    assert pending["id"] == "req-42"
    assert pending["kind"] == "question"
    assert pending["questions"][0]["prompt"] == "Pick a fruit"

    result = await host.answer(
        "issue-1", "req-42", answers={"0": ["banana"]}, decision="accept"
    )
    assert result.accepted and result.action == "answered"
    written = proc.stdin.lines[-1]
    assert written["type"] == "control_response"
    resp = written["response"]["response"]
    assert resp["behavior"] == "allow"
    assert resp["updatedInput"]["answers"] == {"Pick a fruit": "banana"}
    # cleared after answering
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    await host.stop()


async def test_permission_gate_allow_and_deny(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]

    def emit_gate(rid):
        proc.stdout.emit(
            {
                "type": "control_request",
                "request_id": rid,
                "request": {
                    "subtype": "can_use_tool",
                    "tool_name": "Bash",
                    "display_name": "Bash",
                    "description": "delete a file",
                    "input": {"command": "rm x"},
                },
            }
        )

    emit_gate("g1")
    await _settle()
    await host.answer("issue-1", "g1", answers={}, decision="accept")
    assert proc.stdin.lines[-1]["response"]["response"] == {
        "behavior": "allow",
        "updatedInput": {"command": "rm x"},
    }

    emit_gate("g2")
    await _settle()
    await host.answer("issue-1", "g2", answers={}, decision="cancel")
    denied = proc.stdin.lines[-1]["response"]["response"]
    assert denied["behavior"] == "deny" and denied["interrupt"] is True
    await host.stop()


async def test_answer_rejects_unknown_interaction(tmp_path):
    host, _ = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    result = await host.answer("issue-1", "nope", answers={}, decision="accept")
    assert not result.accepted and result.reason == "interaction_not_pending"
    await host.stop()


async def test_result_event_clears_pending_interaction(tmp_path):
    # An interrupt/finish (result event) while a question is pending must drop it,
    # else the widget lingers and a late answer targets an abandoned request.
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "q?", "header": "h", "options": [
                    {"label": "a"}, {"label": "b"}]}]},
            },
        }
    )
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is not None
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()
    assert host.snapshot()["workers"]["issue-1"]["pending_interaction"] is None
    # a late answer for the abandoned request is now rejected
    result = await host.answer("issue-1", "req-1", answers={"0": ["a"]}, decision="accept")
    assert not result.accepted and result.reason == "interaction_not_pending"
    await host.stop()


async def test_background_agent_keeps_worker_effectively_active_after_result(tmp_path):
    host, spawned = _host(tmp_path)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]
    transcript = tmp_path / "cfg" / "projects" / "project" / "sid-auto-1.jsonl"
    transcript.parent.mkdir(parents=True)

    def append_event(event: dict) -> None:
        with transcript.open("a") as stream:
            stream.write(json.dumps(event) + "\n")

    for agent_id in ("agent-1", "agent-2"):
        append_event(
            {
                "type": "user",
                "toolUseResult": {
                    "isAsync": True,
                    "status": "async_launched",
                    "agentId": agent_id,
                },
            }
        )
    proc.stdout.emit({"type": "result", "subtype": "success"})
    await _settle()

    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["turn_active"] is False
    assert worker["background_agent_count"] == 2
    assert worker["effective_activity"] is True

    append_event(
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "<task-notification>\n"
                "<task-id>agent-1</task-id>\n"
                "<task-id>agent-2</task-id>\n"
                "<status>stopped</status>\n"
                "</task-notification>"
            ),
        }
    )
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["background_agent_count"] == 0
    assert worker["effective_activity"] is False

    append_event(
        {
            "type": "user",
            "toolUseResult": {
                "success": True,
                "resumedAgentId": "agent-1",
            },
        }
    )
    worker = host.snapshot()["workers"]["issue-1"]
    assert worker["background_agent_count"] == 1
    assert worker["effective_activity"] is True
    await host.stop()


async def test_worker_waiting_on_interaction_is_not_stalled(tmp_path):
    host, spawned = _host(tmp_path, stall_after_s=100.0)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    proc = spawned[0]["process"]
    proc.stdout.emit(
        {
            "type": "control_request",
            "request_id": "req-1",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "AskUserQuestion",
                "input": {"questions": [{"question": "q?", "header": "h", "options": [
                    {"label": "a"}, {"label": "b"}]}]},
            },
        }
    )
    await _settle()
    # backdate so an ordinary open turn would read as stalled
    host._records["issue-1"].last_delivery_at -= 200
    host._live["issue-1"].last_event_at -= 200
    assert host.snapshot()["workers"]["issue-1"]["stalled"] is False
    await host.stop()


async def test_interactive_prompts_disabled_omits_stdio_flag(tmp_path):
    host, spawned = _host(tmp_path, interactive_prompts=False)
    await host.deliver("issue-1", "start", cwd=str(tmp_path))
    assert "--permission-prompt-tool" not in spawned[0]["args"]
    await host.stop()
async def test_reader_crash_reaps_child(tmp_path):
    # If the stdout reader dies with the child still alive (e.g. an oversized
    # line), the detached process must be terminated — not left as an orphan that
    # a later deliver would --resume a second time on the same transcript.
    class CrashingStdout:
        def __init__(self):
            self.queue: asyncio.Queue = asyncio.Queue()

        def emit(self, event: dict) -> None:
            self.queue.put_nowait(json.dumps(event).encode() + b"\n")

        async def readline(self) -> bytes:
            item = await self.queue.get()
            if item == "BOOM":
                raise ValueError("stdout line exceeded limit")
            return item

    processes: list[FakeProcess] = []

    async def factory(*args, **fkwargs):
        proc = FakeProcess()
        proc.stdout = CrashingStdout()
        processes.append(proc)
        asyncio.get_running_loop().call_soon(
            proc.stdout.emit,
            {"type": "system", "subtype": "init", "session_id": f"sid-{len(processes)}"},
        )
        return proc

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    result = await host.deliver("issue-1", "go", cwd=str(tmp_path))
    assert result.accepted
    await _settle()
    reader_task = host._live["issue-1"].reader_task
    proc = processes[0]
    proc.stdout.queue.put_nowait("BOOM")  # crash the reader
    await asyncio.gather(reader_task, return_exceptions=True)
    assert proc.returncode is not None  # reaped, not orphaned
    assert proc.stdin.closed
    assert "issue-1" not in host._live
    # Session lineage kept so the next deliver revives a single owner.
    assert host._records["issue-1"].session_id == "sid-1"


def _entry(pid: int, session_id: str, proc_start: str = "42"):
    from agentdeck.providers.claude_code.registry import RegistryEntry

    return RegistryEntry(
        pid=pid,
        session_id=session_id,
        cwd=None,
        started_at=None,
        proc_start=proc_start,
        version=None,
        kind=None,
        entrypoint=None,
    )


def _seed_state(tmp_path: Path, session_id: str | None) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "test.json").write_text(
        json.dumps(
            {"workers": [{"key": "issue-9", "cwd": str(tmp_path), "session_id": session_id}]}
        )
    )
    return state_dir


async def test_reconcile_kills_orphan_at_startup(tmp_path, monkeypatch):
    from agentdeck.providers.claude_code import worker as worker_mod

    state_dir = _seed_state(tmp_path, "sid-orphan")
    orphan = [_entry(4242, "sid-orphan")]
    monkeypatch.setattr(worker_mod.registry, "read_registry", lambda root: orphan)
    monkeypatch.setattr(worker_mod.registry, "is_alive", lambda e: True)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=state_dir,
    )
    assert killed == [(4242, worker_mod.signal.SIGKILL)]
    # The record survives the reconcile so a later deliver can revive it.
    assert host._records["issue-9"].session_id == "sid-orphan"


async def test_reconcile_ignores_unowned_and_dead(tmp_path, monkeypatch):
    from agentdeck.providers.claude_code import worker as worker_mod

    state_dir = _seed_state(tmp_path, "sid-orphan")
    entries = [_entry(1, "sid-other"), _entry(2, "sid-orphan")]  # unowned + owned
    monkeypatch.setattr(worker_mod.registry, "read_registry", lambda root: entries)
    monkeypatch.setattr(worker_mod.registry, "is_alive", lambda e: e.pid != 2)  # owned one is dead
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_mod.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=state_dir,
    )
    assert killed == []  # sid-other not ours; sid-orphan pid is dead


async def test_reconcile_noop_without_sessions(tmp_path, monkeypatch):
    from agentdeck.providers.claude_code import worker as worker_mod

    state_dir = _seed_state(tmp_path, None)  # record without a session id
    called = {"read": False}

    def _read(root):
        called["read"] = True
        return []

    monkeypatch.setattr(worker_mod.registry, "read_registry", _read)
    ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=state_dir,
    )
    assert called["read"] is False  # no owned session ids → registry never read


# --- replay spine: at-most-once delivery + durable, time-bounded receipts -----


async def test_receipt_is_persisted_before_the_child_write(tmp_path):
    # The receipt for a delivery id must be durable *before* the message is
    # written, so a crash in the write→confirm window replays as already-handled
    # instead of re-delivering. Snapshot the receipt at the moment of the write.
    holder: dict = {}
    seen_at_write: list[dict | None] = []

    class SnoopStdin(FakeStdin):
        def write(self, data: bytes) -> None:
            rec = holder["host"]._records.get("issue-1")
            seen_at_write.append(dict(rec.deliveries.get("d1", {})) if rec else None)
            super().write(data)

    async def factory(*args, **fkwargs):
        proc = FakeProcess()
        proc.stdin = SnoopStdin()
        asyncio.get_running_loop().call_soon(
            proc.stdout.emit,
            {"type": "system", "subtype": "init", "session_id": "sid-x"},
        )
        return proc

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    holder["host"] = host
    result = await host.deliver("issue-1", "go", cwd=str(tmp_path), delivery_id="d1")
    assert result.accepted
    # A receipt already existed at write time, in the *prepared* state (no action).
    assert seen_at_write and seen_at_write[0].get("fingerprint")
    assert "action" not in seen_at_write[0]
    # After success it is finalized with the real action.
    assert host._records["issue-1"].deliveries["d1"]["action"] == "spawned"


async def test_prepared_receipt_replays_uncertain_without_rewriting(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver("issue-1", "go", cwd=str(tmp_path), delivery_id="d1")
    assert first.accepted
    proc = spawned[0]["process"]
    writes_before = len(proc.stdin.lines)
    # Simulate a crash that persisted the prepared receipt but never finalized it.
    host._records["issue-1"].deliveries["d1"].pop("action")
    # Replaying the same delivery id must not write a second time.
    replay = await host.deliver("issue-1", "go", cwd=str(tmp_path), delivery_id="d1")
    assert replay.accepted and replay.action == "uncertain"
    assert len(proc.stdin.lines) == writes_before


async def test_delivery_receipts_are_time_bounded_not_fifo_64(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver("issue-1", "m0", cwd=str(tmp_path), delivery_id="d0")
    assert first.action == "spawned"
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid"})
    await _settle()
    # 69 further deliveries — well past the old 64-entry FIFO that would have
    # evicted d0's receipt.
    for i in range(1, 70):
        r = await host.deliver("issue-1", f"m{i}", cwd=str(tmp_path), delivery_id=f"d{i}")
        assert r.accepted
    writes = len(proc.stdin.lines)
    assert "d0" in host._records["issue-1"].deliveries  # still remembered
    # Replaying the very first id dedups (cached), rather than re-delivering.
    replay = await host.deliver("issue-1", "m0", cwd=str(tmp_path), delivery_id="d0")
    assert replay.accepted and replay.action == "spawned"
    assert len(proc.stdin.lines) == writes


async def test_clean_write_failure_keeps_delivery_retryable(tmp_path):
    host, spawned = _host(tmp_path)
    first = await host.deliver("issue-1", "go", cwd=str(tmp_path), delivery_id="d1")
    assert first.accepted
    proc = spawned[0]["process"]
    proc.stdout.emit({"type": "system", "subtype": "init", "session_id": "sid"})
    await _settle()

    def boom(data: bytes) -> None:
        raise BrokenPipeError("pipe")

    proc.stdin.write = boom  # the steer write fails cleanly — nothing sent
    second = await host.deliver("issue-1", "steer", cwd=str(tmp_path), delivery_id="d2")
    assert not second.accepted
    # A cleanly-failed write leaves no receipt, so the id can be retried.
    assert "d2" not in host._records["issue-1"].deliveries


async def test_fresh_delivery_replay_dedups_to_one_spawn(tmp_path):
    # New-chat (issue #37) delivers with fresh=True + a delivery id. A retry with
    # the same id must dedup at the real host — one spawn, cached result — NOT
    # terminate-and-respawn a second worker via the fresh path. (End-to-end proof
    # that the fresh=True + repeated delivery_id combination collapses to one.)
    host, spawned = _host(tmp_path)
    first = await host.deliver(
        "chat-x", "hi", cwd=str(tmp_path), fresh=True, delivery_id="act-1"
    )
    assert first.accepted and first.action == "spawned"
    await _settle()
    replay = await host.deliver(
        "chat-x", "hi", cwd=str(tmp_path), fresh=True, delivery_id="act-1"
    )
    assert replay.accepted and replay.action == "spawned"  # cached original result
    assert len(spawned) == 1  # not respawned despite fresh=True


async def test_reuploaded_image_paths_do_not_break_dedup(tmp_path):
    # A retried image new-chat re-uploads identical bytes to a NEW random path;
    # the delivery fingerprint must ignore the path so the retry dedups instead of
    # falsely tripping delivery_id_conflict.
    host, spawned = _host(tmp_path)
    (tmp_path / "a.png").write_bytes(b"img")
    (tmp_path / "b.png").write_bytes(b"img")
    first = await host.deliver(
        "chat-y", "look", cwd=str(tmp_path), fresh=True,
        images=[str(tmp_path / "a.png")], delivery_id="act-2",
    )
    assert first.accepted and first.action == "spawned"
    await _settle()
    replay = await host.deliver(
        "chat-y", "look", cwd=str(tmp_path), fresh=True,
        images=[str(tmp_path / "b.png")], delivery_id="act-2",  # same bytes, new path
    )
    assert replay.accepted and replay.action == "spawned"  # deduped, not conflict
    assert len(spawned) == 1


async def test_uncertain_replay_reports_live_session_id(tmp_path):
    # A fresh spawn's receipt is frozen at prepare time with session_id=None. If it
    # is left prepared-but-unconfirmed (crash before finalize), the uncertain replay
    # must report the now-live rec.session_id, not the stale None.
    host, _ = _host(tmp_path)
    await host.deliver("chat-z", "go", cwd=str(tmp_path), fresh=True, delivery_id="act-3")
    await _settle()
    rec = host._records["chat-z"]
    assert rec.session_id  # populated from the worker's system/init
    receipt = rec.deliveries["act-3"]
    receipt.pop("action", None)   # simulate prepared-but-unconfirmed
    receipt["session_id"] = None  # frozen before the session existed
    replay = await host.deliver(
        "chat-z", "go", cwd=str(tmp_path), fresh=True, delivery_id="act-3"
    )
    assert replay.action == "uncertain"
    assert replay.session_id == rec.session_id  # live session, not None


async def test_revive_reuses_spawn_permission_mode(tmp_path):
    # A follow-up that revives a finished worker must reuse the mode it was spawned
    # with — not escalate to the host default. Spawn a restrictive "plan" chat under
    # a bypassPermissions host default, let it exit, then revive with no explicit
    # mode and assert the revive still uses "plan".
    host, spawned = _host(tmp_path, permission_mode="bypassPermissions")
    await host.deliver(
        "chat-p", "go", cwd=str(tmp_path), fresh=True, permission_mode="plan"
    )
    await _settle()
    args0 = spawned[0]["args"]
    assert args0[args0.index("--permission-mode") + 1] == "plan"

    # Worker exits → the next deliver revives it.
    proc = spawned[0]["process"]
    proc.returncode = 0
    proc.stdout.queue.put_nowait(b"")  # end the reader loop → dropped from _live
    await _settle()

    await host.deliver("chat-p", "again", cwd=str(tmp_path))  # no mode, not fresh
    await _settle()
    assert len(spawned) == 2  # revived, not steered
    args1 = spawned[1]["args"]
    assert "--resume" in args1  # it is a revive
    assert args1[args1.index("--permission-mode") + 1] == "plan"  # not bypassPermissions


async def test_write_then_init_timeout_is_uncertain_not_resent(tmp_path, monkeypatch):
    # If the message is written but the child then fails to initialize by TIMEOUT
    # (still possibly alive/mid-turn), we must NOT re-send it (that could run the
    # task twice). It returns uncertain, spawns exactly once, and a same-id retry
    # replays uncertain without a second spawn.
    from agentdeck.providers.claude_code import worker as worker_mod

    monkeypatch.setattr(worker_mod, "INIT_TIMEOUT_S", 0.1)
    processes: list[FakeProcess] = []

    async def factory(*args, **kwargs):
        proc = FakeProcess()  # alive, never emits init, never exits → init times out
        processes.append(proc)
        return proc

    host = ClaudeWorkerHost(
        Account("claude_code:test", "claude_code", "test", tmp_path / "cfg"),
        state_dir=tmp_path / "state",
        process_factory=factory,
    )
    result = await host.deliver("chat-w", "go", cwd=str(tmp_path), fresh=True, delivery_id="w1")
    assert result.action == "uncertain"
    assert len(processes) == 1  # exactly one spawn — not re-sent to a fresh worker
    assert processes[0].stdin.lines  # the message was written once
    assert "w1" in host._records["chat-w"].deliveries  # receipt kept, not forgotten

    replay = await host.deliver("chat-w", "go", cwd=str(tmp_path), fresh=True, delivery_id="w1")
    assert replay.action == "uncertain"
    assert len(processes) == 1  # same-id retry still does not re-send
    await host.stop()
