"""Reconnectable client for the separate AgentDeck Codex runtime service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from ...models import (
    Account,
    InjectResult,
    PendingInteraction,
)
from ..runtime_socket_client import RuntimeSocketClient, runtime_socket_path

__all__ = ["CodexRuntimeClient", "runtime_socket_path"]


def _interaction(raw: object) -> PendingInteraction | None:
    """The runtime publishes ``asdict(PendingInteraction)`` over the socket; the
    model owns the inverse (and honours ``multiselect``, which this used to drop)."""
    return PendingInteraction.from_dict(raw)


class CodexRuntimeClient(RuntimeSocketClient):
    """Provider-side facade over the long-lived runtime's Unix socket."""

    _runtime_label = "Codex runtime"

    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[str], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, transport=transport)
        self._on_change = on_change or (lambda _thread_id: None)
        self._refresh_lock = asyncio.Lock()
        self._state: dict[str, dict[str, Any]] = {}

    @property
    def _base(self) -> str:
        return f"/accounts/{self.account.label}"

    async def start(self) -> None:
        response = await self._client().get("/healthz")
        response.raise_for_status()
        await self.refresh()

    async def refresh(self) -> set[str]:
        async with self._refresh_lock:
            response = await self._client().get(f"{self._base}/state")
            response.raise_for_status()
            raw = response.json().get("threads", {})
            state = raw if isinstance(raw, dict) else {}
            changed = {
                thread_id
                for thread_id in set(self._state) | set(state)
                if self._state.get(thread_id) != state.get(thread_id)
            }
            self._state = state
        for thread_id in changed:
            self._on_change(thread_id)
        return changed

    def owns(self, thread_id: str) -> bool:
        return thread_id in self._state

    def active_turn(self, thread_id: str) -> str | None:
        value = self._state.get(thread_id, {}).get("active_turn")
        return value if isinstance(value, str) else None

    def thread_status(self, thread_id: str) -> str:
        value = self._state.get(thread_id, {}).get("status")
        return value if isinstance(value, str) else "notLoaded"

    def interaction(self, thread_id: str) -> PendingInteraction | None:
        return _interaction(self._state.get(thread_id, {}).get("interaction"))

    async def start_thread(
        self,
        cwd: Path,
        message: str,
        *,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
    ) -> InjectResult:
        return await self._post(
            "start",
            {
                "cwd": str(cwd),
                "message": message,
                "images": [str(path) for path in images or []],
                "sandbox": sandbox,
                "model": model,
                "approval_policy": approval_policy,
            },
        )

    async def queue_turn(
        self, thread_id: str, message: str, *, images: list[Path] | None = None
    ) -> InjectResult:
        return await self._post(
            "queue",
            {
                "thread_id": thread_id,
                "message": message,
                "images": [str(path) for path in images or []],
            },
        )

    async def wait_for_thread(self, thread_id: str) -> InjectResult:
        return await self._post("wait", {"thread_id": thread_id})

    async def compact(self, thread_id: str) -> InjectResult:
        return await self._post("compact", {"thread_id": thread_id})

    async def steer(
        self, thread_id: str, message: str, *, images: list[Path] | None = None
    ) -> InjectResult:
        return await self._post(
            "steer",
            {
                "thread_id": thread_id,
                "message": message,
                "images": [str(path) for path in images or []],
            },
        )

    async def interrupt(self, thread_id: str) -> InjectResult:
        return await self._post("interrupt", {"thread_id": thread_id})

    async def answer(
        self,
        thread_id: str,
        interaction_id: str,
        *,
        answers: Mapping[str, list[str]],
        decision: str | None = None,
    ) -> InjectResult:
        return await self._post(
            "answer",
            {
                "thread_id": thread_id,
                "interaction_id": interaction_id,
                "answers": dict(answers),
                "decision": decision,
            },
        )
