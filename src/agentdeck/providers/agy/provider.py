"""AgyProvider — translates an AGY root (~/.gemini/antigravity-cli) into agentdeck sessions."""

from __future__ import annotations

import logging
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
)
from ..base import SessionProvider
from . import transcripts as transcripts_mod

LIVE_WINDOW_S = 30.0
MAX_SESSIONS = 200

log = logging.getLogger(__name__)


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


class AgyProvider(SessionProvider):
    provider_id = "agy"
    supports_new_session = False

    async def scan_sessions(self, account: Account) -> list[Session]:
        root = account.root
        brain_dir = root / "brain"
        if not brain_dir.is_dir():
            return []

        sessions: list[Session] = []
        now = datetime.now(tz=UTC)

        for conv_dir in brain_dir.iterdir():
            if not conv_dir.is_dir():
                continue
            session_id = conv_dir.name
            transcript_path = (
                conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            )
            if not transcript_path.is_file():
                transcript_path = (
                    conv_dir / ".system_generated" / "logs" / "transcript_full.jsonl"
                )
                if not transcript_path.is_file():
                    continue

            mtime = _mtime(transcript_path)
            events = transcripts_mod.read_events(transcript_path)

            first_prompt = None
            last_text = None
            last_role = None
            last_activity = mtime

            if events:
                user_evs = [e for e in events if e.role == "user" and e.text]
                if user_evs:
                    first_prompt = user_evs[0].text
                last_ev = events[-1]
                last_role = last_ev.role
                last_text = last_ev.text

            is_live = False
            if mtime is not None:
                age_s = (now - mtime).total_seconds()
                is_live = age_s <= LIVE_WINDOW_S

            status = SessionStatus.LIVE if is_live else SessionStatus.IDLE
            capabilities = frozenset({Capability.TRANSCRIPT})

            key = f"{account.key}:{session_id}"
            sessions.append(
                Session(
                    key=key,
                    account_key=account.key,
                    session_id=session_id,
                    status=status,
                    title=first_prompt[:60] if first_prompt else f"AGY {session_id[:8]}",
                    initial_prompt=first_prompt,
                    last_text=last_text,
                    last_role=last_role,
                    started_at=events[0].ts if events and events[0].ts else mtime,
                    last_activity=last_activity,
                    capabilities=capabilities,
                    show_when_idle=True,
                )
            )

        sessions.sort(
            key=lambda s: s.last_activity or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return sessions[:MAX_SESSIONS]

    def watch_paths(self, account: Account) -> list[Path]:
        brain_dir = account.root / "brain"
        return [brain_dir] if brain_dir.is_dir() else [account.root]

    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]:
        transcript_path = (
            account.root
            / "brain"
            / session.session_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if not transcript_path.is_file():
            transcript_path = (
                account.root
                / "brain"
                / session.session_id
                / ".system_generated"
                / "logs"
                / "transcript_full.jsonl"
            )
        return transcripts_mod.read_events(transcript_path, after_seq=after_seq)

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        return None

    async def load_transcript(
        self, account: Account, session: Session, before_seq: int | None = None
    ) -> TranscriptDetail:
        events = await self.read_transcript(account, session)
        total_events = len(events)
        if before_seq is not None:
            events = [e for e in events if e.seq < before_seq]
        window = events[-400:]
        return TranscriptDetail(
            events=window,
            tokens=TokenTotals(),
            model=None,
            todos=[],
            total_events=total_events,
            earliest_seq=window[0].seq if window else 0,
        )
