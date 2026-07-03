"""ChatManager: owns the live chat subprocesses, one per session key.

Kept out of the provider so the web layer has a single place to open/find/stop
chats; the actual spawning is delegated to ``provider.open_chat`` (claude-
specific). Idle chats are reaped and everything is torn down on shutdown so we
never leak a `claude` child.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class ChatManager:
    def __init__(self, idle_timeout_s: float = 600.0, reap_interval_s: float = 30.0):
        self.idle_timeout_s = idle_timeout_s
        self.reap_interval_s = reap_interval_s
        self._sessions: dict[str, object] = {}
        self._last_active: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None

    def _now(self) -> float:
        return asyncio.get_event_loop().time()

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def get(self, key: str):
        return self._sessions.get(key)

    def touch(self, key: str) -> None:
        if key in self._sessions:
            self._last_active[key] = self._now()

    async def get_or_open(self, key: str, account, session, provider):
        """Return the running chat for ``key`` or open a new one (may raise
        the provider's refusal, e.g. ChatRefused)."""
        async with self._lock(key):
            cs = self._sessions.get(key)
            if cs is not None and not cs.closed:
                self._last_active[key] = self._now()
                return cs
            cs = await provider.open_chat(account, session)
            self._sessions[key] = cs
            self._last_active[key] = self._now()
            return cs

    async def stop(self, key: str) -> None:
        cs = self._sessions.pop(key, None)
        self._last_active.pop(key, None)
        if cs is not None:
            await cs.stop()

    async def start_reaper(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reap_interval_s)
            now = self._now()
            stale = [
                k
                for k, cs in list(self._sessions.items())
                if getattr(cs, "closed", False)
                or (now - self._last_active.get(k, now)) > self.idle_timeout_s
            ]
            for k in stale:
                log.info("reaping idle chat %s", k)
                await self.stop(k)

    async def stop_all(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        for key in list(self._sessions):
            await self.stop(key)
