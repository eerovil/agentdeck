"""Optional SQLite persistence for usage history, the sessions-seen ledger,
Deckhand state, and shared UI preferences. Disabled entirely when ``[history]
enabled = false`` — the app runs fully from memory in that case (NullDb).

Writes are small and infrequent (usage every ~5 min, sessions-seen every scan),
so a short synchronous insert on the event loop is negligible here; we keep one
connection guarded by a lock rather than a thread pool.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import GeneratedTitle, Session, UsageSnapshot

log = logging.getLogger(__name__)


class NullDb:
    """No-op implementation used when history is disabled."""

    enabled = False

    def record_usage(self, snapshot: UsageSnapshot) -> None: ...
    def recent_five_hour(self, account_key: str, limit: int = 24) -> list[float]:
        return []

    def upsert_sessions_seen(self, sessions: list[Session]) -> None: ...
    def load_generated_titles(self) -> dict[str, GeneratedTitle]:
        return {}

    def record_generated_title(
        self, session_key: str, title: str, evidence_signature: str
    ) -> GeneratedTitle:
        return GeneratedTitle(title, evidence_signature, datetime.now(UTC))

    def load_delegated_sessions(self) -> set[str]:
        return set()

    def load_delegation_parents(self) -> dict[str, str]:
        return {}

    def record_delegated_session(
        self, session_key: str, parent_session_id: str | None = None
    ) -> None: ...
    def load_manual_new_chat_cwd(self) -> str | None:
        return None

    def record_manual_new_chat_cwd(self, cwd: str) -> None: ...
    def load_assistant_checkpoint(self) -> dict[str, Any] | None:
        return None

    def record_assistant_checkpoint(self, payload: dict[str, Any]) -> None: ...
    def load_assistant_handled(
        self,
    ) -> dict[str, tuple[str, str | None, str | None, str | None]]:
        return {}

    def record_assistant_handled(
        self,
        session_key: str,
        evidence_signature: str,
        kind: str | None = None,
        headline: str | None = None,
        detail: str | None = None,
    ) -> None: ...
    def delete_assistant_handled(self, session_key: str) -> None: ...
    def load_vapid_keys(self) -> tuple[str, str] | None:
        return None

    def save_vapid_keys(self, public_key: str, private_pem: str) -> None: ...
    def load_push_subscriptions(self) -> list[dict]:
        return []

    def add_push_subscription(self, endpoint: str, p256dh: str, auth: str) -> None: ...
    def delete_push_subscription(self, endpoint: str) -> None: ...
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
                CREATE TABLE IF NOT EXISTS generated_titles (
                    session_key TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    evidence_signature TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistant_handled (
                    session_key TEXT PRIMARY KEY,
                    evidence_signature TEXT NOT NULL,
                    kind TEXT,
                    headline TEXT,
                    detail TEXT,
                    handled_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assistant_checkpoint (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS delegated_sessions (
                    session_key TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    parent_session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS manual_new_chat_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    cwd TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    endpoint TEXT PRIMARY KEY,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS push_vapid (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    public_key TEXT NOT NULL,
                    private_pem TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(assistant_handled)")
            }
            for column in ("kind", "headline", "detail"):
                if column not in columns:
                    self._conn.execute(f"ALTER TABLE assistant_handled ADD COLUMN {column} TEXT")
            delegated_columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(delegated_sessions)")
            }
            if "parent_session_id" not in delegated_columns:
                self._conn.execute(
                    "ALTER TABLE delegated_sessions ADD COLUMN parent_session_id TEXT"
                )
        self._prune_usage_history()

    # --- durability seam ------------------------------------------------
    # Every persisting method funnels through these, so the lock, the transaction,
    # and the "a DB error is non-fatal — log at debug and degrade" policy live in
    # exactly one place instead of being re-typed per method.

    def _write(self, sql: str, params: tuple = ()) -> None:
        try:
            with self._lock, self._conn:
                self._conn.execute(sql, params)
        except sqlite3.Error as exc:
            log.debug("db write failed: %s", exc)

    def _write_many(self, sql: str, rows: Iterable[tuple]) -> None:
        try:
            with self._lock, self._conn:
                self._conn.executemany(sql, rows)
        except sqlite3.Error as exc:
            log.debug("db write failed: %s", exc)

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        try:
            with self._lock:
                return self._conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            log.debug("db query failed: %s", exc)
            return []

    def _prune_usage_history(self) -> None:
        """Drop usage_history rows older than the retention window, once at open.
        ``retention_days <= 0`` keeps everything — a valid "disable pruning"
        setting — so the append-only table can't grow unbounded by default."""
        if self.retention_days <= 0:
            return
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        self._write("DELETE FROM usage_history WHERE ts < ?", (cutoff,))

    def record_usage(self, snapshot: UsageSnapshot) -> None:
        self._write(
            "INSERT INTO usage_history(account_key, ts, five_hour_pct, seven_day_pct)"
            " VALUES (?, ?, ?, ?)",
            (
                snapshot.account_key,
                snapshot.fetched_at.isoformat(),
                snapshot.five_hour_pct,
                snapshot.seven_day_pct,
            ),
        )

    def recent_five_hour(self, account_key: str, limit: int = 24) -> list[float]:
        rows = self._query(
            "SELECT five_hour_pct FROM usage_history"
            " WHERE account_key = ? AND five_hour_pct IS NOT NULL"
            " ORDER BY ts DESC LIMIT ?",
            (account_key, limit),
        )
        return [float(r[0]) for r in reversed(rows)]

    def upsert_sessions_seen(self, sessions: list[Session]) -> None:
        now = datetime.now(UTC).isoformat()
        self._write_many(
            "INSERT INTO sessions_seen(session_key, account_key, title, cwd,"
            " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET"
            " title=excluded.title, cwd=excluded.cwd, last_seen=excluded.last_seen",
            (
                (s.key, s.account_key, s.title, str(s.cwd) if s.cwd else None, now, now)
                for s in sessions
            ),
        )

    def load_generated_titles(self) -> dict[str, GeneratedTitle]:
        rows = self._query(
            "SELECT session_key, title, evidence_signature, updated_at FROM generated_titles"
        )
        try:
            return {
                str(session_key): GeneratedTitle(
                    str(title), str(signature), datetime.fromisoformat(str(updated_at))
                )
                for session_key, title, signature, updated_at in rows
            }
        except ValueError as exc:  # a stored timestamp that won't parse
            log.debug("load_generated_titles decode failed: %s", exc)
            return {}

    def record_generated_title(
        self, session_key: str, title: str, evidence_signature: str
    ) -> GeneratedTitle:
        updated_at = datetime.now(UTC)
        self._write(
            "INSERT INTO generated_titles(session_key, title, evidence_signature,"
            " updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET"
            " title=excluded.title, evidence_signature=excluded.evidence_signature,"
            " updated_at=excluded.updated_at",
            (session_key, title, evidence_signature, updated_at.isoformat()),
        )
        return GeneratedTitle(title, evidence_signature, updated_at)

    def load_manual_new_chat_cwd(self) -> str | None:
        rows = self._query("SELECT cwd FROM manual_new_chat_state WHERE singleton = 1")
        return str(rows[0][0]) if rows else None

    def load_delegated_sessions(self) -> set[str]:
        rows = self._query("SELECT session_key FROM delegated_sessions")
        return {str(row[0]) for row in rows}

    def load_delegation_parents(self) -> dict[str, str]:
        rows = self._query(
            "SELECT session_key, parent_session_id FROM delegated_sessions"
            " WHERE parent_session_id IS NOT NULL"
        )
        return {str(row[0]): str(row[1]) for row in rows}

    def record_delegated_session(
        self, session_key: str, parent_session_id: str | None = None
    ) -> None:
        if parent_session_id is None:
            # Never clobber a previously recorded parent with a bare is_delegated
            # re-record.
            self._write(
                "INSERT OR IGNORE INTO delegated_sessions(session_key, created_at)"
                " VALUES (?, ?)",
                (session_key, datetime.now(UTC).isoformat()),
            )
        else:
            self._write(
                "INSERT INTO delegated_sessions"
                "(session_key, created_at, parent_session_id)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT(session_key) DO UPDATE SET"
                " parent_session_id = excluded.parent_session_id",
                (session_key, datetime.now(UTC).isoformat(), parent_session_id),
            )

    def record_manual_new_chat_cwd(self, cwd: str) -> None:
        self._write(
            "INSERT INTO manual_new_chat_state(singleton, cwd, updated_at)"
            " VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET"
            " cwd=excluded.cwd, updated_at=excluded.updated_at",
            (cwd, datetime.now(UTC).isoformat()),
        )

    def load_assistant_checkpoint(self) -> dict[str, Any] | None:
        rows = self._query("SELECT payload FROM assistant_checkpoint WHERE singleton = 1")
        if not rows:
            return None
        try:
            payload = json.loads(rows[0][0])
        except (json.JSONDecodeError, TypeError) as exc:
            log.debug("load_assistant_checkpoint decode failed: %s", exc)
            return None
        return payload if isinstance(payload, dict) else None

    def record_assistant_checkpoint(self, payload: dict[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            log.debug("record_assistant_checkpoint encode failed: %s", exc)
            return
        self._write(
            "INSERT INTO assistant_checkpoint(singleton, payload, updated_at)"
            " VALUES (1, ?, ?)"
            " ON CONFLICT(singleton) DO UPDATE SET"
            " payload=excluded.payload, updated_at=excluded.updated_at",
            (encoded, datetime.now(UTC).isoformat()),
        )

    def load_assistant_handled(
        self,
    ) -> dict[str, tuple[str, str | None, str | None, str | None]]:
        rows = self._query(
            "SELECT session_key, evidence_signature, kind, headline, detail"
            " FROM assistant_handled ORDER BY handled_at"
        )
        return {
            str(session_key): (
                str(signature),
                str(kind) if kind is not None else None,
                str(headline) if headline is not None else None,
                str(detail) if detail is not None else None,
            )
            for session_key, signature, kind, headline, detail in rows
        }

    def record_assistant_handled(
        self,
        session_key: str,
        evidence_signature: str,
        kind: str | None = None,
        headline: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._write(
            "INSERT INTO assistant_handled(session_key, evidence_signature, kind,"
            " headline, detail, handled_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_key) DO UPDATE SET"
            " evidence_signature=excluded.evidence_signature,"
            " kind=excluded.kind, headline=excluded.headline, detail=excluded.detail,"
            " handled_at=excluded.handled_at",
            (
                session_key,
                evidence_signature,
                kind,
                headline,
                detail,
                datetime.now(UTC).isoformat(),
            ),
        )

    def delete_assistant_handled(self, session_key: str) -> None:
        self._write(
            "DELETE FROM assistant_handled WHERE session_key = ?", (session_key,)
        )

    # --- web push (issue #7) ------------------------------------------

    def load_vapid_keys(self) -> tuple[str, str] | None:
        rows = self._query("SELECT public_key, private_pem FROM push_vapid WHERE singleton = 1")
        return (rows[0][0], rows[0][1]) if rows else None

    def save_vapid_keys(self, public_key: str, private_pem: str) -> None:
        self._write(
            "INSERT INTO push_vapid(singleton, public_key, private_pem, created_at)"
            " VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET"
            " public_key=excluded.public_key, private_pem=excluded.private_pem,"
            " created_at=excluded.created_at",
            (public_key, private_pem, datetime.now(UTC).isoformat()),
        )

    def load_push_subscriptions(self) -> list[dict]:
        """Subscriptions in the ``subscription_info`` shape pywebpush expects."""
        rows = self._query("SELECT endpoint, p256dh, auth FROM push_subscriptions")
        return [{"endpoint": r[0], "keys": {"p256dh": r[1], "auth": r[2]}} for r in rows]

    def add_push_subscription(self, endpoint: str, p256dh: str, auth: str) -> None:
        self._write(
            "INSERT INTO push_subscriptions(endpoint, p256dh, auth, created_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET"
            " p256dh=excluded.p256dh, auth=excluded.auth",
            (endpoint, p256dh, auth, datetime.now(UTC).isoformat()),
        )

    def delete_push_subscription(self, endpoint: str) -> None:
        self._write("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))

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
