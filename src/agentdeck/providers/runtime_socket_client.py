"""Shared transport for the two web-process facades that drive the persistent
runtime over its Unix socket.

Both the Codex runtime client and the Claude worker client speak the same small
wire dialect to the same socket: a reopenable httpx client bound to the runtime's
UDS, a ``POST {/account-base}/{action}`` that returns an ``InjectResult`` wire
envelope, and a graceful close-on-cancel so a web deploy can shut down while the
separate runtime keeps processing. That transport, the reopen lifecycle, and the
envelope decode live here once; a subclass supplies only its ``_base`` prefix,
its ``refresh`` of runtime-owned state, and its ``_runtime_label`` for messages.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx

from ..action_context import current_client_action_id
from ..models import InjectResult


def runtime_socket_path() -> Path:
    """Return the user-local socket shared by the web and runtime services."""
    configured = os.environ.get("AGENTDECK_CODEX_SOCKET")
    if configured:
        return Path(configured).expanduser()
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime_dir) if runtime_dir else Path("~/.cache").expanduser()
    return base / "agentdeck" / "codex-runtime.sock"


class RuntimeSocketClient:
    """A reopenable httpx client over the runtime's Unix socket.

    Subclasses set ``_runtime_label`` (used in wire-decode and error messages)
    and implement ``_base`` (the per-account URL prefix) and ``refresh`` (re-read
    runtime-owned state after a mutating post)."""

    _runtime_label = "runtime"

    def __init__(
        self,
        account,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.account = account
        # A cancelled _post() closes self._http so uvicorn can shut down promptly,
        # but a still-running web process must be able to reopen it. Keep a factory
        # so any call can lazily rebuild a closed client; otherwise a later refresh
        # would die silently and the cached runtime state would freeze forever.
        self._new_transport = (
            (lambda: transport)
            if transport is not None
            else (lambda: httpx.AsyncHTTPTransport(uds=str(runtime_socket_path())))
        )
        self._http = self._make_client()

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._new_transport(),
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )

    def _client(self) -> httpx.AsyncClient:
        # Reopen after a shutdown-path aclose() so the live process keeps syncing.
        if self._http.is_closed:
            self._http = self._make_client()
        return self._http

    @property
    def _base(self) -> str:
        raise NotImplementedError

    async def refresh(self):
        raise NotImplementedError

    async def _post(self, action: str, payload: dict) -> InjectResult:
        """POST one action, decode the runtime's InjectResult envelope, then
        refresh runtime-owned state. Closes the socket (and re-raises) on cancel
        so a web-service shutdown does not block on an in-flight turn."""
        client_action_id = current_client_action_id()
        if client_action_id:
            payload = {**payload, "client_action_id": client_action_id}
        try:
            response = await self._client().post(f"{self._base}/{action}", json=payload)
            response.raise_for_status()
            result = InjectResult.from_wire(response.json(), source=self._runtime_label)
            await self.refresh()
            return result
        except asyncio.CancelledError:
            # A web deploy cancels InjectionService tasks that may be awaiting a
            # queued turn for minutes. Close the local UDS client immediately; the
            # separate runtime keeps processing the request while uvicorn finishes
            # its shutdown.
            await self._http.aclose()
            raise
        except httpx.HTTPError as exc:
            return InjectResult(False, f"{self._runtime_label} unavailable: {exc}")

    async def stop(self) -> None:
        await self._http.aclose()
