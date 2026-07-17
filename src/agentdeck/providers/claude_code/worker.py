"""Deck-owned Claude Code worker processes (stream-json control channel).

Each worker is one long-lived ``claude -p --input-format stream-json`` process.
The host keys workers by an opaque *dedupe key* and exposes one idempotent
primitive — ``deliver(key, message)`` — which steers a live worker, revives a
finished session (``--resume``), or spawns a fresh one. Callers (e.g. an
external board poller) never track process state themselves.

Transcripts are written by the CLI into ``<account root>/projects/``, so the
ordinary ClaudeCodeProvider scan picks deck-owned workers up with no extra
plumbing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ...models import Account

log = logging.getLogger(__name__)

# One JSON event per stdout line; large tool results can exceed asyncio's
# default 64 KiB StreamReader limit (same rationale as the Codex app-server).
STDOUT_LINE_LIMIT = 16 * 1024 * 1024

INTERRUPT_TIMEOUT_S = 15.0


class WorkerError(RuntimeError):
    """A Claude worker process could not complete a request."""


@dataclass
class DeliverResult:
    accepted: bool
    action: str  # "spawned" | "revived" | "steered" | "queued" | "rejected"
    reason: str | None = None
    session_id: str | None = None


@dataclass
class WorkerRecord:
    """Durable per-key state — survives runtime restarts via the state file."""

    key: str
    cwd: str
    session_id: str | None = None
    last_delivery_at: float = 0.0
    last_result_at: float = 0.0
    last_result_subtype: str | None = None


@dataclass
class _LiveWorker:
    process: asyncio.subprocess.Process
    reader_task: asyncio.Task
    turn_active: bool = False
    started_at: float = field(default_factory=time.time)


class ClaudeWorkerHost:
    """Own all deck-managed Claude worker processes for one account."""

    def __init__(
        self,
        account: Account,
        *,
        state_dir: Path,
        max_workers: int = 4,
        permission_mode: str | None = None,
        model: str | None = None,
        process_factory=None,
    ) -> None:
        self.account = account
        self.max_workers = max_workers
        self.permission_mode = permission_mode
        self.model = model
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._state_path = state_dir / f"{account.label}.json"
        self._records: dict[str, WorkerRecord] = {}
        self._live: dict[str, _LiveWorker] = {}
        self._interrupt_waiters: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._load_state()

    # --- state persistence -------------------------------------------------

    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        for item in raw.get("workers", []):
            try:
                rec = WorkerRecord(**item)
            except TypeError:
                continue
            self._records[rec.key] = rec

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workers": [vars(rec) for rec in self._records.values()]}
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        tmp.replace(self._state_path)

    # --- public API --------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        workers = {}
        for key, rec in self._records.items():
            live = self._live.get(key)
            workers[key] = {
                "session_id": rec.session_id,
                "cwd": rec.cwd,
                "live": live is not None,
                "turn_active": bool(live and live.turn_active),
                "last_delivery_at": rec.last_delivery_at,
                "last_result_at": rec.last_result_at,
                "last_result_subtype": rec.last_result_subtype,
            }
        return {"workers": workers, "live_count": len(self._live)}

    async def deliver(
        self,
        key: str,
        message: str,
        *,
        cwd: str | None = None,
        fresh: bool = False,
    ) -> DeliverResult:
        """Idempotent work delivery: steer if live, revive if finished, else spawn."""
        async with self._lock:
            rec = self._records.get(key)
            live = self._live.get(key)

            if live is not None and live.process.returncode is None and not fresh:
                await self._write_user_message(live, message)
                rec.last_delivery_at = time.time()
                self._save_state()
                action = "steered" if live.turn_active else "queued"
                live.turn_active = True
                return DeliverResult(True, action, session_id=rec.session_id)

            # Not live (or fresh requested) — this is a (re)spawn: capacity applies.
            running = [w for w in self._live.values() if w.process.returncode is None]
            if len(running) >= self.max_workers:
                return DeliverResult(False, "rejected", reason="at_capacity")

            if rec is None or fresh or rec.cwd != (cwd or (rec.cwd if rec else None)):
                if cwd is None and rec is None:
                    return DeliverResult(False, "rejected", reason="cwd_required")
                rec = WorkerRecord(key=key, cwd=cwd or rec.cwd)
                self._records[key] = rec

            resume_id = None if fresh else rec.session_id
            try:
                live = await self._spawn(key, rec, resume_id=resume_id)
            except WorkerError as exc:
                if resume_id is not None:
                    # Revive fallback: session gone/incompatible → fresh spawn.
                    log.warning("revive of %s failed (%s); spawning fresh", key, exc)
                    rec.session_id = None
                    live = await self._spawn(key, rec, resume_id=None)
                else:
                    return DeliverResult(False, "rejected", reason=str(exc))

            await self._write_user_message(live, message)
            live.turn_active = True
            rec.last_delivery_at = time.time()
            self._save_state()
            action = "revived" if resume_id is not None else "spawned"
            return DeliverResult(True, action, session_id=rec.session_id)

    async def interrupt(self, key: str) -> DeliverResult:
        live = self._live.get(key)
        if live is None or live.process.returncode is not None:
            return DeliverResult(False, "rejected", reason="not_live")
        request_id = f"int-{uuid.uuid4().hex[:8]}"
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._interrupt_waiters[request_id] = waiter
        try:
            await self._write(
                live,
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": "interrupt"},
                },
            )
            await asyncio.wait_for(waiter, timeout=INTERRUPT_TIMEOUT_S)
        except TimeoutError:
            return DeliverResult(False, "rejected", reason="interrupt_timeout")
        finally:
            self._interrupt_waiters.pop(request_id, None)
        rec = self._records.get(key)
        return DeliverResult(True, "interrupted", session_id=rec.session_id if rec else None)

    async def stop_worker(self, key: str) -> DeliverResult:
        live = self._live.pop(key, None)
        if live is None:
            return DeliverResult(False, "rejected", reason="not_live")
        await self._terminate(live)
        rec = self._records.get(key)
        return DeliverResult(True, "stopped", session_id=rec.session_id if rec else None)

    def forget(self, key: str) -> bool:
        """Drop a finished key's record (its session lineage) from the registry."""
        if key in self._live:
            return False
        removed = self._records.pop(key, None) is not None
        if removed:
            self._save_state()
        return removed

    async def stop(self) -> None:
        live = list(self._live.values())
        self._live.clear()
        await asyncio.gather(*(self._terminate(w) for w in live), return_exceptions=True)

    # --- process management ------------------------------------------------

    def _command(self, resume_id: str | None) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if resume_id:
            cmd += ["--resume", resume_id]
        return cmd

    async def _spawn(self, key: str, rec: WorkerRecord, *, resume_id: str | None) -> _LiveWorker:
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(self.account.root)
        try:
            process = await self._process_factory(
                *self._command(resume_id),
                cwd=rec.cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                limit=STDOUT_LINE_LIMIT,
            )
        except (OSError, ValueError) as exc:
            raise WorkerError(f"could not start claude worker: {exc}") from exc
        live = _LiveWorker(process=process, reader_task=None)  # type: ignore[arg-type]
        live.reader_task = asyncio.create_task(
            self._read_loop(key, live), name=f"claude-worker:{self.account.key}:{key}"
        )
        self._live[key] = live
        return live

    async def _terminate(self, live: _LiveWorker) -> None:
        process = live.process
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if live.reader_task is not None and live.reader_task is not asyncio.current_task():
            live.reader_task.cancel()
            await asyncio.gather(live.reader_task, return_exceptions=True)

    async def _write(self, live: _LiveWorker, message: dict[str, Any]) -> None:
        process = live.process
        if process.stdin is None or process.returncode is not None:
            raise WorkerError("worker process is not running")
        process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await process.stdin.drain()

    async def _write_user_message(self, live: _LiveWorker, text: str) -> None:
        await self._write(
            live,
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            },
        )

    async def _read_loop(self, key: str, live: _LiveWorker) -> None:
        process = live.process
        assert process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                self._handle_event(key, live, event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- reader failure only affects one worker
            log.exception("claude worker reader failed for %s:%s", self.account.key, key)
        finally:
            if self._live.get(key) is live:
                del self._live[key]
                live.turn_active = False

    def _handle_event(self, key: str, live: _LiveWorker, event: dict[str, Any]) -> None:
        etype = event.get("type")
        rec = self._records.get(key)
        if etype == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if rec is not None and isinstance(session_id, str):
                rec.session_id = session_id
                self._save_state()
        elif etype == "result":
            live.turn_active = False
            if rec is not None:
                rec.last_result_at = time.time()
                subtype = event.get("subtype")
                rec.last_result_subtype = subtype if isinstance(subtype, str) else None
                self._save_state()
        elif etype == "control_response":
            response = event.get("response")
            request_id = response.get("request_id") if isinstance(response, dict) else None
            waiter = self._interrupt_waiters.get(request_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(response)
