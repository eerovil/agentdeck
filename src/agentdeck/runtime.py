"""Long-lived Codex control plane, separate from the restartable web UI."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import AppConfig
from .models import Account, InjectResult
from .providers.claude_code.restart import MARKER_TTL_S, RestartMarker, read_markers
from .providers.claude_code.usage import shared_cache_dir
from .providers.claude_code.worker import ClaudeWorkerHost, DeliverResult
from .providers.codex.appserver import CodexAppServer
from .providers.codex.runtime_client import runtime_socket_path

log = logging.getLogger(__name__)


class ActionRequest(BaseModel):
    client_action_id: str | None = None


class TurnRequest(ActionRequest):
    thread_id: str
    message: str
    images: list[str] = Field(default_factory=list)


class ThreadRequest(ActionRequest):
    thread_id: str


class StartRequest(ActionRequest):
    cwd: str
    message: str
    images: list[str] = Field(default_factory=list)
    sandbox: str | None = None
    model: str | None = None
    approval_policy: str | None = None


class AnswerRequest(ActionRequest):
    thread_id: str
    interaction_id: str
    answers: dict[str, list[str]] = Field(default_factory=dict)
    decision: str | None = None


def _result(result: InjectResult) -> dict[str, Any]:
    return asdict(result)


class DeliverRequest(ActionRequest):
    key: str
    message: str
    cwd: str | None = None
    fresh: bool = False
    images: list[str] = Field(default_factory=list)
    model: str | None = None
    permission_mode: str | None = None
    delivery_id: str | None = None


class WorkerKeyRequest(ActionRequest):
    # Keys are opaque and may contain '#' or '/' (e.g. "owner/repo#12"), which
    # are unsafe in a URL path — always carry the key in the body.
    key: str


class ClaudeWorkerRuntime:
    """Own deck-managed Claude worker hosts, one per claude_code account."""

    def __init__(self, config: AppConfig) -> None:
        self.settings = config.claude_workers
        self._usage_cache_dir = shared_cache_dir(config.usage.shared_cache_dir)
        self.accounts = {
            account.label: account
            for account in config.build_accounts()
            if account.provider_id == "claude_code"
        }
        self.hosts: dict[str, ClaudeWorkerHost] = {}

    def host(self, label: str) -> ClaudeWorkerHost:
        if not self.settings.enabled:
            raise HTTPException(status_code=404, detail="claude workers are disabled")
        account = self.accounts.get(label)
        if account is None:
            raise HTTPException(status_code=404, detail="unknown Claude account")
        host = self.hosts.get(label)
        if host is None:
            effective = self.settings.for_account(label)
            host = ClaudeWorkerHost(
                account,
                state_dir=effective.state_path,
                max_workers=effective.max_workers,
                permission_mode=effective.permission_mode or None,
                model=effective.model or None,
                usage_ceiling_pct=effective.usage_ceiling_pct,
                usage_cache_dir=self._usage_cache_dir,
                stall_after_s=effective.stall_after_s,
            )
            self.hosts[label] = host
        return host

    async def resume_after_restart(self) -> None:
        """Drive any session that requested a post-restart continuation.

        Called on runtime boot: an agent that restarted the runtime via
        ``agentdeck restart-runtime`` left a durable marker. Deliver its
        follow-up prompt to that session (``claude --resume``) so it keeps going
        on the fresh code. Never raises — a bad marker must not block boot.
        """
        if not self.settings.enabled:
            return
        try:
            entries = read_markers(self.settings.state_path)
        except Exception:  # noqa: BLE001 -- boot continuation must never crash the runtime
            log.exception("failed to read restart-continue markers")
            return
        for path, marker in entries:
            try:
                if time.time() - marker.created_at > MARKER_TTL_S:
                    log.info(
                        "restart-continue marker for session %s is stale; dropping",
                        marker.session_id,
                    )
                    continue
                await self._deliver_continuation(marker)
            except Exception:  # noqa: BLE001
                log.exception(
                    "failed to deliver restart continuation for session %s",
                    marker.session_id,
                )
            finally:
                path.unlink(missing_ok=True)

    async def _deliver_continuation(self, marker: RestartMarker) -> None:
        for label in self.accounts:
            host = self.host(label)
            key = host.find_key_by_session(marker.session_id)
            if key is None:
                continue
            result = await host.deliver(
                key,
                marker.prompt,
                delivery_id=f"restart-continue:{marker.session_id}:{int(marker.created_at)}",
            )
            log.info(
                "restart-continue: session %s -> %s/%s (%s)",
                marker.session_id,
                label,
                key,
                result.action if result.accepted else f"rejected:{result.reason}",
            )
            return
        log.warning(
            "restart-continue: no worker record for session %s; nothing to resume",
            marker.session_id,
        )

    async def stop(self) -> None:
        await asyncio.gather(
            *(host.stop() for host in self.hosts.values()), return_exceptions=True
        )
        self.hosts.clear()


async def _timed_action(
    action: str,
    label: str,
    client_action_id: str | None,
    operation: Callable[[], Awaitable[InjectResult]],
) -> InjectResult:
    started = time.perf_counter_ns()
    try:
        result = await operation()
    except BaseException:
        if client_action_id:
            log.debug(
                "runtime_action action_id=%s action=%s account=%s outcome=error "
                "elapsed_ms=%.1f",
                client_action_id,
                action,
                label,
                (time.perf_counter_ns() - started) / 1_000_000,
            )
        raise
    if client_action_id:
        log.debug(
            "runtime_action action_id=%s action=%s account=%s accepted=%s elapsed_ms=%.1f",
            client_action_id,
            action,
            label,
            result.accepted,
            (time.perf_counter_ns() - started) / 1_000_000,
        )
    return result


class CodexRuntime:
    """Own all app-server processes and pending interactions across web deploys."""

    def __init__(self, config: AppConfig) -> None:
        self.accounts = {
            account.label: account
            for account in config.build_accounts()
            if account.provider_id == "codex"
        }
        self.clients: dict[str, CodexAppServer] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        try:
            for label, account in self.accounts.items():
                client = CodexAppServer(account)
                await client.start()
                self.clients[label] = client
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        cleanup = list(self._cleanup_tasks)
        for task in cleanup:
            task.cancel()
        if cleanup:
            await asyncio.gather(*cleanup, return_exceptions=True)
            self._cleanup_tasks.clear()
        await asyncio.gather(
            *(client.stop() for client in self.clients.values()),
            return_exceptions=True,
        )
        self.clients.clear()

    def defer_upload_cleanup(
        self, client: CodexAppServer, thread_id: str, root: Path
    ) -> None:
        """Keep runtime-owned image copies until the accepted turn finishes."""

        async def cleanup() -> None:
            try:
                await client.wait_for_thread(thread_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                shutil.rmtree(root, ignore_errors=True)

        task = asyncio.create_task(cleanup(), name=f"upload-cleanup:{thread_id}")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    def client(self, label: str) -> CodexAppServer:
        client = self.clients.get(label)
        if client is None:
            raise HTTPException(status_code=404, detail="unknown Codex account")
        return client

    def snapshot(self, label: str) -> dict[str, Any]:
        client = self.client(label)
        threads = {}
        for thread_id in sorted(client.owned_threads()):
            interaction = client.interaction(thread_id)
            threads[thread_id] = {
                "active_turn": client.active_turn(thread_id),
                "status": client.thread_status(thread_id),
                "interaction": asdict(interaction) if interaction is not None else None,
            }
        return {"threads": threads}


def _paths(values: list[str]) -> list[Path]:
    return [Path(value) for value in values]


def _preserve_images(account: Account, values: list[str]) -> tuple[list[Path], Path | None]:
    """Copy uploads before a web restart can remove its temporary originals."""
    if not values:
        return [], None
    root = runtime_socket_path().parent / "uploads" / account.label / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    copied = []
    try:
        for value in values:
            source = Path(value)
            target = root / source.name
            shutil.copy2(source, target)
            copied.append(target)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return copied, root


def create_runtime_app(config: AppConfig) -> FastAPI:
    runtime = CodexRuntime(config)
    claude_workers = ClaudeWorkerRuntime(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        # Resume any session that restarted the runtime and is waiting to
        # continue. Backgrounded so a `claude --resume` spawn never delays boot.
        resume_task = asyncio.create_task(
            claude_workers.resume_after_restart(),
            name="claude-resume-after-restart",
        )
        try:
            yield
        finally:
            resume_task.cancel()
            await asyncio.gather(resume_task, return_exceptions=True)
            await runtime.stop()
            await claude_workers.stop()

    app = FastAPI(title="agentdeck-codex-runtime", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.claude_workers = claude_workers

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "accounts": sorted(runtime.clients)}

    # --- deck-owned Claude workers (config-gated) ----------------------

    def _deliver_result(result: DeliverResult) -> dict[str, Any]:
        return asdict(result)

    @app.get("/claude/accounts/{label}/workers")
    async def claude_workers_state(label: str) -> dict[str, Any]:
        return claude_workers.host(label).snapshot()

    @app.post("/claude/accounts/{label}/deliver")
    async def claude_deliver(label: str, body: DeliverRequest) -> dict[str, Any]:
        host = claude_workers.host(label)
        result = await _timed_action(
            "claude_deliver",
            label,
            body.client_action_id,
            lambda: host.deliver(
                body.key,
                body.message,
                cwd=body.cwd,
                fresh=body.fresh,
                images=body.images,
                model=body.model,
                permission_mode=body.permission_mode,
                delivery_id=body.delivery_id,
            ),
        )
        return _deliver_result(result)

    @app.post("/claude/accounts/{label}/interrupt")
    async def claude_interrupt(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        return _deliver_result(await claude_workers.host(label).interrupt(body.key))

    @app.post("/claude/accounts/{label}/stop")
    async def claude_stop(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        return _deliver_result(await claude_workers.host(label).stop_worker(body.key))

    @app.post("/claude/accounts/{label}/park")
    async def claude_park(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        return _deliver_result(await claude_workers.host(label).park_worker(body.key))

    @app.post("/claude/accounts/{label}/release")
    async def claude_release(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        return _deliver_result(await claude_workers.host(label).release_worker(body.key))

    @app.post("/claude/accounts/{label}/forget")
    async def claude_forget(label: str, body: WorkerKeyRequest) -> dict[str, Any]:
        return {"removed": claude_workers.host(label).forget(body.key)}

    @app.get("/accounts/{label}/state")
    async def state(label: str) -> dict[str, Any]:
        return runtime.snapshot(label)

    @app.post("/accounts/{label}/start")
    async def start(label: str, body: StartRequest) -> dict[str, Any]:
        client = runtime.client(label)
        return _result(
            await _timed_action(
                "new_session",
                label,
                body.client_action_id,
                lambda: client.start_thread(
                    Path(body.cwd),
                    body.message,
                    images=_paths(body.images),
                    sandbox=body.sandbox,
                    model=body.model,
                    approval_policy=body.approval_policy,
                ),
            )
        )

    @app.post("/accounts/{label}/queue")
    async def queue(label: str, body: TurnRequest) -> dict[str, Any]:
        client = runtime.client(label)
        images, root = _preserve_images(client.account, body.images)
        cleanup_now = True
        try:
            result = await _timed_action(
                "send",
                label,
                body.client_action_id,
                lambda: client.queue_turn(body.thread_id, body.message, images=images),
            )
            if root is not None and result.accepted:
                runtime.defer_upload_cleanup(client, body.thread_id, root)
                cleanup_now = False
            return _result(result)
        finally:
            if root is not None and cleanup_now:
                shutil.rmtree(root, ignore_errors=True)

    @app.post("/accounts/{label}/wait")
    async def wait(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(await runtime.client(label).wait_for_thread(body.thread_id))

    @app.post("/accounts/{label}/compact")
    async def compact(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(await runtime.client(label).compact(body.thread_id))

    @app.post("/accounts/{label}/steer")
    async def steer(label: str, body: TurnRequest) -> dict[str, Any]:
        return _result(
            await _timed_action(
                "steer",
                label,
                body.client_action_id,
                lambda: runtime.client(label).steer(
                    body.thread_id, body.message, images=_paths(body.images)
                ),
            )
        )

    @app.post("/accounts/{label}/interrupt")
    async def interrupt(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(
            await _timed_action(
                "stop",
                label,
                body.client_action_id,
                lambda: runtime.client(label).interrupt(body.thread_id),
            )
        )

    @app.post("/accounts/{label}/answer")
    async def answer(label: str, body: AnswerRequest) -> dict[str, Any]:
        return _result(
            await _timed_action(
                "interaction",
                label,
                body.client_action_id,
                lambda: runtime.client(label).answer(
                    body.thread_id,
                    body.interaction_id,
                    answers=body.answers,
                    decision=body.decision,
                ),
            )
        )

    return app
