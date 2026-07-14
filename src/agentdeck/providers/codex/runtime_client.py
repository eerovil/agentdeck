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
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
)


def runtime_socket_path() -> Path:
    """Return the user-local socket shared by the web and runtime services."""
    import os

    configured = os.environ.get("AGENTDECK_CODEX_SOCKET")
    if configured:
        return Path(configured).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("~/.cache").expanduser()
    return base / "agentdeck" / "codex-runtime.sock"


def _interaction(raw: object) -> PendingInteraction | None:
    if not isinstance(raw, dict):
        return None
    try:
        questions = tuple(
            InteractionQuestion(
                id=question["id"],
                header=question["header"],
                prompt=question["prompt"],
                options=tuple(
                    InteractionOption(
                        label=option["label"],
                        description=option.get("description", ""),
                        value=option.get("value"),
                    )
                    for option in question.get("options", [])
                ),
                allow_other=bool(question.get("allow_other")),
                secret=bool(question.get("secret")),
            )
            for question in raw.get("questions", [])
        )
        return PendingInteraction(
            id=raw["id"],
            kind=raw["kind"],
            thread_id=raw["thread_id"],
            turn_id=raw.get("turn_id"),
            title=raw["title"],
            message=raw.get("message"),
            questions=questions,
            command=raw.get("command"),
            cwd=raw.get("cwd"),
            url=raw.get("url"),
            decisions=tuple(raw.get("decisions", [])),
        )
    except (KeyError, TypeError):
        return None


class CodexRuntimeClient:
    """Provider-side facade over the long-lived runtime's Unix socket."""

    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[str], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account = account
        self._on_change = on_change or (lambda _thread_id: None)
        transport = transport or httpx.AsyncHTTPTransport(uds=str(runtime_socket_path()))
        self._http = httpx.AsyncClient(
            transport=transport,
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )
        self._refresh_lock = asyncio.Lock()
        self._state: dict[str, dict[str, Any]] = {}

    @property
    def _base(self) -> str:
        return f"/accounts/{self.account.label}"

    async def start(self) -> None:
        response = await self._http.get("/healthz")
        response.raise_for_status()
        await self.refresh()

    async def stop(self) -> None:
        await self._http.aclose()

    async def refresh(self) -> set[str]:
        async with self._refresh_lock:
            response = await self._http.get(f"{self._base}/state")
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

    @staticmethod
    def _result(raw: object) -> InjectResult:
        if not isinstance(raw, dict):
            return InjectResult(False, "invalid response from Codex runtime")
        return InjectResult(
            bool(raw.get("accepted")),
            raw.get("reason") if isinstance(raw.get("reason"), str) else None,
            raw.get("session_id") if isinstance(raw.get("session_id"), str) else None,
        )

    async def _post(self, action: str, payload: dict[str, Any]) -> InjectResult:
        try:
            response = await self._http.post(f"{self._base}/{action}", json=payload)
            response.raise_for_status()
            result = self._result(response.json())
            await self.refresh()
            return result
        except asyncio.CancelledError:
            # A web deploy cancels InjectionService tasks that may be awaiting a
            # queued turn for minutes. Close the local UDS client immediately;
            # the separate runtime keeps processing the request and the Codex
            # turn, while uvicorn can finish its web-service shutdown promptly.
            await self._http.aclose()
            raise
        except httpx.HTTPError as exc:
            return InjectResult(False, f"Codex runtime unavailable: {exc}")

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
