"""CodexProvider — translates a CODEX_HOME into agentdeck sessions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from ...models import (
    Account,
    Capability,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
)
from ..base import SessionProvider
from . import transcripts as transcripts_mod

DETAIL_WINDOW = 400
MAX_SESSIONS = 200
LIVE_WINDOW_S = 30.0


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _list_rollouts(root: Path) -> list[Path]:
    sessions = root / "sessions"
    if not sessions.is_dir():
        return []
    found = []
    for path in sessions.glob("*/*/*/rollout-*.jsonl"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:MAX_SESSIONS]]


class CodexProvider(SessionProvider):
    """Read Codex CLI rollouts.

    Codex exposes no process-to-session PID registry. LIVE therefore means the
    rollout was written within ``LIVE_WINDOW_S``; a quiet process waiting for
    input may be reported IDLE, and a just-finished process briefly remains LIVE.
    """

    provider_id = "codex"

    def __init__(self) -> None:
        self._meta_cache: dict[str, tuple[float, transcripts_mod.TranscriptMeta]] = {}
        self._last_ev_cache: dict[str, tuple[float, TranscriptEvent | None]] = {}
        self._paths: dict[tuple[str, str], Path] = {}

    def _cached_meta(self, path: Path) -> transcripts_mod.TranscriptMeta:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return transcripts_mod.TranscriptMeta()
        hit = self._meta_cache.get(str(path))
        if hit is not None and hit[0] == mtime:
            return hit[1]
        meta = transcripts_mod.transcript_meta(path)
        self._meta_cache[str(path)] = (mtime, meta)
        return meta

    def _cached_last_event(self, path: Path) -> TranscriptEvent | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._last_ev_cache.get(str(path))
        if hit is not None and hit[0] == mtime:
            return hit[1]
        event = transcripts_mod.last_event(path)
        self._last_ev_cache[str(path)] = (mtime, event)
        return event

    def _transcript_path(self, account: Account, session: Session) -> Path | None:
        path = self._paths.get((account.key, session.session_id))
        if path is not None and path.is_file():
            return path
        for candidate in _list_rollouts(account.root):
            meta = self._cached_meta(candidate)
            if meta.session_id == session.session_id:
                self._paths[(account.key, session.session_id)] = candidate
                return candidate
        return None

    def _derived_state(
        self, path: Path, last_activity: datetime | None
    ) -> tuple[SessionStatus, bool, str | None]:
        if last_activity is None:
            return (SessionStatus.IDLE, False, None)
        age = max(0.0, (datetime.now(UTC) - last_activity).total_seconds())
        live = age < LIVE_WINDOW_S
        status = SessionStatus.LIVE if live else SessionStatus.IDLE
        last_event = self._cached_last_event(path)
        activity = activity_label(live, live, last_event, age)
        return (status, activity is not None, activity)

    @staticmethod
    def _apply_model(events: list[TranscriptEvent], model: str | None) -> list[TranscriptEvent]:
        for event in events:
            if event.role == "assistant" and event.model is None:
                event.model = model
        return events

    def watch_paths(self, account: Account) -> list[Path]:
        sessions = account.root / "sessions"
        return [sessions] if sessions.exists() else []

    async def scan_sessions(self, account: Account) -> list[Session]:
        sessions = []
        current_paths: dict[tuple[str, str], Path] = {}
        seen: set[str] = set()
        for path in _list_rollouts(account.root):
            meta = self._cached_meta(path)
            session_id = meta.session_id
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            current_paths[(account.key, session_id)] = path
            last_activity = _mtime(path)
            status, thinking, activity = self._derived_state(path, last_activity)
            sessions.append(
                Session(
                    key=f"{account.key}:{session_id}",
                    account_key=account.key,
                    session_id=session_id,
                    status=status,
                    thinking=thinking,
                    activity=activity,
                    cwd=Path(meta.cwd) if meta.cwd else None,
                    title=meta.title,
                    last_prompt=meta.last_prompt,
                    last_text=meta.last_text,
                    last_role=meta.last_role,
                    model=meta.model,
                    kind=meta.kind,
                    worker_type="you",
                    started_at=meta.started_at,
                    last_activity=last_activity,
                    tokens=meta.tokens,
                    context_tokens=meta.context_tokens,
                    capabilities=frozenset({Capability.TRANSCRIPT}),
                )
            )
        self._paths = {
            key: value for key, value in self._paths.items() if key[0] != account.key
        }
        self._paths.update(current_paths)
        return sessions

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Refresh recency-derived status and mtime-cached card metadata."""
        changed = []
        for session in sessions:
            path = self._transcript_path(account, session)
            if path is None:
                continue
            last_activity = _mtime(path)
            status, thinking, activity = self._derived_state(path, last_activity)
            meta = self._cached_meta(path)
            values = (
                status,
                thinking,
                activity,
                last_activity,
                meta.last_prompt,
                meta.last_text,
                meta.last_role,
                meta.model,
                meta.tokens,
                meta.context_tokens,
            )
            current = (
                session.status,
                session.thinking,
                session.activity,
                session.last_activity,
                session.last_prompt,
                session.last_text,
                session.last_role,
                session.model,
                session.tokens,
                session.context_tokens,
            )
            if values == current:
                continue
            (
                session.status,
                session.thinking,
                session.activity,
                session.last_activity,
                session.last_prompt,
                session.last_text,
                session.last_role,
                session.model,
                session.tokens,
                session.context_tokens,
            ) = values
            changed.append(session)
        return changed

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        return None

    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]:
        path = self._transcript_path(account, session)
        if path is None:
            return []
        return await asyncio.to_thread(self._read_transcript_file, path, after_seq)

    def _read_transcript_file(self, path: Path, after_seq: int) -> list[TranscriptEvent]:
        """Read and filter a transcript outside the caller's event loop."""
        read = transcripts_mod.read_events(path)
        events = [event for event in read.events if event.seq > after_seq]
        return self._apply_model(events, self._cached_meta(path).model)

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        path = self._transcript_path(account, session)
        return (
            await asyncio.to_thread(transcripts_mod.transcript_cursor, path)
            if path is not None
            else (0, 0)
        )

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return ([], byte_offset, seq)
        read = await asyncio.to_thread(
            transcripts_mod.read_events, path, byte_offset=byte_offset, seq=seq
        )
        events = self._apply_model(read.events, self._cached_meta(path).model)
        return (events, read.byte_offset, read.seq)

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        path = self._transcript_path(account, session)
        if path is None:
            return None
        event = self._cached_last_event(path)
        if event is not None and event.role == "assistant" and event.model is None:
            event.model = self._cached_meta(path).model
        return event

    async def load_transcript(
        self, account: Account, session: Session, before_seq: int | None = None
    ) -> TranscriptDetail:
        path = self._transcript_path(account, session)
        if path is None:
            return TranscriptDetail(
                events=[],
                tokens=TokenTotals(),
                model=None,
                todos=[],
                total_events=0,
                earliest_seq=0,
            )
        return await asyncio.to_thread(
            self._load_transcript_file,
            path,
            before_seq,
        )

    def _load_transcript_file(self, path: Path, before_seq: int | None) -> TranscriptDetail:
        """Build a detail window without blocking the caller's event loop."""
        read = transcripts_mod.read_events(path)
        all_events = read.events
        meta = self._cached_meta(path)
        self._apply_model(all_events, meta.model)
        events = all_events
        if before_seq is not None:
            events = [event for event in events if event.seq < before_seq]
        window = events[-DETAIL_WINDOW:]
        return TranscriptDetail(
            events=window,
            tokens=transcripts_mod.token_totals(all_events),
            model=meta.model,
            todos=[],
            total_events=len(all_events),
            earliest_seq=window[0].seq if window else 0,
            skipped=read.skipped,
        )
