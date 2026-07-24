"""Host-shared, best-effort cache for GitHub metadata lookups.

The production and staging web processes monitor the same agent account data,
so they also ask many of the same GitHub questions.  This module keeps that
coordination out of callers: a short SQLite lease lets one process refresh a
stale key while the other continues using the last trusted value.

The table name is versioned rather than migrated in place.  Adjacent AgentDeck
versions can therefore share the v1 contract without one process rewriting the
schema under another; a future incompatible format can use a new table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitHubMetadata:
    """A successful fetch result and whether it can still change on GitHub."""

    value: Any
    terminal: bool = False


@dataclass(frozen=True)
class GitHubLookup:
    """Resolved metadata.

    ``complete`` is false when ``value`` is stale (or absent) because another
    process owns the refresh lease or the fetch failed.  A stale value remains
    trustworthy historical evidence; callers must not reinterpret an
    incomplete empty result as proof that remote metadata disappeared.
    """

    value: Any | None
    complete: bool
    refreshed: bool = False


class GitHubMetadataCache:
    """Resolve opaque GitHub metadata through a process-safe shared cache."""

    OPEN_TTL_S = 300.0
    TERMINAL_TTL_S = 3600.0
    NEGATIVE_TTL_S = 6 * 3600.0
    FAILURE_BACKOFF_S = 60.0
    LEASE_S = 15.0

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(
            os.environ.get(
                "AGENTDECK_GITHUB_CACHE",
                "~/.cache/agentdeck/github_metadata.sqlite3",
            )
        ).expanduser()
        self._locks: dict[str, asyncio.Lock] = {}
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout = 2000")
        return connection

    def _prepare(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS github_metadata_v1 (
                        key TEXT PRIMARY KEY,
                        payload TEXT,
                        terminal INTEGER NOT NULL DEFAULT 0,
                        fetched_at REAL NOT NULL DEFAULT 0,
                        lease_until REAL NOT NULL DEFAULT 0,
                        lease_token TEXT
                    )
                    """
                )
        except sqlite3.Error as exc:
            log.debug("GitHub metadata cache init failed: %s", exc)

    @staticmethod
    def _decode(payload: str | None) -> Any | None:
        if payload is None:
            return None
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            return None

    def peek(self, key: str) -> GitHubLookup:
        """Return the last shared value without refreshing it."""
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload, fetched_at FROM github_metadata_v1 WHERE key = ?",
                    (key,),
                ).fetchone()
        except sqlite3.Error as exc:
            log.debug("GitHub metadata cache read failed: %s", exc)
            return GitHubLookup(None, False)
        if row is None or not row[1]:
            return GitHubLookup(None, False)
        return GitHubLookup(self._decode(row[0]), True)

    def _claim(
        self,
        key: str,
        *,
        force: bool,
        fresh_after: float | None,
        now: float,
    ) -> tuple[str | None, GitHubLookup]:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT payload, terminal, fetched_at, lease_until
                    FROM github_metadata_v1 WHERE key = ?
                    """,
                    (key,),
                ).fetchone()
                value = self._decode(row[0]) if row and row[2] else None
                if row and row[2]:
                    ttl = (
                        self.NEGATIVE_TTL_S
                        if value is None
                        else self.TERMINAL_TTL_S
                        if row[1]
                        else self.OPEN_TTL_S
                    )
                    forced_stale = force and (
                        fresh_after is None or float(row[2]) < fresh_after
                    )
                    if not forced_stale and now - float(row[2]) < ttl:
                        return (None, GitHubLookup(value, True))
                if row and float(row[3]) > now:
                    return (None, GitHubLookup(value, False))
                token = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO github_metadata_v1 (key, lease_until, lease_token)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        lease_until = excluded.lease_until,
                        lease_token = excluded.lease_token
                    """,
                    (key, now + self.LEASE_S, token),
                )
                return (token, GitHubLookup(value, False))
        except sqlite3.Error as exc:
            # Cache failure must not disable GitHub-backed behavior.  Fetch
            # directly; storing the result below remains best-effort.
            log.debug("GitHub metadata cache claim failed: %s", exc)
            return ("", GitHubLookup(None, False))

    def _store(
        self, key: str, metadata: GitHubMetadata, now: float, token: str
    ) -> bool:
        try:
            payload = json.dumps(metadata.value, separators=(",", ":"), sort_keys=True)
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE github_metadata_v1 SET
                        payload = ?, terminal = ?, fetched_at = ?,
                        lease_until = 0, lease_token = NULL
                    WHERE key = ? AND lease_token = ?
                    """,
                    (payload, int(metadata.terminal), now, key, token),
                )
            return cursor.rowcount == 1
        except (TypeError, sqlite3.Error) as exc:
            log.debug("GitHub metadata cache save failed: %s", exc)
            return False

    def _back_off(self, key: str, now: float, token: str) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE github_metadata_v1
                    SET lease_until = ?, lease_token = NULL
                    WHERE key = ? AND lease_token = ?
                    """,
                    (now + self.FAILURE_BACKOFF_S, key, token),
                )
        except sqlite3.Error as exc:
            log.debug("GitHub metadata cache backoff failed: %s", exc)

    async def resolve(
        self,
        key: str,
        fetch: Callable[[], Awaitable[GitHubMetadata | None]],
        *,
        force: bool = False,
        fresh_after: float | None = None,
        now: float | None = None,
    ) -> GitHubLookup:
        """Resolve ``key``, refreshing it once across all sharing processes.

        ``fetch`` returns ``None`` only when GitHub is unavailable.  A
        successful negative lookup is represented by ``GitHubMetadata(None)``.
        """
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            results = await self.resolve_many(
                [(key, fetch)],
                force=force,
                fresh_after=fresh_after,
                now=now,
                max_refreshes=1,
            )
            return results[key]

    async def resolve_many(
        self,
        requests: list[
            tuple[str, Callable[[], Awaitable[GitHubMetadata | None]]]
        ],
        *,
        force: bool = False,
        fresh_after: float | None = None,
        now: float | None = None,
        max_refreshes: int | None = None,
    ) -> dict[str, GitHubLookup]:
        """Resolve a batch while optionally bounding its actual GitHub calls."""
        checked_at = time.time() if now is None else now
        results: dict[str, GitHubLookup] = {}
        claimed: list[
            tuple[
                str,
                Callable[[], Awaitable[GitHubMetadata | None]],
                GitHubLookup,
                str,
            ]
        ] = []
        for key, fetch in dict(requests).items():
            if max_refreshes is not None and len(claimed) >= max_refreshes:
                results[key] = self.peek(key)
                continue
            token, cached = self._claim(
                key,
                force=force,
                fresh_after=fresh_after,
                now=checked_at,
            )
            if token is not None:
                claimed.append((key, fetch, cached, token))
            else:
                results[key] = cached

        async def finish(
            key: str,
            fetch: Callable[[], Awaitable[GitHubMetadata | None]],
            cached: GitHubLookup,
            token: str,
        ) -> tuple[str, GitHubLookup]:
            metadata = await fetch()
            if metadata is None:
                self._back_off(key, checked_at, token)
                return (key, cached)
            if not token:
                return (key, GitHubLookup(metadata.value, True, True))
            if not self._store(key, metadata, checked_at, token):
                return (key, self.peek(key))
            return (key, GitHubLookup(metadata.value, True, True))

        if claimed:
            refreshed = await asyncio.gather(
                *(
                    finish(key, fetch, cached, token)
                    for key, fetch, cached, token in claimed
                )
            )
            results.update(refreshed)
        return results
