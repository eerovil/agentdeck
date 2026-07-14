"""Application-level injection coordination and result tracking."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import InjectConfig
from .models import Account, Capability, InjectResult, Session
from .providers.base import SessionProvider
from .web.uploads import cleanup_image_files

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InjectionStatus:
    state: str
    reason: str | None = None
    items: tuple[QueuedMessage, ...] = ()
    session_key: str | None = None


@dataclass
class QueuedMessage:
    id: int
    text: str
    images: tuple[Path, ...] = ()
    state: str = "queued"
    reason: str | None = None


@dataclass(frozen=True)
class DelegationStatus:
    id: str
    state: str
    account_key: str
    session_key: str | None = None
    reason: str | None = None
    final_message: str | None = None


class InjectionService:
    _MAX_STATUSES = 200

    def __init__(
        self,
        config: InjectConfig,
        *,
        on_change: Callable[[str], None] | None = None,
    ):
        self.config = config
        self._on_change = on_change or (lambda _session_key: None)
        self._tasks: dict[str, asyncio.Task] = {}
        self._status: dict[str, InjectionStatus] = {}
        self._items: dict[str, list[QueuedMessage]] = {}
        self._new_tasks: dict[str, asyncio.Task] = {}
        self._new_status: dict[str, InjectionStatus] = {}
        self._delegation_tasks: dict[str, asyncio.Task] = {}
        self._delegations: dict[str, DelegationStatus] = {}
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._next_id = 1

    def status(self, session_key: str) -> InjectionStatus | None:
        return self._status.get(session_key)

    def can_queue(self, session_key: str) -> bool:
        task = self._tasks.get(session_key)
        return task is not None and not task.done()

    def new_status(self, account_key: str) -> InjectionStatus | None:
        return self._new_status.get(account_key)

    def delegation_status(self, delegation_id: str) -> DelegationStatus | None:
        return self._delegations.get(delegation_id)

    def _remember_delegation(self, status: DelegationStatus) -> None:
        self._delegations.pop(status.id, None)
        self._delegations[status.id] = status
        while len(self._delegations) > self._MAX_STATUSES:
            self._delegations.pop(next(iter(self._delegations)))

    def _remember(self, session_key: str, status: InjectionStatus) -> None:
        self._status.pop(session_key, None)
        self._status[session_key] = status
        while len(self._status) > self._MAX_STATUSES:
            self._status.pop(next(iter(self._status)))

    def _snapshot(self, session_key: str, fallback: str = "complete") -> None:
        items = self._items.get(session_key, [])
        active = next((item for item in items if item.state == "running"), None)
        queued = any(item.state == "queued" for item in items)
        last = items[-1] if items else None
        if active is not None:
            state = "running"
        elif queued:
            state = "queued"
        elif last is not None:
            state = last.state
        else:
            state = fallback
        reason = last.reason if last is not None and last.state == "failed" else None
        self._remember(
            session_key,
            InjectionStatus(state, reason, tuple(items[-12:])),
        )
        self._on_change(session_key)

    async def start(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        message: str,
        images: list[Path] | None = None,
    ) -> InjectResult:
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled")
        if Capability.INJECT not in session.capabilities and not self.can_queue(session.key):
            return InjectResult(False, "this session cannot be injected safely")
        message = message.strip()
        if not message:
            return InjectResult(False, "message is empty")
        if len(message) > self.config.max_message_chars:
            return InjectResult(False, "message is too long")
        item = QueuedMessage(self._next_id, message, tuple(images or []))
        self._next_id += 1
        self._items.setdefault(session.key, []).append(item)
        self._snapshot(session.key, "queued")
        running = self._tasks.get(session.key)
        if running is None or running.done():
            task = asyncio.create_task(
                self._run(account, session, provider),
                name=f"inject:{session.key}",
            )
            self._tasks[session.key] = task
        return InjectResult(True)

    async def _run(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
    ) -> None:
        try:
            while True:
                items = self._items.get(session.key, [])
                item = next((queued for queued in items if queued.state == "queued"), None)
                if item is None:
                    break
                item.state = "running"
                self._snapshot(session.key)
                try:
                    kwargs = {"images": list(item.images)} if item.images else {}
                    result = await provider.inject(
                        account,
                        session,
                        item.text,
                        timeout_s=self.config.timeout_s,
                        **kwargs,
                    )
                finally:
                    cleanup_image_files(item.images)
                item.state = "complete" if result.accepted else "failed"
                item.reason = result.reason
                if not result.accepted:
                    for pending in items:
                        if pending.state == "queued":
                            pending.state = "failed"
                            pending.reason = "not sent because the previous turn failed"
                            cleanup_image_files(pending.images)
                    self._snapshot(session.key)
                    break
                self._snapshot(session.key)
        except asyncio.CancelledError:
            for item in self._items.get(session.key, []):
                if item.state in ("queued", "running"):
                    item.state = "failed"
                    item.reason = "injection cancelled"
                    cleanup_image_files(item.images)
            self._snapshot(session.key, "failed")
            raise
        except Exception as exc:  # noqa: BLE001 -- background actions must report failure
            log.exception("injection failed for %s", session.key)
            for item in self._items.get(session.key, []):
                if item.state in ("queued", "running"):
                    item.state = "failed"
                    item.reason = str(exc)
                    cleanup_image_files(item.images)
            self._snapshot(session.key, "failed")
        finally:
            self._tasks.pop(session.key, None)

    async def start_new(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        images: list[Path] | None = None,
    ) -> InjectResult:
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled")
        if not provider.supports_new_session:
            return InjectResult(False, "this provider cannot start sessions")
        message = message.strip()
        if not message:
            return InjectResult(False, "message is empty")
        if len(message) > self.config.max_message_chars:
            return InjectResult(False, "message is too long")
        if not cwd.is_dir():
            return InjectResult(False, "working directory does not exist")
        running = self._new_tasks.get(account.key)
        if running is not None and not running.done():
            return InjectResult(False, "a new session is already starting for this account")
        self._new_status[account.key] = InjectionStatus("running")
        self._new_tasks[account.key] = asyncio.create_task(
            self._run_new(account, provider, cwd, message, images or []),
            name=f"new-session:{account.key}",
        )
        return InjectResult(True)

    async def _run_new(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        images: list[Path],
    ) -> None:
        cleanup_now = True
        try:
            kwargs = {"images": images} if images else {}
            result = await provider.start_session(
                account,
                cwd,
                message,
                timeout_s=self.config.timeout_s,
                **kwargs,
            )
            if images and result.accepted and result.session_id:
                self.defer_cleanup(account, provider, result.session_id, images)
                cleanup_now = False
            self._new_status[account.key] = InjectionStatus(
                "complete" if result.accepted else "failed",
                result.reason,
                session_key=(
                    f"{account.key}:{result.session_id}" if result.session_id else None
                ),
            )
        except asyncio.CancelledError:
            self._new_status[account.key] = InjectionStatus("failed", "session cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 -- background actions must report failure
            log.exception("new session failed for %s", account.key)
            self._new_status[account.key] = InjectionStatus("failed", str(exc))
        finally:
            if cleanup_now:
                cleanup_image_files(images)
            self._new_tasks.pop(account.key, None)

    def defer_cleanup(
        self,
        account: Account,
        provider: SessionProvider,
        session_id: str,
        images: list[Path],
    ) -> None:
        """Remove steer uploads after the active turn finishes."""
        task = asyncio.create_task(
            self._wait_and_cleanup(account, provider, session_id, images),
            name=f"image-cleanup:{session_id}",
        )
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _wait_and_cleanup(
        self,
        account: Account,
        provider: SessionProvider,
        session_id: str,
        images: list[Path],
    ) -> None:
        try:
            await provider.wait_for_session(
                account,
                session_id,
                timeout_s=self.config.timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- cleanup must outlive turn failures
            log.debug("image cleanup wait failed for %s: %s", session_id, exc)
        finally:
            cleanup_image_files(images)

    async def start_delegation(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        *,
        sandbox: str = "workspace-write",
        model: str | None = None,
    ) -> tuple[InjectResult, str | None]:
        """Start a machine-oriented delegation and retain a pollable result."""
        if not self.config.enabled:
            return InjectResult(False, "message injection is disabled"), None
        if not provider.supports_new_session:
            return InjectResult(False, "this provider cannot start sessions"), None
        message = message.strip()
        if not message:
            return InjectResult(False, "message is empty"), None
        if len(message) > self.config.max_message_chars:
            return InjectResult(False, "message is too long"), None
        if not cwd.is_dir():
            return InjectResult(False, "working directory does not exist"), None

        delegation_id = uuid.uuid4().hex
        self._remember_delegation(
            DelegationStatus(delegation_id, "starting", account.key)
        )
        self._delegation_tasks[delegation_id] = asyncio.create_task(
            self._run_delegation(
                delegation_id,
                account,
                provider,
                cwd,
                message,
                sandbox,
                model,
            ),
            name=f"delegation:{delegation_id}",
        )
        return InjectResult(True), delegation_id

    async def _run_delegation(
        self,
        delegation_id: str,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
        sandbox: str,
        model: str | None,
    ) -> None:
        session_key = None
        try:
            started = await provider.start_session(
                account,
                cwd,
                message,
                timeout_s=self.config.timeout_s,
                sandbox=sandbox,
                model=model,
                approval_policy="on-request",
            )
            if not started.accepted or not started.session_id:
                self._remember_delegation(
                    DelegationStatus(
                        delegation_id,
                        "failed",
                        account.key,
                        reason=started.reason or "Codex did not return a session id",
                    )
                )
                return
            session_key = f"{account.key}:{started.session_id}"
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "running",
                    account.key,
                    session_key=session_key,
                )
            )
            completed = await provider.wait_for_session(
                account,
                started.session_id,
                timeout_s=self.config.timeout_s,
            )
            if not completed.accepted:
                self._remember_delegation(
                    DelegationStatus(
                        delegation_id,
                        "failed",
                        account.key,
                        session_key=session_key,
                        reason=completed.reason or "Codex delegation failed",
                    )
                )
                return
            final_message = None
            for _ in range(50):
                final_message = await provider.session_result(account, started.session_id)
                if final_message:
                    break
                await asyncio.sleep(0.2)
            if not final_message:
                self._remember_delegation(
                    DelegationStatus(
                        delegation_id,
                        "failed",
                        account.key,
                        session_key=session_key,
                        reason="Codex completed but its final message was unavailable",
                    )
                )
                return
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "complete",
                    account.key,
                    session_key=session_key,
                    final_message=final_message,
                )
            )
        except asyncio.CancelledError:
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "failed",
                    account.key,
                    session_key=session_key,
                    reason="delegation cancelled",
                )
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- retain failure for the caller
            log.exception("delegation failed for %s", delegation_id)
            self._remember_delegation(
                DelegationStatus(
                    delegation_id,
                    "failed",
                    account.key,
                    session_key=session_key,
                    reason=str(exc),
                )
            )
        finally:
            self._delegation_tasks.pop(delegation_id, None)

    async def stop(self) -> None:
        tasks = [
            *self._tasks.values(),
            *self._new_tasks.values(),
            *self._delegation_tasks.values(),
            *self._cleanup_tasks,
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._new_tasks.clear()
        self._delegation_tasks.clear()
        self._cleanup_tasks.clear()
