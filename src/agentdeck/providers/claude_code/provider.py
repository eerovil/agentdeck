"""ClaudeCodeProvider — translates a $CLAUDE_CONFIG_DIR into agentdeck sessions.

v0.1 scope: discover sessions (live from the pid registry, idle from transcript
files) and surface per-account usage limits. Transcript parsing, injection and
chat arrive in v0.2/v0.3 and raise NotImplementedError here for now.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ...models import (
    Account,
    Capability,
    InjectResult,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
)
from ..base import SessionProvider
from . import history as history_mod
from . import inject as inject_mod
from . import registry as registry_mod
from . import transcripts as transcripts_mod
from .chat import ChatRefused, ChatSession
from .usage import UsagePoller, fetch_usage_once

# Max events rendered on a detail page; older ones fetched via "load earlier".
DETAIL_WINDOW = 400

log = logging.getLogger(__name__)

# Upper bound on idle sessions surfaced per account in v0.1 — the projects/ dir
# can hold thousands of transcripts; show the most recently active.
MAX_IDLE_SESSIONS = 200

# A LIVE session whose transcript was written within this window is "thinking"
# (streaming a response); past it, the agent is live but waiting for input.
THINKING_WINDOW_S = 25.0


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _list_transcripts(config_dir: Path) -> dict[str, Path]:
    """Map sessionId -> newest transcript path under projects/<slug>/<uuid>.jsonl."""
    out: dict[str, Path] = {}
    projects = config_dir / "projects"
    if not projects.is_dir():
        return out
    for path in projects.glob("*/*.jsonl"):
        sid = path.stem
        prev = out.get(sid)
        if prev is None or path.stat().st_mtime > prev.stat().st_mtime:
            out[sid] = path
    return out


class ClaudeCodeProvider(SessionProvider):
    provider_id = "claude_code"

    def __init__(self) -> None:
        # (title, last_prompt, first_user) cache keyed by path, invalidated by mtime,
        # so idle transcripts aren't re-parsed on every scan.
        self._meta_cache: dict[str, tuple[float, tuple[str | None, str | None, str | None]]] = {}

    def _cached_meta(self, path: Path) -> tuple[str | None, str | None, str | None]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return (None, None, None)
        hit = self._meta_cache.get(str(path))
        if hit is not None and hit[0] == mtime:
            return hit[1]
        meta = transcripts_mod.transcript_meta(path)
        self._meta_cache[str(path)] = (mtime, meta)
        return meta

    # --- discovery -----------------------------------------------------

    def watch_paths(self, account: Account) -> list[Path]:
        root = account.root
        candidates = (root / "sessions", root / "history.jsonl", root / "projects")
        return [p for p in candidates if p.exists()]

    async def scan_sessions(self, account: Account) -> list[Session]:
        root = account.root
        entries = {e.session_id: e for e in registry_mod.read_registry(root)}
        alive = {sid: registry_mod.is_alive(e) for sid, e in entries.items()}
        hist = history_mod.load_history(root)
        transcripts = _list_transcripts(root)

        # Universe = local transcripts (capped, most-recent) ∪ live registry sessions.
        ranked = sorted(
            transcripts.items(),
            key=lambda kv: kv[1].stat().st_mtime,
            reverse=True,
        )
        keep = {sid for sid, _ in ranked[:MAX_IDLE_SESSIONS]}
        keep |= {sid for sid, ok in alive.items() if ok}

        sessions: list[Session] = []
        for sid in keep:
            entry = entries.get(sid)
            is_live = alive.get(sid, False)
            h = hist.get(sid)
            tpath = transcripts.get(sid)

            cwd: Path | None = None
            if entry and entry.cwd:
                cwd = entry.cwd
            elif h and h.project:
                cwd = Path(h.project)

            # Title: prefer the transcript's AI-generated title, then history,
            # then the first user message; last_prompt likewise from the transcript.
            ai_title = last_prompt = first_user = None
            if tpath is not None:
                ai_title, last_prompt, first_user = self._cached_meta(tpath)
            title = ai_title or (h.title if h else None) or first_user
            last_prompt = last_prompt or (h.last_prompt if h else None)

            last_activity = _mtime(tpath) if tpath else (entry.started_at if entry else None)

            status = SessionStatus.LIVE if is_live else SessionStatus.IDLE
            thinking = bool(
                is_live
                and last_activity is not None
                and (datetime.now(UTC) - last_activity).total_seconds() < THINKING_WINDOW_S
            )
            caps: set[Capability] = set()
            if tpath is not None:
                caps.add(Capability.TRANSCRIPT)  # readable from v0.2
            # Injectable only when idle with a known cwd; the route re-checks
            # liveness + trust at spawn time, this is just a UI hint.
            if not is_live and cwd is not None:
                caps.add(Capability.INJECT)

            sessions.append(
                Session(
                    key=f"{account.key}:{sid}",
                    account_key=account.key,
                    session_id=sid,
                    status=status,
                    thinking=thinking,
                    cwd=cwd,
                    title=title,
                    last_prompt=last_prompt,
                    kind=entry.kind if entry else None,
                    pid=entry.pid if (entry and is_live) else None,
                    proc_start=entry.proc_start if entry else None,
                    started_at=entry.started_at if entry else None,
                    last_activity=last_activity,
                    capabilities=frozenset(caps),
                )
            )
        return sessions

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Cheap recheck (~every 10s): flip LIVE↔IDLE and refresh the thinking
        flag from transcript recency, so both update between full scans."""
        entries = {e.session_id: e for e in registry_mod.read_registry(account.root)}
        changed: list[Session] = []
        now = datetime.now(UTC)
        for s in sessions:
            entry = entries.get(s.session_id)
            live_now = bool(entry and registry_mod.is_alive(entry))
            status_now = SessionStatus.LIVE if live_now else SessionStatus.IDLE
            thinking_now = False
            if live_now:
                tp = self._transcript_path(account, s)
                mt = _mtime(tp) if tp else None
                thinking_now = bool(mt and (now - mt).total_seconds() < THINKING_WINDOW_S)
            pid_now = entry.pid if (entry and live_now) else None
            if status_now != s.status or thinking_now != s.thinking or pid_now != s.pid:
                s.status = status_now
                s.thinking = thinking_now
                s.pid = pid_now
                s.kind = entry.kind if entry else s.kind
                changed.append(s)
        return changed

    # --- usage ---------------------------------------------------------

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await fetch_usage_once(account, client)
        except Exception as exc:  # noqa: BLE001 — one-shot helper, never fatal
            log.debug("fetch_usage failed for %s: %s", account.key, exc)
            return None

    def make_usage_poller(self, account: Account, state, bus, **kwargs):
        return UsagePoller(
            account,
            state,
            interval_s=kwargs.get("interval_s", 300.0),
            cache_dir=kwargs.get("cache_dir"),
        )

    # --- transcripts (v0.2) --------------------------------------------

    def _transcript_path(self, account: Account, session: Session) -> Path | None:
        projects = account.root / "projects"
        if not projects.is_dir():
            return None
        matches = sorted(
            projects.glob(f"*/{session.session_id}.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else None

    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]:
        """New renderable events with seq > after_seq (used by the live tail)."""
        path = self._transcript_path(account, session)
        if path is None:
            return []
        read = transcripts_mod.read_events(path)
        return [e for e in read.events if e.seq > after_seq]

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return (0, 0)
        read = transcripts_mod.read_events(path)
        return (read.byte_offset, read.seq)

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return ([], byte_offset, seq)
        read = transcripts_mod.read_events(path, byte_offset=byte_offset, seq=seq)
        return (read.events, read.byte_offset, read.seq)

    async def load_transcript(
        self, account: Account, session: Session, before_seq: int | None = None
    ) -> TranscriptDetail:
        """Full detail bundle. ``before_seq`` returns the window ending just
        before that seq (for "load earlier"); otherwise the most recent window."""
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
        read = transcripts_mod.read_events(path)
        events = read.events
        tokens = transcripts_mod.token_totals(events)
        model = transcripts_mod.last_model(events)
        todos = transcripts_mod.load_todos(account.root, session.session_id)

        if before_seq is not None:
            events = [e for e in events if e.seq < before_seq]
        window = events[-DETAIL_WINDOW:]
        earliest = window[0].seq if window else 0
        return TranscriptDetail(
            events=window,
            tokens=tokens,
            model=model,
            todos=todos,
            total_events=len(read.events),
            earliest_seq=earliest,
            skipped=read.skipped,
        )

    # --- injection (v0.3) ----------------------------------------------

    async def inject(
        self, account: Account, session: Session, message: str, *, timeout_s: float = 600.0
    ) -> InjectResult:
        return await inject_mod.inject_oneshot(account, session, message, timeout_s=timeout_s)

    # --- interactive chat (v0.3) ---------------------------------------

    async def open_chat(self, account: Account, session: Session) -> ChatSession:
        reason = inject_mod.preflight(account, session)
        if reason is not None:
            raise ChatRefused(reason)
        cs = ChatSession(
            session.session_id, cwd=str(session.cwd), config_dir=str(account.root)
        )
        await cs.start()
        return cs
