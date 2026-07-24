"""Deck-owned Claude Code worker processes (stream-json control channel).

Each worker is one long-lived ``claude -p --input-format stream-json`` process.
The host keys workers by an opaque *dedupe key* and exposes one idempotent
primitive — ``deliver(key, delivery_id, message)`` — which steers a live worker, revives a
finished session (``--resume``), or spawns a fresh one. Callers (e.g. an
external board poller) never track process state themselves.

Transcripts are written by the CLI into ``<account root>/projects/``, so the
ordinary ClaudeCodeProvider scan picks deck-owned workers up with no extra
plumbing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ...images import SUPPORTED_IMAGE_MEDIA_TYPES
from ...models import Account
from ..instructions import FILE_PRESENTATION_INSTRUCTIONS
from . import registry
from .delivery import (
    DeliverResult,
    DeliveryReceipts,
)

log = logging.getLogger(__name__)

# One JSON event per stdout line; large tool results can exceed asyncio's
# default 64 KiB StreamReader limit (same rationale as the Codex app-server).
STDOUT_LINE_LIMIT = 16 * 1024 * 1024

INTERRUPT_TIMEOUT_S = 15.0
INIT_TIMEOUT_S = 30.0

# Published usage snapshots older than this are treated as unknown rather than
# trusted — a stalled collector must not permanently block (or greenlight) work.
USAGE_MAX_AGE_S = 1800.0


class WorkerError(RuntimeError):
    """A Claude worker process could not complete a request.

    ``write_started`` marks a failure that happened *after* the user message was
    already written to the child — the payload may have been delivered, so the
    caller must not re-send it (that would run the task twice)."""

    def __init__(self, *args: object, write_started: bool = False) -> None:
        super().__init__(*args)
        self.write_started = write_started


@dataclass
class WorkerRecord:
    """Durable per-key state — survives runtime restarts via the state file."""

    key: str
    cwd: str
    session_id: str | None = None
    last_delivery_at: float = 0.0
    last_result_at: float = 0.0
    last_result_subtype: str | None = None
    # The permission mode the worker was first spawned with, reused on every
    # revive so a follow-up can't silently change a chat's policy.
    permission_mode: str | None = None
    deliveries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def receipts(self) -> DeliveryReceipts:
        """At-most-once fence over this record's durable ``deliveries`` dict. A
        property (not a field) so it stays out of ``vars()`` and the on-disk
        state shape is unchanged."""
        return DeliveryReceipts(self.deliveries)


@dataclass
class _LiveWorker:
    process: asyncio.subprocess.Process
    reader_task: asyncio.Task
    initialized: asyncio.Future[str]
    turn_active: bool = False
    started_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    # request_ids of interrupts awaiting a control_response on this worker, so
    # the reader can fail them fast if the process exits before acking.
    pending_interrupts: set[str] = field(default_factory=set)
    # The current interactive `can_use_tool` control_request the agent is blocked
    # on (AskUserQuestion or a permission gate), or None. Its `request_id` is the
    # interaction id the web layer answers against. Only one is open at a time —
    # the turn is paused until we write the matching control_response.
    pending_interaction: dict[str, Any] | None = None


def _preview_tool_input(tool_input: object) -> str | None:
    """A compact one-line preview of a permission-gated tool's key input, so a
    non-Bash approval (Write/WebFetch/…) isn't answered blind."""
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "path", "url", "pattern", "query", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{key}: {value}"[:200]
    return None


