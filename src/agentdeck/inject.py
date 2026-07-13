"""Application-level injection coordination and result tracking."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import InjectConfig
from .models import Account, Capability, InjectResult, Session
from .providers.base import SessionProvider

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
    state: str = "queued"
    reason: str | None = None


class InjectionService:
    _MAX_STATUSES = 200

    def __init__(self, config: InjectConfig):
        self.config = config
        self._tasks: dict[str, asyncio.Task] = {}
        self._status: dict[str, InjectionStatus] = {}
        self._items: dict[str, list[QueuedMessage]] = {}
        self._new_tasks: dict[str, asyncio.Task] = {}
        self._new_status: dict[str, InjectionStatus] = {}
        self._next_id = 1

    def status(self, session_key: str) -> InjectionStatus | None:
        return self._status.get(session_key)

    def can_queue(self, session_key: str) -> bool:
        task = self._tasks.get(session_key)
        return task is not None and not task.done()

    def new_status(self, account_key: str) -> InjectionStatus | None:
        return self._new_status.get(account_key)

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

    async def start(
        self,
        account: Account,
        session: Session,
        provider: SessionProvider,
        message: str,
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
        item = QueuedMessage(self._next_id, message)
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
                result = await provider.inject(
                    account,
                    session,
                    item.text,
                    timeout_s=self.config.timeout_s,
                )
                item.state = "complete" if result.accepted else "failed"
                item.reason = result.reason
                if not result.accepted:
                    for pending in items:
                        if pending.state == "queued":
                            pending.state = "failed"
                            pending.reason = "not sent because the previous turn failed"
                    self._snapshot(session.key)
                    break
                self._snapshot(session.key)
        except asyncio.CancelledError:
            for item in self._items.get(session.key, []):
                if item.state in ("queued", "running"):
                    item.state = "failed"
                    item.reason = "injection cancelled"
            self._snapshot(session.key, "failed")
            raise
        except Exception as exc:  # noqa: BLE001 -- background actions must report failure
            log.exception("injection failed for %s", session.key)
            for item in self._items.get(session.key, []):
                if item.state in ("queued", "running"):
                    item.state = "failed"
                    item.reason = str(exc)
            self._snapshot(session.key, "failed")
        finally:
            self._tasks.pop(session.key, None)

    async def start_new(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
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
            self._run_new(account, provider, cwd, message),
            name=f"new-session:{account.key}",
        )
        return InjectResult(True)

    async def _run_new(
        self,
        account: Account,
        provider: SessionProvider,
        cwd: Path,
        message: str,
    ) -> None:
        try:
            result = await provider.start_session(
                account,
                cwd,
                message,
                timeout_s=self.config.timeout_s,
            )
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
            self._new_tasks.pop(account.key, None)

    async def stop(self) -> None:
        tasks = [*self._tasks.values(), *self._new_tasks.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._new_tasks.clear()
