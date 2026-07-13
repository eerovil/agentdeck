"""Codex account rate-limit reads through ``codex app-server``.

The app-server owns authentication and exposes the same rolling limits Codex
shows in its UI.  agentdeck speaks the small JSON-RPC subset it needs instead
of reading credentials or calling an undocumented HTTP endpoint directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...models import Account, UsageSnapshot
from ..claude_code.usage import shared_cache_dir, write_shared_cache

log = logging.getLogger(__name__)

FIVE_HOUR_MINS = 5 * 60
SEVEN_DAY_MINS = 7 * 24 * 60
BACKOFF_CAP_S = 1800.0
REQUEST_TIMEOUT_S = 30.0


class UsageUnavailable(Exception):
    """Codex usage could not be read this cycle."""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _codex_limits(data: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer the named Codex bucket, then the compatible legacy view."""
    by_id = data.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        bucket = by_id.get("codex")
        if isinstance(bucket, dict):
            return bucket
    legacy = data.get("rateLimits")
    return legacy if isinstance(legacy, dict) else None


def parse_usage(account_key: str, data: dict[str, Any]) -> UsageSnapshot:
    limits = _codex_limits(data)
    if limits is None:
        raise UsageUnavailable("app-server response has no Codex rate-limit bucket")

    windows: dict[int, tuple[float | None, datetime | None]] = {}
    raw_windows: list[dict[str, Any]] = []
    for name in ("primary", "secondary"):
        window = limits.get(name)
        if not isinstance(window, dict):
            continue
        raw_windows.append(window)
        duration = window.get("windowDurationMins")
        if isinstance(duration, int) and not isinstance(duration, bool):
            windows[duration] = (
                _number(window.get("usedPercent")),
                _timestamp(window.get("resetsAt")),
            )

    # Older app-server responses may omit durations. Preserve the historical
    # primary=5h / secondary=7d ordering only for otherwise-unidentified windows.
    five = windows.get(FIVE_HOUR_MINS)
    seven = windows.get(SEVEN_DAY_MINS)
    if five is None and raw_windows and raw_windows[0].get("windowDurationMins") is None:
        first = raw_windows[0]
        five = (_number(first.get("usedPercent")), _timestamp(first.get("resetsAt")))
    if seven is None and len(raw_windows) > 1 and raw_windows[1].get("windowDurationMins") is None:
        second = raw_windows[1]
        seven = (_number(second.get("usedPercent")), _timestamp(second.get("resetsAt")))

    if five is None and seven is None:
        raise UsageUnavailable("Codex rate-limit bucket has no recognized rolling window")

    return UsageSnapshot(
        account_key=account_key,
        five_hour_pct=five[0] if five else None,
        five_hour_resets_at=five[1] if five else None,
        seven_day_pct=seven[0] if seven else None,
        seven_day_resets_at=seven[1] if seven else None,
        fetched_at=datetime.now(UTC),
    )


async def _read_response(process: asyncio.subprocess.Process, request_id: int) -> dict[str, Any]:
    assert process.stdout is not None
    while line := await process.stdout.readline():
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        error = message.get("error")
        if error is not None:
            raise UsageUnavailable(f"Codex app-server request failed: {error}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise UsageUnavailable("Codex app-server returned an invalid result")
        return result
    raise UsageUnavailable("Codex app-server closed before replying")


async def _send(process: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    if process.stdin is None:
        raise UsageUnavailable("Codex app-server stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    await process.stdin.drain()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


async def fetch_usage_once(
    account: Account,
    *,
    process_factory: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
) -> UsageSnapshot:
    """Start a short-lived app-server and read this account's rate limits."""
    factory = process_factory or asyncio.create_subprocess_exec
    env = os.environ.copy()
    env["CODEX_HOME"] = str(account.root)
    try:
        process = await factory(
            "codex",
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except (OSError, ValueError) as exc:
        raise UsageUnavailable(f"could not start Codex app-server: {exc}") from exc

    try:
        async with asyncio.timeout(timeout_s):
            await _send(
                process,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "agentdeck", "version": "0.3.1"}},
                },
            )
            await _read_response(process, 1)
            await _send(process, {"method": "initialized", "params": {}})
            await _send(process, {"id": 2, "method": "account/rateLimits/read", "params": {}})
            result = await _read_response(process, 2)
            return parse_usage(account.key, result)
    except TimeoutError as exc:
        raise UsageUnavailable("Codex app-server usage request timed out") from exc
    except (BrokenPipeError, ConnectionError, OSError) as exc:
        raise UsageUnavailable(f"Codex app-server communication failed: {exc}") from exc
    finally:
        await _stop_process(process)


class UsagePoller:
    def __init__(
        self,
        account: Account,
        state,
        *,
        interval_s: float = 300.0,
        cache_dir: Path | None = None,
        fetch: Callable[[Account], Awaitable[UsageSnapshot]] = fetch_usage_once,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0.9, 1.1),
    ):
        self.account = account
        self.state = state
        self.interval_s = interval_s
        self.cache_dir = cache_dir or shared_cache_dir()
        self._fetch = fetch
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

    async def run(self) -> None:
        await self._sleep(self.interval_s * (self._jitter() - 0.9))
        while True:
            try:
                snapshot = await self._fetch(self.account)
                self.state.set_usage(snapshot)
                write_shared_cache(snapshot, self.account, self.cache_dir)
                self._backoff = None
                delay = self.interval_s * self._jitter()
            except Exception as exc:  # noqa: BLE001 -- keep the long-lived poller alive
                log.debug("Codex usage poll failed for %s: %s", self.account.key, exc)
                self.state.mark_usage_stale(self.account.key)
                delay = self._next_backoff()
            await self._sleep(delay)
