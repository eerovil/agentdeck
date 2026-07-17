"""Web-process facade over the runtime service's deck-owned Claude workers.

The worker *processes* live in the long-lived runtime service; this client
reaches them over the same Unix socket the Codex runtime client uses. It keeps
a session-id → worker-key map (refreshed from the worker snapshot) so the
provider can light up inject/steer/interrupt for the sessions the deck owns.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ...models import Account, InjectResult
from ..codex.runtime_client import runtime_socket_path


class ClaudeWorkerClient:
    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[], None] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account = account
        self._on_change = on_change or (lambda: None)
        transport = transport or httpx.AsyncHTTPTransport(uds=str(runtime_socket_path()))
        self._http = httpx.AsyncClient(
            transport=transport,
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )
        # session_id -> {"key", "turn_active", "stalled"}
        self._owned: dict[str, dict] = {}
        self.available = False

    @property
    def _base(self) -> str:
        return f"/claude/accounts/{self.account.label}"

    async def probe(self) -> bool:
        """Return True iff the runtime serves Claude workers for this account
        (workers enabled + account known); populates the owned map on success."""
        try:
            response = await self._http.get(f"{self._base}/workers")
        except httpx.HTTPError:
            self.available = False
            return False
        self.available = response.status_code == 200
        if self.available:
            self._ingest(response.json())
        return self.available

    async def refresh(self) -> bool:
        """Re-read the worker snapshot; return True if the owned map changed."""
        try:
            response = await self._http.get(f"{self._base}/workers")
            response.raise_for_status()
        except httpx.HTTPError:
            return False
        return self._ingest(response.json())

    def _ingest(self, data: object) -> bool:
        owned: dict[str, dict] = {}
        workers = data.get("workers", {}) if isinstance(data, dict) else {}
        if isinstance(workers, dict):
            for key, worker in workers.items():
                if not isinstance(worker, dict):
                    continue
                session_id = worker.get("session_id")
                if isinstance(session_id, str):
                    owned[session_id] = {
                        "key": key,
                        "turn_active": bool(worker.get("turn_active")),
                        "stalled": bool(worker.get("stalled")),
                    }
        changed = owned != self._owned
        self._owned = owned
        if changed:
            self._on_change()
        return changed

    def owns(self, session_id: str) -> bool:
        return session_id in self._owned

    def key_for(self, session_id: str) -> str | None:
        entry = self._owned.get(session_id)
        return entry["key"] if entry else None

    def turn_active(self, session_id: str) -> bool:
        entry = self._owned.get(session_id)
        return bool(entry and entry["turn_active"])

    async def deliver(
        self, key: str, message: str, *, cwd: str | None = None, fresh: bool = False
    ) -> InjectResult:
        return await self._post(
            "deliver", {"key": key, "message": message, "cwd": cwd, "fresh": fresh}
        )

    async def interrupt(self, key: str) -> InjectResult:
        return await self._post("interrupt", {"key": key})

    async def _post(self, action: str, payload: dict) -> InjectResult:
        try:
            response = await self._http.post(f"{self._base}/{action}", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return InjectResult(False, f"claude worker runtime unavailable: {exc}")
        data = response.json()
        if not isinstance(data, dict):
            return InjectResult(False, "invalid response from claude worker runtime")
        # DeliverResult -> InjectResult; refresh so the owned map reflects the spawn.
        await self.refresh()
        return InjectResult(
            bool(data.get("accepted")),
            data.get("reason") if isinstance(data.get("reason"), str) else None,
            data.get("session_id") if isinstance(data.get("session_id"), str) else None,
        )

    async def stop(self) -> None:
        await self._http.aclose()
