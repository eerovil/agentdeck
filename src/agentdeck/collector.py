"""Collector: keeps AppState in sync with each account's session sources.

Per account we run up to three cooperating tasks:
- a **scan loop** (full ``scan_sessions`` every ``scan_interval_s``) — the
  authoritative but heavier pass,
- a **liveness sweep** (cheap ``sweep_liveness`` every ``liveness_interval_s``)
  — flips LIVE→IDLE fast when a pid dies, since that is not a filesystem event,
- a **usage poller** (provider-supplied) — the OAuth limit reader.

One additional host sampler publishes aggregate CPU and memory usage.

v0.1 uses periodic scanning; filesystem watching (watchfiles) is a v0.2
optimisation that slots in behind the same provider ``watch_paths`` interface.
"""

from __future__ import annotations

import asyncio
import logging

from .config import AppConfig
from .host_stats import HostStatsSampler
from .models import Account
from .providers import PROVIDERS, SessionProvider
from .providers.claude_code.usage import shared_cache_dir
from .state import AppState

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, config: AppConfig, state: AppState):
        self.config = config
        self.state = state
        self.accounts: list[Account] = config.build_accounts()
        self._tasks: list[asyncio.Task] = []
        self._host_sampler = HostStatsSampler()

    def _provider(self, account: Account) -> SessionProvider:
        return PROVIDERS[account.provider_id]

    async def _scan_loop(self, account: Account) -> None:
        provider = self._provider(account)
        interval = self.config.polling.scan_interval_s
        while True:
            try:
                sessions = await provider.scan_sessions(account)
                self.state.replace_account_sessions(account.key, sessions)
            except Exception as exc:  # noqa: BLE001 — a bad scan must not kill the loop
                log.warning("scan failed for %s: %s", account.key, exc)
            await asyncio.sleep(interval)

    async def _liveness_loop(self, account: Account) -> None:
        provider = self._provider(account)
        interval = self.config.polling.liveness_interval_s
        while True:
            await asyncio.sleep(interval)
            try:
                current = self.state.sessions_for_account(account.key)
                # The sweep refreshes state's own session objects and publishes
                # "sessions" once through AppState if anything changed, so the
                # mutation and its notification stay a single transition.
                provider.sweep_liveness(account, current, self.state)
            except Exception as exc:  # noqa: BLE001
                log.debug("liveness sweep failed for %s: %s", account.key, exc)

    def _make_usage_task(self, account: Account) -> asyncio.Task | None:
        provider = self._provider(account)
        poller = provider.make_usage_poller(
            account,
            self.state,
            self.state.bus,
            interval_s=self.config.polling.usage_interval_s,
            cache_dir=shared_cache_dir(self.config.usage.shared_cache_dir),
        )
        if poller is None:
            return None
        return asyncio.create_task(poller.run(), name=f"usage:{account.key}")

    async def _host_loop(self) -> None:
        while True:
            try:
                self.state.set_host_stats(self._host_sampler.sample())
            except Exception as exc:  # noqa: BLE001 -- host stats are optional UI data
                log.debug("host stats sample failed: %s", exc)
            await asyncio.sleep(self.config.polling.host_interval_s)

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._host_loop(), name="usage:host"))
        for account in self.accounts:
            try:
                await self._provider(account).start_account(account, self.state)
            except Exception as exc:  # noqa: BLE001 -- scans remain useful without controls
                log.warning("provider runtime failed for %s: %s", account.key, exc)
            self._tasks.append(
                asyncio.create_task(self._scan_loop(account), name=f"scan:{account.key}")
            )
            self._tasks.append(
                asyncio.create_task(self._liveness_loop(account), name=f"liveness:{account.key}")
            )
            usage = self._make_usage_task(account)
            if usage is not None:
                self._tasks.append(usage)
        log.info("collector started for %d account(s)", len(self.accounts))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        for account in self.accounts:
            try:
                await self._provider(account).stop_account(account)
            except Exception as exc:  # noqa: BLE001 -- finish stopping other accounts
                log.debug("provider runtime stop failed for %s: %s", account.key, exc)
