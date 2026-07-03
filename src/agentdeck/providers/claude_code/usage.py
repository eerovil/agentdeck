"""OAuth usage-limit reads + the per-account poller.

The undocumented endpoint ``GET https://api.anthropic.com/api/oauth/usage``
returns the same 5-hour / 7-day limit data the ``/usage`` slash command shows.
It is server-side rate limited, so the poller runs sparingly (minutes), reads
the bearer token *fresh* each cycle (tokens rotate), backs off on 429/5xx, and
publishes each snapshot to a shared cache other host tools can read.

Observed response shape (CLI v2.1.198):
    {"five_hour": {"utilization": 0.0, "resets_at": null},
     "seven_day": {"utilization": 5.0, "resets_at": "2026-07-06T20:59:59Z"}, ...}
``utilization`` is already a 0-100 percentage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ...models import Account, UsageSnapshot
from .credentials import read_access_token

log = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# Must look like Claude Code or the request lands in an aggressively throttled
# bucket. Kept generic + version-tagged; update alongside claude-code-internals.md.
USER_AGENT = "claude-code/2.1.198 (agentdeck)"
BETA_HEADER = "oauth-2025-04-20"

BACKOFF_CAP_S = 1800.0


class UsageUnavailable(Exception):
    """Usage could not be read this cycle (no token / network / parse error)."""


class UsageAuthError(UsageUnavailable):
    """401 — token likely rotated; caller should re-read credentials once."""


class UsageRateLimited(UsageUnavailable):
    """429 or 5xx — caller should back off exponentially."""


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pct(bucket: object) -> float | None:
    if isinstance(bucket, dict):
        util = bucket.get("utilization")
        if isinstance(util, (int, float)):
            return float(util)
    return None


def parse_usage(account_key: str, data: dict) -> UsageSnapshot:
    five = data.get("five_hour")
    seven = data.get("seven_day")
    return UsageSnapshot(
        account_key=account_key,
        five_hour_pct=_pct(five),
        five_hour_resets_at=_parse_ts(five.get("resets_at") if isinstance(five, dict) else None),
        seven_day_pct=_pct(seven),
        seven_day_resets_at=_parse_ts(seven.get("resets_at") if isinstance(seven, dict) else None),
        fetched_at=datetime.now(UTC),
    )


async def fetch_usage_once(account: Account, client: httpx.AsyncClient) -> UsageSnapshot:
    """One request. Reads the token fresh from disk; raises on any failure."""
    token = read_access_token(account.root)
    if not token:
        raise UsageUnavailable(f"no access token for {account.key}")
    resp = await client.get(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "User-Agent": USER_AGENT,
        },
    )
    if resp.status_code == 401:
        raise UsageAuthError(f"401 for {account.key}")
    if resp.status_code == 429 or resp.status_code >= 500:
        raise UsageRateLimited(f"{resp.status_code} for {account.key}")
    if resp.status_code >= 400:
        raise UsageUnavailable(f"{resp.status_code} for {account.key}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise UsageUnavailable(f"malformed usage JSON for {account.key}") from exc
    if not isinstance(data, dict):
        raise UsageUnavailable(f"unexpected usage payload for {account.key}")
    return parse_usage(account.key, data)


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


class UsagePoller:
    """Long-lived per-account loop. Injectable ``sleep``/``jitter`` for tests."""

    def __init__(
        self,
        account: Account,
        state,
        *,
        interval_s: float = 300.0,
        cache_dir: Path | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.9, 1.1),
    ):
        self.account = account
        self.state = state
        self.interval_s = interval_s
        self.cache_dir = cache_dir or shared_cache_dir()
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=30.0))
        self._sleep = sleep
        self._jitter = jitter
        self._backoff: float | None = None

    def _next_backoff(self) -> float:
        if self._backoff is None:
            self._backoff = self.interval_s
        else:
            self._backoff = min(self._backoff * 2, BACKOFF_CAP_S)
        return self._backoff

    async def _publish(self, client: httpx.AsyncClient) -> None:
        snap = await fetch_usage_once(self.account, client)
        self.state.set_usage(snap)
        write_shared_cache(snap, self.account, self.cache_dir)
        self._backoff = None

    async def run(self) -> None:
        # Phase offset so multiple accounts never fire in lockstep.
        await self._sleep(self.interval_s * (self._jitter() - 0.9))
        async with self._client_factory() as client:
            while True:
                try:
                    await self._publish(client)
                    delay = self.interval_s * self._jitter()
                except UsageAuthError:
                    # Token may have rotated between read and use — retry once immediately
                    # (fetch_usage_once re-reads the token from disk).
                    try:
                        await self._publish(client)
                        delay = self.interval_s * self._jitter()
                    except UsageUnavailable:
                        self.state.mark_usage_stale(self.account.key)
                        delay = self._next_backoff()
                except UsageRateLimited:
                    self.state.mark_usage_stale(self.account.key)
                    delay = self._next_backoff()
                except (UsageUnavailable, httpx.HTTPError) as exc:
                    log.debug("usage poll failed for %s: %s", self.account.key, exc)
                    self.state.mark_usage_stale(self.account.key)
                    delay = self._next_backoff()
                await self._sleep(delay)
