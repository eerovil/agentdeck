"""Optional SQLite persistence: usage history (for sparklines) + a
sessions-seen ledger. Disabled entirely when ``[history] enabled = false`` —
the app runs fully from memory in that case (NullDb).

Writes are small and infrequent (usage every ~5 min, sessions-seen every scan),
so a short synchronous insert on the event loop is negligible here; we keep one
connection guarded by a lock rather than a thread pool.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .models import Session, UsageSnapshot

log = logging.getLogger(__name__)


class NullDb:
    """No-op implementation used when history is disabled."""

    enabled = False

    def record_usage(self, snapshot: UsageSnapshot) -> None: ...
    def recent_five_hour(self, account_key: str, limit: int = 24) -> list[float]:
        return []

    def upsert_sessions_seen(self, sessions: list[Session]) -> None: ...
    def load_assistant_handled(self) -> dict[str, str]:
        return {}

    def record_assistant_handled(self, session_key: str, evidence_signature: str) -> None: ...
    def delete_assistant_handled(self, session_key: str) -> None: ...
    def close(self) -> None: ...


class Db:
    enabled = True

    def __init__(self, path: Path, retention_days: int = 30):
        self.path = path
        self.retention_days = retention_days
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_history (
                    account_key TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    five_hour_pct REAL,
                    seven_day_pct REAL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_acct_ts
                    ON usage_history(account_key, ts);
                CREATE TABLE IF NOT EXISTS sessions_seen (
                    session_key TEXT PRIMARY KEY,
                    account_key TEXT,
                    title TEXT,
                    cwd TEXT,
                    first_seen TEXT,
                    last_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS assistant_handled (
                    session_key TEXT PRIMARY KEY,
                    evidence_signature TEXT NOT NULL,
                    handled_at TEXT NOT NULL
                );
                """
            )

    def record_usage(self, snapshot: UsageSnapshot) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO usage_history(account_key, ts, five_hour_pct, seven_day_pct)"
                    " VALUES (?, ?, ?, ?)",
                    (
                        snapshot.account_key,
                        snapshot.fetched_at.isoformat(),
                        snapshot.five_hour_pct,
                        snapshot.seven_day_pct,
                    ),
                )
        except sqlite3.Error as exc:
            log.debug("record_usage failed: %s", exc)

    def recent_five_hour(self, account_key: str, limit: int = 24) -> list[float]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT five_hour_pct FROM usage_history"
                    " WHERE account_key = ? AND five_hour_pct IS NOT NULL"
                    " ORDER BY ts DESC LIMIT ?",
                    (account_key, limit),
                ).fetchall()
            return [float(r[0]) for r in reversed(rows)]
        except sqlite3.Error:
            return []

    def upsert_sessions_seen(self, sessions: list[Session]) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            with self._lock, self._conn:
                for s in sessions:
                    self._conn.execute(
                        "INSERT INTO sessions_seen(session_key, account_key, title, cwd,"
                        " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(session_key) DO UPDATE SET"
                        " title=excluded.title, cwd=excluded.cwd, last_seen=excluded.last_seen",
                        (
                            s.key,
                            s.account_key,
                            s.title,
                            str(s.cwd) if s.cwd else None,
                            now,
                            now,
                        ),
                    )
        except sqlite3.Error as exc:
            log.debug("upsert_sessions_seen failed: %s", exc)

    def load_assistant_handled(self) -> dict[str, str]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT session_key, evidence_signature FROM assistant_handled"
                ).fetchall()
            return {str(session_key): str(signature) for session_key, signature in rows}
        except sqlite3.Error as exc:
            log.debug("load_assistant_handled failed: %s", exc)
            return {}

    def record_assistant_handled(self, session_key: str, evidence_signature: str) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO assistant_handled(session_key, evidence_signature, handled_at)"
                    " VALUES (?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET"
                    " evidence_signature=excluded.evidence_signature,"
                    " handled_at=excluded.handled_at",
                    (session_key, evidence_signature, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            log.debug("record_assistant_handled failed: %s", exc)

    def delete_assistant_handled(self, session_key: str) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "DELETE FROM assistant_handled WHERE session_key = ?", (session_key,)
                )
        except sqlite3.Error as exc:
            log.debug("delete_assistant_handled failed: %s", exc)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def make_db(enabled: bool, db_path: str, retention_days: int) -> Db | NullDb:
    if not enabled:
        return NullDb()
    try:
        return Db(Path(db_path).expanduser(), retention_days)
    except sqlite3.Error as exc:
        log.warning("history disabled — could not open db: %s", exc)
        return NullDb()