def _normalize_interaction(
    pending: object, session_id: str | None
) -> dict[str, Any] | None:
    """Map a raw `can_use_tool` control_request to the provider-neutral
    PendingInteraction shape the shared widget renders. Lives in the runtime (which
    owns the CLI control channel) so the web process never parses Claude's wire
    schema — symmetric to how the Codex app-server normalizes before the socket."""
    if not isinstance(pending, dict):
        return None
    request_id = pending.get("request_id")
    if not isinstance(request_id, str):
        return None
    tool_name = pending.get("tool_name")
    tool_input = pending.get("input") if isinstance(pending.get("input"), dict) else {}
    if tool_name == "AskUserQuestion":
        questions = []
        for index, q in enumerate(tool_input.get("questions") or []):
            if not isinstance(q, dict) or not isinstance(q.get("question"), str):
                continue
            options = [
                {"label": o["label"], "description": o.get("description") or ""}
                for o in (q.get("options") or [])
                if isinstance(o, dict) and isinstance(o.get("label"), str)
            ]
            questions.append(
                {
                    "id": str(index),
                    "header": q.get("header") or "",
                    "prompt": q["question"],
                    "options": options,
                    "allow_other": True,  # Claude always lets you write your own
                    "multiselect": bool(q.get("multiSelect")),
                }
            )
        return {
            "id": request_id,
            "kind": "question",
            "thread_id": session_id or "",
            "title": "Claude is asking",
            "questions": questions,
        }
    description = pending.get("description")
    command = tool_input.get("command") if isinstance(tool_input.get("command"), str) else None
    message = (
        description
        if isinstance(description, str) and description
        else _preview_tool_input(tool_input)
    )
    return {
        "id": request_id,
        "kind": "permission",
        "thread_id": session_id or "",
        "title": f"Allow {pending.get('display_name') or tool_name or 'this tool'}?",
        "message": message,
        "command": command,
        # No "acceptForSession": our control_response can't durably grant a
        # session-scoped rule, so we don't offer a button that wouldn't be honored.
        "decisions": ["accept", "decline", "cancel"],
    }


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
        usage_ceiling_pct: float = 0.0,
        usage_cache_dir: Path | None = None,
        stall_after_s: float = 0.0,
        interactive_prompts: bool = True,
        process_factory=None,
        usage_reader=None,
    ) -> None:
        self.account = account
        self.max_workers = max_workers
        self.permission_mode = permission_mode
        self.model = model
        # Whether to spawn with `--permission-prompt-tool stdio` (surfaces
        # AskUserQuestion + permission gates to the user). Config escape hatch:
        # set false to revert to the old auto-run spawn without a code change.
        self.interactive_prompts = interactive_prompts
        self.usage_ceiling_pct = usage_ceiling_pct
        self.stall_after_s = stall_after_s
        self._usage_cache_dir = usage_cache_dir
        self._usage_reader = usage_reader or self._read_published_usage
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._state_path = state_dir / f"{account.label}.json"
        self._records: dict[str, WorkerRecord] = {}
        self._live: dict[str, _LiveWorker] = {}
        self._interrupt_waiters: dict[str, asyncio.Future] = {}
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._load_state()
        self._reconcile_orphans()

    def _key_lock(self, key: str) -> asyncio.Lock:
        """Per-key serialization lock.

        Deliveries and lifecycle ops (stop/park/release) on ONE key are mutually
        exclusive, but operations on *different* keys run concurrently — so
        stopping a stalled worker never blocks behind another key's up-to-30s
        spawn. Host-wide admission (``max_workers``) is therefore advisory under
        this scheme; a transient off-by-one at the cap is immaterial on a single
        host and never destroys a mid-turn worker (the fresh-replace path checks
        admission before terminating anything, and idle-worker eviction only
        reclaims a slot from a worker with no active turn, skipping any whose key
        lock a concurrent delivery already holds).
        """
        lock = self._key_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[key] = lock
        return lock

    # --- admission policy --------------------------------------------------

    def _read_published_usage(self) -> float | None:
        """Max(5h, 7d) utilization % from the shared usage cache, or None if
        missing/unparseable/stale — the collector publishes ``usage-<label>.json``."""
        if self._usage_cache_dir is None:
            return None
        try:
            raw = json.loads(
                (self._usage_cache_dir / f"usage-{self.account.label}.json").read_text()
            )
        except (OSError, ValueError):
            return None
        fetched = raw.get("fetched_at")
        if isinstance(fetched, str):
            try:
                age = time.time() - datetime.fromisoformat(fetched).timestamp()
            except ValueError:
                age = None
            if age is not None and age > USAGE_MAX_AGE_S:
                return None
        pcts = [raw.get("five_hour_pct"), raw.get("seven_day_pct")]
        known = [p for p in pcts if isinstance(p, (int, float))]
        return max(known) if known else None

    def _over_budget(self) -> bool:
        if self.usage_ceiling_pct <= 0:
            return False
        pct = self._usage_reader()
        return pct is not None and pct >= self.usage_ceiling_pct

    async def _evict_idle_worker(self, admit_key: str) -> str | None:
        """Free one admission slot by terminating the least-recently-used *idle*
        live worker (process alive, no active turn), so a new chat is never
        blocked by finished-but-resident sessions. Returns the evicted key, or
        None when every other live worker is mid-turn.

        The victim's durable record (incl. ``session_id``) survives, so its chat
        revives on the next delivery. Never *waits* on another key's lock: a
        victim currently being delivered to is skipped rather than risk racing a
        fresh turn or deadlocking against a concurrent eviction.
        """

        def _recency(item: tuple[str, _LiveWorker]) -> float:
            key, live = item
            rec = self._records.get(key)
            return max(live.last_event_at, rec.last_delivery_at if rec else 0.0)

        candidates = sorted(
            (
                (key, live)
                for key, live in self._live.items()
                if key != admit_key
                and live.process.returncode is None
                and not live.turn_active
            ),
            key=_recency,
        )
        for victim_key, victim in candidates:
            lock = self._key_locks.get(victim_key)
            if lock is not None and lock.locked():
                continue  # a concurrent delivery owns it; don't race its turn
            # No await between the locked() check and acquire(): atomic on the loop.
            if lock is not None:
                await lock.acquire()
            try:
                current = self._live.get(victim_key)
                if (
                    current is not victim
                    or current.process.returncode is not None
                    or current.turn_active
                ):
                    continue  # raced: state changed under us, try the next one
                self._live.pop(victim_key, None)
            finally:
                if lock is not None:
                    lock.release()
            log.info(
                "evicted idle worker %s:%s to admit %s",
                self.account.key,
                victim_key,
                admit_key,
            )
            await self._terminate(victim)
            return victim_key
        return None

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

    def _reconcile_orphans(self) -> None:
        """Kill workers left running by a previously-crashed runtime incarnation.

        After an ungraceful exit (OOM/``kill -9``) the lifespan shutdown never
        ran, so detached worker process groups may still be alive and registered
        under ``<root>/sessions/<pid>.json`` even though our in-memory ``_live``
        map starts empty. Their stdio belonged to the dead runtime, so they can't
        be re-adopted, and the next ``deliver`` for a key would ``--resume`` a
        *second* process on the same transcript. Any still-alive process whose
        session id matches one of our records is therefore killed; the record
        (session lineage) is kept so the next deliver revives cleanly as the sole
        owner.
        """
        owned = {rec.session_id for rec in self._records.values() if rec.session_id}
        if not owned:
            return
        try:
            entries = registry.read_registry(self.account.root)
        except Exception:  # noqa: BLE001 -- reconcile must never break host init
            log.exception("orphan reconcile: registry read failed (%s)", self.account.key)
            return
        for entry in entries:
            if entry.session_id not in owned or not registry.is_alive(entry):
                continue
            # SIGKILL, not SIGTERM-then-wait: these are not our children (we can't
            # reap them) and must be gone before any revive; the JSONL transcript
            # tolerates a truncated trailing line on --resume. Workers are spawned
            # start_new_session=True, so pid is the group leader (pgid == pid).
            try:
                os.killpg(entry.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
            log.warning(
                "orphan reconcile: killed stale worker pid=%s session=%s (%s)",
                entry.pid,
                entry.session_id,
                self.account.key,
            )

    # --- public API --------------------------------------------------------

    def find_key_by_session(self, session_id: str) -> str | None:
        """Return the worker key whose session lineage matches, if any."""
        for key, rec in self._records.items():
            if rec.session_id == session_id:
                return key
        return None

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        workers = {}
        for key, rec in self._records.items():
            live = self._live.get(key)
            alive = live is not None and live.process.returncode is None
            turn_active = bool(alive and live.turn_active)
            pending = live.pending_interaction if alive else None
            stalled = (
                turn_active
                # A worker blocked on a pending interaction is waiting on the user,
                # not hung — don't badge it as stalled.
                and pending is None
                and self.stall_after_s > 0
                and (now - max(rec.last_delivery_at, live.last_event_at))
                > self.stall_after_s
            )
            workers[key] = {
                "session_id": rec.session_id,
                "cwd": rec.cwd,
                "live": alive,
                "turn_active": turn_active,
                "stalled": stalled,
                "last_delivery_at": rec.last_delivery_at,
                "last_result_at": rec.last_result_at,
                "last_result_subtype": rec.last_result_subtype,
                # Normalized here (in the runtime that owns the CLI control
                # channel) so the web side never parses raw Claude wire schema.
                "pending_interaction": _normalize_interaction(pending, rec.session_id),
            }
        return {
            "workers": workers,
            "live_count": sum(
                1 for w in self._live.values() if w.process.returncode is None
            ),
            "usage_pct": self._usage_reader(),
            "over_budget": self._over_budget(),
        }

    async def deliver(
        self,
        key: str,
        message: str,
        *,
        cwd: str | None = None,
        fresh: bool = False,
        images: list[str] | None = None,
        model: str | None = None,
        permission_mode: str | None = None,
        delivery_id: str | None = None,
    ) -> DeliverResult:
        """Idempotent work delivery: steer if live, revive if finished, else spawn.

        ``delivery_id`` is optional for interactive callers, but durable dispatchers
        should always provide one. An accepted id is persisted with a fingerprint and
        its original result before the HTTP response is returned, so an ambiguous retry
        cannot write the same message twice or replace a successfully spawned worker.
        """
        fingerprint = DeliveryReceipts.fingerprint(
            message,
            cwd=cwd,
            fresh=fresh,
            images=images or [],
            model=model,
            permission_mode=permission_mode,
        )
        async with self._key_lock(key):
            rec = self._records.get(key)
            cached = (
                rec.receipts.lookup(delivery_id, fingerprint, live_session_id=rec.session_id)
                if rec is not None
                else None
            )
            if cached is not None:
                return cached
            try:
                image_blocks = [
                    await asyncio.to_thread(self._image_block, path) for path in images or []
                ]
            except WorkerError as exc:
                return DeliverResult(False, "rejected", reason=str(exc))
            live = self._live.get(key)

            if live is not None and live.process.returncode is None and not fresh:
                rec.receipts.prepare(delivery_id, fingerprint, session_id=rec.session_id)
                self._save_state()
                try:
                    await self._write_user_message(live, message, image_blocks)
                except WorkerError as exc:
                    if rec.receipts.forget(delivery_id, write_started=exc.write_started):
                        self._save_state()
                    return DeliverResult(False, "rejected", reason=str(exc))
                rec.last_delivery_at = time.time()
                action = "steered" if live.turn_active else "queued"
                live.turn_active = True
                result = DeliverResult(True, action, session_id=rec.session_id)
                rec.receipts.finalize(delivery_id, fingerprint, result)
                self._save_state()
                return result

            admission_checked = False
            # A fresh delivery replaces the existing process atomically. Check
            # admission first so a rejected replacement never destroys the
            # healthy worker it was meant to replace.
            if live is not None:
                running = [
                    worker
                    for worker in self._live.values()
                    if worker is not live and worker.process.returncode is None
                ]
                if len(running) >= self.max_workers and (
                    await self._evict_idle_worker(admit_key=key) is None
                ):
                    return DeliverResult(False, "rejected", reason="at_capacity")
                if self._over_budget():
                    return DeliverResult(False, "rejected", reason="over_budget")
                admission_checked = True
                self._live.pop(key, None)
                await self._terminate(live)

            # Not live (or fresh requested) — this is a (re)spawn: admission
            # policy applies here only. Steering a live worker is always allowed
            # so in-flight work finishes even at the cap or over budget.
            if not admission_checked:
                running = [w for w in self._live.values() if w.process.returncode is None]
                if len(running) >= self.max_workers and (
                    await self._evict_idle_worker(admit_key=key) is None
                ):
                    return DeliverResult(False, "rejected", reason="at_capacity")
                if self._over_budget():
                    return DeliverResult(False, "rejected", reason="over_budget")

            if rec is None or fresh or rec.cwd != (cwd or (rec.cwd if rec else None)):
                if cwd is None and rec is None:
                    return DeliverResult(False, "rejected", reason="cwd_required")
                rec = WorkerRecord(key=key, cwd=cwd or rec.cwd)
                self._records[key] = rec

            resume_id = None if fresh else rec.session_id
            resumed = resume_id is not None
            # Reuse the spawn-time permission mode on a revive so a follow-up that
            # brings back a finished worker can't silently change policy (e.g.
            # escalate a manual "default" chat to bypassPermissions); a fresh spawn
            # records its mode for future revives.
            if resumed and rec.permission_mode is not None:
                spawn_permission_mode = rec.permission_mode
            else:
                spawn_permission_mode = permission_mode
                rec.permission_mode = permission_mode
            rec.receipts.prepare(delivery_id, fingerprint, session_id=resume_id)
            self._save_state()
            try:
                live = await self._spawn_and_deliver(
                    key,
                    rec,
                    message,
                    image_blocks,
                    resume_id=resume_id,
                    model=model,
                    permission_mode=spawn_permission_mode,
                )
            except WorkerError as exc:
                if exc.write_started:
                    # The message already reached a worker; re-sending it (or fresh
                    # -spawning and sending again) would run the task twice. Keep the
                    # prepared receipt so a same-id retry replays as uncertain, and
                    # never re-send — progress requires a new delivery id.
                    return DeliverResult(True, "uncertain", session_id=rec.session_id)
                if resume_id is not None:
                    # Revive failed *before* the write → nothing was sent, so a fresh
                    # spawn is safe.
                    log.warning("revive of %s failed (%s); spawning fresh", key, exc)
                    rec.session_id = None
                    resumed = False
                    self._save_state()
                    try:
                        live = await self._spawn_and_deliver(
                            key,
                            rec,
                            message,
                            image_blocks,
                            resume_id=None,
                            model=model,
                            permission_mode=spawn_permission_mode,
                        )
                    except WorkerError as fallback_exc:
                        if fallback_exc.write_started:
                            return DeliverResult(
                                True, "uncertain", session_id=rec.session_id
                            )
                        if rec.receipts.forget(
                            delivery_id, write_started=fallback_exc.write_started
                        ):
                            self._save_state()
                        return DeliverResult(False, "rejected", reason=str(fallback_exc))
                else:
                    if rec.receipts.forget(delivery_id, write_started=exc.write_started):
                        self._save_state()
                    return DeliverResult(False, "rejected", reason=str(exc))

            rec.last_delivery_at = time.time()
            action = "revived" if resumed else "spawned"
            result = DeliverResult(True, action, session_id=rec.session_id)
            rec.receipts.finalize(delivery_id, fingerprint, result)
            self._save_state()
            return result

    async def interrupt(self, key: str) -> DeliverResult:
        live = self._live.get(key)
        if live is None or live.process.returncode is not None:
            return DeliverResult(False, "rejected", reason="not_live")
        request_id = f"int-{uuid.uuid4().hex[:8]}"
        waiter: asyncio.Future = asyncio.get_running_loop().create_future()
        self._interrupt_waiters[request_id] = waiter
        live.pending_interrupts.add(request_id)
        try:
            await self._write(
                live,
                {
                    "type": "control_request",
                    "request_id": request_id,
                    "request": {"subtype": "interrupt"},
                },
            )
            # The reader resolves the waiter with the control_response, or with
            # None if the process exits first (see _read_loop) — so a turn that
            # finishes concurrently returns immediately instead of hanging.
            response = await asyncio.wait_for(waiter, timeout=INTERRUPT_TIMEOUT_S)
        except TimeoutError:
            return DeliverResult(False, "rejected", reason="interrupt_timeout")
        except WorkerError as exc:
            return DeliverResult(False, "rejected", reason=str(exc))
        finally:
            self._interrupt_waiters.pop(request_id, None)
            live.pending_interrupts.discard(request_id)
        if response is None:
            return DeliverResult(False, "rejected", reason="worker_exited")
        rec = self._records.get(key)
        return DeliverResult(True, "interrupted", session_id=rec.session_id if rec else None)

    async def answer(
        self,
        key: str,
        interaction_id: str,
        *,
        answers: dict[str, list[str]],
        decision: str | None,
    ) -> DeliverResult:
        """Resolve the worker's open `can_use_tool` control_request.

        ``answers`` is keyed by the question's positional id ("0", "1", …). For
        AskUserQuestion an "allow" carries the selected labels back as the
        ``answers`` map (question text → answer string) inside ``updatedInput``;
        for a permission gate the decision maps to allow/deny.
        """
        live = self._live.get(key)
        if live is None or live.process.returncode is not None:
            return DeliverResult(False, "rejected", reason="not_live")
        pending = live.pending_interaction
        if pending is None or pending.get("request_id") != interaction_id:
            return DeliverResult(False, "rejected", reason="interaction_not_pending")
        response = self._build_tool_decision(pending, answers, decision)
        try:
            await self._write(
                live,
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": interaction_id,
                        "response": response,
                    },
                },
            )
        except WorkerError as exc:
            return DeliverResult(False, "rejected", reason=str(exc))
        live.pending_interaction = None
        rec = self._records.get(key)
        return DeliverResult(True, "answered", session_id=rec.session_id if rec else None)

    @staticmethod
    def _build_tool_decision(
        pending: dict[str, Any],
        answers: dict[str, list[str]],
        decision: str | None,
    ) -> dict[str, Any]:
        tool_name = pending.get("tool_name")
        tool_input = pending.get("input") if isinstance(pending.get("input"), dict) else {}
        deny = decision in ("decline", "cancel")
        if tool_name == "AskUserQuestion":
            if deny:
                return {"behavior": "deny", "message": "The user dismissed the questions."}
            questions = tool_input.get("questions")
            answer_map: dict[str, str] = {}
            if isinstance(questions, list):
                for index, question in enumerate(questions):
                    values = answers.get(str(index)) or []
                    if not isinstance(question, dict) or not values:
                        continue
                    text = question.get("question")
                    if isinstance(text, str):
                        answer_map[text] = ", ".join(values)
            return {"behavior": "allow", "updatedInput": {**tool_input, "answers": answer_map}}
        # Permission gate on a regular tool.
        if deny:
            deny_body: dict[str, Any] = {"behavior": "deny", "message": "Denied by the user."}
            if decision == "cancel":
                deny_body["interrupt"] = True
            return deny_body
        return {"behavior": "allow", "updatedInput": tool_input}

    async def stop_worker(self, key: str) -> DeliverResult:
        async with self._key_lock(key):
            live = self._live.pop(key, None)
            if live is None:
                return DeliverResult(False, "rejected", reason="not_live")
            await self._terminate(live)
            rec = self._records.get(key)
            return DeliverResult(True, "stopped", session_id=rec.session_id if rec else None)

    async def park_worker(self, key: str) -> DeliverResult:
        """Terminate a process while retaining resumable session lineage.

        Parking is idempotent: an already parked known key is accepted so callers
        can safely retry after losing the response.
        """
        async with self._key_lock(key):
            rec = self._records.get(key)
            live = self._live.pop(key, None)
            if live is not None:
                await self._terminate(live)
            # Unknown is already safely parked. Accept it so a caller that lost
            # the original response (or survived state repair) never deadlocks
            # its own durable lifecycle transition.
            return DeliverResult(
                True, "parked", session_id=rec.session_id if rec else None
            )

    async def release_worker(self, key: str) -> DeliverResult:
        """Terminate and forget a key atomically; safe to retry after success."""
        async with self._key_lock(key):
            rec = self._records.get(key)
            live = self._live.pop(key, None)
            if live is not None:
                await self._terminate(live)
            if rec is not None:
                self._records.pop(key, None)
                self._save_state()
            return DeliverResult(
                True, "released", session_id=rec.session_id if rec else None
            )

    def forget(self, key: str) -> bool:
        """Drop a finished key's record (its session lineage) from the registry."""
        if key in self._live:
            return False
        removed = self._records.pop(key, None) is not None
        self._key_locks.pop(key, None)
        if removed:
            self._save_state()
        return removed

    async def stop(self) -> None:
        live = list(self._live.values())
        self._live.clear()
        await asyncio.gather(*(self._terminate(w) for w in live), return_exceptions=True)

    # --- process management ------------------------------------------------

    def _command(
        self,
        resume_id: str | None,
        *,
        model: str | None = None,
        permission_mode: str | None = None,
    ) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--append-system-prompt",
            FILE_PRESENTATION_INSTRUCTIONS,
        ]
        if self.interactive_prompts:
            # Route interactive decisions (AskUserQuestion, and — for non-bypass
            # accounts — permission gates) to us over the control channel as
            # `can_use_tool` control_requests. Also the *only* way AskUserQuestion
            # becomes available to a headless `-p` worker. Autonomy is preserved:
            # under bypassPermissions regular tools still auto-run without a
            # callback; only `requires_user_interaction` tools route here.
            cmd += ["--permission-prompt-tool", "stdio"]
        effective_model = model or self.model
        effective_permission_mode = permission_mode or self.permission_mode
        if effective_model:
            cmd += ["--model", effective_model]
        if effective_permission_mode:
            cmd += ["--permission-mode", effective_permission_mode]
        if resume_id:
            cmd += ["--resume", resume_id]
        return cmd

    async def _spawn(
        self,
        key: str,
        rec: WorkerRecord,
        *,
        resume_id: str | None,
        model: str | None = None,
        permission_mode: str | None = None,
    ) -> _LiveWorker:
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(self.account.root)
        try:
            process = await self._process_factory(
                *self._command(
                    resume_id, model=model, permission_mode=permission_mode
                ),
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
        initialized: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        live = _LiveWorker(
            process=process,
            reader_task=None,  # type: ignore[arg-type]
            initialized=initialized,
        )
        live.reader_task = asyncio.create_task(
            self._read_loop(key, live), name=f"claude-worker:{self.account.key}:{key}"
        )
        self._live[key] = live
        return live

    async def _spawn_and_deliver(
        self,
        key: str,
        rec: WorkerRecord,
        message: str,
        image_blocks: list[dict[str, Any]],
        *,
        resume_id: str | None,
        model: str | None,
        permission_mode: str | None,
    ) -> _LiveWorker:
        live = await self._spawn(
            key,
            rec,
            resume_id=resume_id,
            model=model,
            permission_mode=permission_mode,
        )
        wrote = False
        try:
            await self._write_user_message(live, message, image_blocks)
            wrote = True  # payload is now in the child's stdin — never re-send it
            live.turn_active = True
            await asyncio.wait_for(asyncio.shield(live.initialized), timeout=INIT_TIMEOUT_S)
            return live
        except (TimeoutError, WorkerError, OSError) as exc:
            if self._live.get(key) is live:
                self._live.pop(key, None)
            await self._terminate(live)
            if live.initialized.done() and not live.initialized.cancelled():
                live.initialized.exception()  # consume shutdown error after timeout
            # Only an init *timeout* leaves the child possibly alive and mid-turn
            # after our write — then re-sending could run the task twice, so fence
            # it. If the process *died* (WorkerError/OSError, e.g. a gone --resume
            # session that exits without processing), the write never took effect,
            # so recovery may safely re-send under a fresh spawn.
            started = wrote and isinstance(exc, TimeoutError)
            if isinstance(exc, WorkerError):
                exc.write_started = exc.write_started or started
                raise
            raise WorkerError(
                f"claude worker failed to initialize: {exc}", write_started=started
            ) from exc

    async def _terminate(self, live: _LiveWorker) -> None:
        process = live.process
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            pid = getattr(process, "pid", None)
            try:
                if isinstance(pid, int):
                    os.killpg(pid, signal.SIGTERM)
                else:  # test doubles without a pid
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                try:
                    if isinstance(pid, int):
                        os.killpg(pid, signal.SIGKILL)
                    else:
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
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            raise WorkerError(f"could not write to claude worker: {exc}") from exc

    @staticmethod
    def _image_block(path_value: str) -> dict[str, Any]:
        path = Path(path_value)
        media_type, _ = mimetypes.guess_type(path.name)
        if media_type not in SUPPORTED_IMAGE_MEDIA_TYPES:
            raise WorkerError(f"unsupported image type: {path.name}")
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise WorkerError(f"could not read image {path.name}: {exc}") from exc
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }

    async def _write_user_message(
        self, live: _LiveWorker, text: str, image_blocks: list[dict[str, Any]]
    ) -> None:
        content = list(image_blocks)
        content.append({"type": "text", "text": text})
        await self._write(
            live,
            {
                "type": "user",
                "message": {"role": "user", "content": content},
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
                live.last_event_at = time.time()
                self._handle_event(key, live, event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- reader failure only affects one worker
            log.exception("claude worker reader failed for %s:%s", self.account.key, key)
        finally:
            if not live.initialized.done():
                live.initialized.set_exception(
                    WorkerError("claude worker exited before session initialization")
                )
            # The process is gone: fail any interrupt still waiting on an ack for
            # this worker with None, so interrupt() returns at once instead of
            # blocking for the full INTERRUPT_TIMEOUT_S.
            for request_id in list(live.pending_interrupts):
                waiter = self._interrupt_waiters.get(request_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(None)
            live.pending_interrupts.clear()
            if self._live.get(key) is live:
                del self._live[key]
                live.turn_active = False
                # The reader is tearing down while still the registered worker.
                # On a clean process exit the child is already gone (returncode
                # set) and its session lineage is kept for a later --resume. But
                # if the reader itself died with the child still alive (e.g. an
                # oversized stdout line raising past STDOUT_LINE_LIMIT), the
                # detached process group would survive as an orphan and the next
                # deliver would --resume a *second* process on the same
                # transcript — so reap it here.
                if process.returncode is None:
                    await self._terminate(live)

    def _handle_event(self, key: str, live: _LiveWorker, event: dict[str, Any]) -> None:
        etype = event.get("type")
        rec = self._records.get(key)
        if etype == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            if rec is not None and isinstance(session_id, str):
                rec.session_id = session_id
                self._save_state()
                if not live.initialized.done():
                    live.initialized.set_result(session_id)
        elif etype == "result":
            live.turn_active = False
            # The turn is over (finished or interrupted). Any interaction the CLI
            # was blocked on is abandoned — drop it so the widget clears and a late
            # answer can't write a control_response for a request that's gone.
            live.pending_interaction = None
            if rec is not None:
                rec.last_result_at = time.time()
                subtype = event.get("subtype")
                rec.last_result_subtype = subtype if isinstance(subtype, str) else None
                self._save_state()
        elif etype == "control_request":
            # Inbound interactive decision (from --permission-prompt-tool stdio):
            # the agent is blocked until we write a matching control_response.
            request = event.get("request")
            request_id = event.get("request_id")
            if (
                isinstance(request, dict)
                and request.get("subtype") == "can_use_tool"
                and isinstance(request_id, str)
            ):
                live.pending_interaction = {"request_id": request_id, **request}
        elif etype == "control_response":
            response = event.get("response")
            request_id = response.get("request_id") if isinstance(response, dict) else None
            waiter = self._interrupt_waiters.get(request_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(response)
