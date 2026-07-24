"""Shared usage-poller scaffolding for both providers.

Each provider reads its own account limits its own way (Claude over an OAuth HTTP
endpoint with a persistent client; Codex over a short-lived ``app-server``
subprocess), but the loop *around* the read is identical: a phase offset so
accounts don't fire in lockstep, publish-and-cache on success, and mark-stale +
exponential backoff on failure. That loop, the backoff, and the snapshot cache
that other host tools read live here once. A provider subclass overrides three
seams — ``_fetch`` (the read), ``_resource`` (any loop-scoped resource), and
``_on_error`` (provider-specific failure handling).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from ..models import Account, UsageSnapshot

log = logging.getLogger(__name__)

# The exponential backoff ceiling shared by both pollers.
BACKOFF_CAP_S = 1800.0


def shared_cache_dir(configured: str = "") -> Path:
    """Resolve the shared usage-cache directory, with an XDG fallback."""
    if configured:
        return Path(configured).expanduser()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path("~/.cache").expanduser()
    return base / "agentdeck"


def write_shared_cache(snapshot: UsageSnapshot, account: Account, cache_dir: Path) -> None:
    """Atomically publish a snapshot other host tools (e.g. kanban_poll.sh) can read."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "account": account.key,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "five_hour_pct": snapshot.five_hour_pct,
            "five_hour_resets_at": snapshot.five_hour_resets_at.isoformat()
            if snapshot.five_hour_resets_at
            else None,
            "seven_day_pct": snapshot.seven_day_pct,
            "seven_day_resets_at": snapshot.seven_day_resets_at.isoformat()
            if snapshot.seven_day_resets_at
            else None,
        }
        target = cache_dir / f"usage-{account.label}.json"
        fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".usage-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as exc:
        log.warning("could not write shared usage cache for %s: %s", account.key, exc)


def _parse_iso(value: object) -> datetime | None:
    """Decode an ISO timestamp write_shared_cache emitted, tolerant of junk."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_shared_cache(account: Account, cache_dir: Path) -> UsageSnapshot | None:
    """Reconstruct the last snapshot ``write_shared_cache`` persisted for this
    account, or ``None`` if it is absent or unreadable. The read counterpart of
    ``write_shared_cache``, used to warm in-memory state on startup so a web
    restart shows aged bars instead of blank "no usage data yet" until the first
    live poll (which, under usage-endpoint 429 backoff, can be minutes)."""
    path = cache_dir / f"usage-{account.label}.json"
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    fetched = _parse_iso(raw.get("fetched_at"))
    if fetched is None:
        return None
    return UsageSnapshot(
        account_key=account.key,
        five_hour_pct=raw.get("five_hour_pct"),
        five_hour_resets_at=_parse_iso(raw.get("five_hour_resets_at")),
        seven_day_pct=raw.get("seven_day_pct"),
        seven_day_resets_at=_parse_iso(raw.get("seven_day_resets_at")),
        fetched_at=fetched,
    )


def warm_usage_state(state, accounts: list[Account], cache_dir: Path) -> None:
    """Seed each account's usage bars at startup from its last persisted snapshot:
    the shared cache first (full fidelity, incl. reset times), else the history
    DB (percentages only — reset times aren't stored). Age-based staleness then
    renders anything old as "stale · updated …" on its own. Runs before the live
    poller starts, so a restart never blanks the bars."""
    for account in accounts:
        snap = read_shared_cache(account, cache_dir)
        if snap is None and state.db is not None:
            snap = state.db.latest_usage(account.key)
        if snap is not None:
            state.warm_usage(snap)


class UsagePoller:
    """Long-lived per-account usage loop. Injectable ``sleep``/``jitter`` for tests."""

    def __init__(
        self,
        account: Account,
        state,
        *,
        interval_s: float = 300.0,
        cache_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.9, 1.1),
    ):
        self.account = account
        self.state = state
        self.interval_s = interval_s
        self.cache_dir = cache_dir or shared_cache_dir()
        self._sleep = sleep
        self._jitter = jitter
        self._backoff: float | None = None

    def _next_backoff(self) -> float:
        self._backoff = (
            self.interval_s
            if self._backoff is None
            else min(self._backoff * 2, BACKOFF_CAP_S)
        )
        return self._backoff

    def _resource(self) -> contextlib.AbstractAsyncContextManager:
        """A resource entered once around the whole loop (e.g. a persistent HTTP
        client). Default: none — the loop yields ``None`` to ``_fetch``."""
        return contextlib.nullcontext(None)

    async def _fetch(self, resource: object) -> UsageSnapshot:
        """One usage read; ``resource`` is whatever ``_resource`` yielded."""
        raise NotImplementedError

    async def _publish_once(self, resource: object) -> None:
        snapshot = await self._fetch(resource)
        self.state.set_usage(snapshot)
        write_shared_cache(snapshot, self.account, self.cache_dir)
        self._backoff = None

    async def _on_error(self, exc: Exception, resource: object) -> float:
        """Handle a failed cycle and return the next delay. Default: mark the
        account's usage stale and back off exponentially."""
        log.debug("usage poll failed for %s: %s", self.account.key, exc)
        self.state.mark_usage_stale(self.account.key)
        return self._next_backoff()

    async def run(self) -> None:
        # Phase offset so multiple accounts never fire in lockstep.
        await self._sleep(self.interval_s * (self._jitter() - 0.9))
        async with self._resource() as resource:
            while True:
                try:
                    await self._publish_once(resource)
                    delay = self.interval_s * self._jitter()
                except Exception as exc:  # noqa: BLE001 -- keep the long-lived poller alive
                    delay = await self._on_error(exc, resource)
                await self._sleep(delay)
