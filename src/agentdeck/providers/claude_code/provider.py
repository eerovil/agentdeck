"""ClaudeCodeProvider — translates a $CLAUDE_CONFIG_DIR into agentdeck sessions.

Discovers sessions (live from the pid registry, idle from transcript files),
reads their transcripts, surfaces per-account usage limits, and derives the
claude.ai/code deep link for cloud/RC-spawned sessions. agentdeck is a
read-only viewer — replies happen on claude.ai via the deep link, not here.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

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
from . import history as history_mod
from . import kanban as kanban_mod
from . import registry as registry_mod
from . import transcripts as transcripts_mod
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


def worker_type(is_kanban: bool, has_deep_link: bool) -> str:
    """Classify a session for list colouring: an autonomous kanban worker, a
    cloud/RC-spawned session, or one of your own interactive chats. Kanban wins
    over cloud — a live kanban worker is also RC-spawned, but "kanban" is the
    more useful label."""
    if is_kanban:
        return "kanban"
    if has_deep_link:
        return "cloud"
    return "you"


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
        # (title, last_prompt, first_user, cwd, last_text) cache keyed by path,
        # invalidated by mtime, so idle transcripts aren't re-parsed every scan.
        self._meta_cache: dict[
            str,
            tuple[float, tuple[str | None, ...]],
        ] = {}
        # last-renderable-event cache (mtime-keyed) — the liveness sweep reads it
        # every few seconds per live session, so idle ones must stay a cache hit.
        self._last_ev_cache: dict[str, tuple[float, TranscriptEvent | None]] = {}
        # Context-window occupancy (mtime-keyed), read from the transcript tail —
        # same cheap-cache treatment as meta/last-event so idle sessions cost nothing.
        self._ctx_cache: dict[str, tuple[float, int | None]] = {}
        # Resolves kanban dispatch prompts to real GitHub issue titles.
        self._kanban = kanban_mod.KanbanTitleCache()

    def _cached_meta(self, path: Path) -> tuple[str | None, ...]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return (None, None, None, None, None, None)
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
        ev = transcripts_mod.last_event(path)
        self._last_ev_cache[str(path)] = (mtime, ev)
        return ev

    def _cached_context_tokens(self, path: Path) -> int | None:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._ctx_cache.get(str(path))
        if hit is not None and hit[0] == mtime:
            return hit[1]
        ctx = transcripts_mod.last_context_tokens(path)
        self._ctx_cache[str(path)] = (mtime, ctx)
        return ctx

    def _activity(self, is_live: bool, tpath: Path | None, last_write) -> str | None:
        """Activity label for a live session, from its open turn (cheap, mtime
        -cached tail read). Same logic as the detail page, so list and detail
        agree."""
        if not (is_live and tpath is not None and last_write is not None):
            return None
        age = (datetime.now(UTC) - last_write).total_seconds()
        last_ev = self._cached_last_event(tpath)
        return activity_label(True, age < THINKING_WINDOW_S, last_ev, age)

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

        # Kanban worker sessions carry a fixed dispatch prompt instead of a real
        # title; resolve the referenced GitHub issue title (cached) up front so
        # the build loop below can substitute it. Best-effort — a miss just
        # leaves the existing title fallback in place.
        krefs = []
        for sid in keep:
            tpath = transcripts.get(sid)
            if tpath is None:
                continue
            first_user = self._cached_meta(tpath)[2]
            kref = kanban_mod.parse_ref(first_user)
            if kref is not None:
                krefs.append(kref)
        if krefs:
            try:
                await self._kanban.resolve_missing(krefs, datetime.now(UTC).timestamp())
            except Exception as exc:  # noqa: BLE001 — title polish must never break a scan
                log.debug("kanban title resolve failed: %s", exc)

        sessions: list[Session] = []
        for sid in keep:
            entry = entries.get(sid)
            is_live = alive.get(sid, False)
            h = hist.get(sid)
            tpath = transcripts.get(sid)

            # Title: prefer the transcript's AI-generated title, then history,
            # then the first user message; last_prompt/cwd likewise from the
            # transcript when the registry/history don't have them.
            ai_title = last_prompt = first_user = tcwd = last_text = last_role = None
            if tpath is not None:
                ai_title, last_prompt, first_user, tcwd, last_text, last_role = self._cached_meta(
                    tpath
                )

            cwd: Path | None = None
            if entry and entry.cwd:
                cwd = entry.cwd
            elif h and h.project:
                cwd = Path(h.project)
            elif tcwd:
                # The registry entry is gone once a session is idle; the
                # transcript's cwd is what keeps idle sessions injectable.
                cwd = Path(tcwd)
            title = ai_title or (h.title if h else None) or first_user
            # A kanban dispatch prompt makes a poor title; prefer the resolved
            # issue title, then fall back to the bare "repo#n" reference (which
            # still beats the raw, near-identical dispatch string).
            kref = kanban_mod.parse_ref(first_user)
            issue_url = issue_status = issue_status_kind = None
            if kref is not None:
                issue_url = kanban_mod.issue_url(kref)
                issue_title = self._kanban.get(kref)
                title = (
                    kanban_mod.format_title(kref, issue_title)
                    if issue_title
                    else (ai_title or kanban_mod.format_title(kref, None))
                )
                badge = self._kanban.get_status(kref)
                if badge is not None:
                    issue_status, issue_status_kind = badge
            last_prompt = last_prompt or (h.last_prompt if h else None)
            # When the agent is paused on your answer, surface the question. An
            # unanswered AskUserQuestion (multiple-choice tool) carries its prompt
            # on the last event, not in any text block — prefer it; otherwise fall
            # back to a plain-text question the agent ended its turn on.
            last_ev = self._cached_last_event(tpath) if tpath is not None else None
            question = (last_ev.question if last_ev else None) or (
                transcripts_mod.trailing_question(last_text) if last_role == "agent" else None
            )

            context_tokens = self._cached_context_tokens(tpath) if tpath is not None else None

            last_activity = _mtime(tpath) if tpath else (entry.started_at if entry else None)

            status = SessionStatus.LIVE if is_live else SessionStatus.IDLE
            activity = self._activity(is_live, tpath, last_activity)
            thinking = activity is not None
            # A claude.ai deep link exists only for live, cloud/RC-spawned
            # sessions (the access token lives in the process env).
            deep_link = (
                registry_mod.session_deep_link(entry.pid)
                if (is_live and entry is not None)
                else None
            )

            caps: set[Capability] = set()
            if tpath is not None:
                caps.add(Capability.TRANSCRIPT)  # readable from v0.2
            if deep_link is not None:
                caps.add(Capability.DEEPLINK)

            wtype = worker_type(kref is not None, deep_link is not None)

            sessions.append(
                Session(
                    key=f"{account.key}:{sid}",
                    account_key=account.key,
                    session_id=sid,
                    status=status,
                    thinking=thinking,
                    activity=activity,
                    cwd=cwd,
                    title=title,
                    last_prompt=last_prompt,
                    last_text=last_text,
                    last_role=last_role,
                    question=question,
                    kind=entry.kind if entry else None,
                    worker_type=wtype,
                    issue_url=issue_url,
                    issue_status=issue_status,
                    issue_status_kind=issue_status_kind,
                    pid=entry.pid if (entry and is_live) else None,
                    proc_start=entry.proc_start if entry else None,
                    started_at=entry.started_at if entry else None,
                    last_activity=last_activity,
                    context_tokens=context_tokens,
                    deep_link=deep_link,
                    capabilities=frozenset(caps),
                )
            )
        return sessions

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Cheap recheck (~every 10s): flip LIVE↔IDLE and refresh the activity
        (busy) state from the open turn, so both update between full scans."""
        entries = {e.session_id: e for e in registry_mod.read_registry(account.root)}
        changed: list[Session] = []
        for s in sessions:
            entry = entries.get(s.session_id)
            live_now = bool(entry and registry_mod.is_alive(entry))
            status_now = SessionStatus.LIVE if live_now else SessionStatus.IDLE
            activity_now = None
            last_prompt_now = s.last_prompt
            last_text_now = s.last_text
            last_role_now = s.last_role
            context_now = s.context_tokens
            last_ev_now = None
            if live_now:
                tp = self._transcript_path(account, s)
                activity_now = self._activity(True, tp, _mtime(tp) if tp else None)
                if tp is not None:
                    # keep the list's messages as fresh as the detail tail — this
                    # is the mtime-cached meta, so idle transcripts cost nothing.
                    _at, lp, _fu, _cwd, lt, lr = self._cached_meta(tp)
                    last_prompt_now = lp or s.last_prompt
                    last_text_now = lt or s.last_text
                    last_role_now = lr or s.last_role
                    last_ev_now = self._cached_last_event(tp)  # cache hit (_activity read it)
                    context_now = self._cached_context_tokens(tp)
            if last_ev_now is not None and last_ev_now.question:
                # Unanswered AskUserQuestion → surface its prompt (see scan_sessions).
                question_now = last_ev_now.question
            elif live_now:
                question_now = (
                    transcripts_mod.trailing_question(last_text_now)
                    if last_role_now == "agent"
                    else None
                )
            else:
                # Idle: no fresh read this sweep — keep whatever the last full scan
                # surfaced rather than recomputing from stale text.
                question_now = s.question
            thinking_now = activity_now is not None
            pid_now = entry.pid if (entry and live_now) else None
            deep_link_now = (
                registry_mod.session_deep_link(entry.pid) if (entry and live_now) else None
            )
            if (
                status_now != s.status
                or thinking_now != s.thinking
                or activity_now != s.activity
                or last_prompt_now != s.last_prompt
                or last_text_now != s.last_text
                or last_role_now != s.last_role
                or question_now != s.question
                or context_now != s.context_tokens
                or pid_now != s.pid
                or deep_link_now != s.deep_link
            ):
                s.status = status_now
                s.thinking = thinking_now
                s.activity = activity_now
                s.last_prompt = last_prompt_now
                s.last_text = last_text_now
                s.last_role = last_role_now
                s.question = question_now
                s.context_tokens = context_now
                s.pid = pid_now
                s.deep_link = deep_link_now
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
        return await asyncio.to_thread(self._read_transcript_file, path, after_seq)

    def _read_transcript_file(self, path: Path, after_seq: int) -> list[TranscriptEvent]:
        """Read and filter a transcript outside the caller's event loop."""
        read = transcripts_mod.read_events(path)
        return [event for event in read.events if event.seq > after_seq]

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        path = self._transcript_path(account, session)
        return (
            await asyncio.to_thread(transcripts_mod.last_event, path)
            if path is not None
            else None
        )

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return (0, 0)
        return await asyncio.to_thread(transcripts_mod.transcript_cursor, path)

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        path = self._transcript_path(account, session)
        if path is None:
            return ([], byte_offset, seq)
        read = await asyncio.to_thread(
            transcripts_mod.read_events, path, byte_offset=byte_offset, seq=seq
        )
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
        return await asyncio.to_thread(
            self._load_transcript_file,
            account.root,
            session.session_id,
            path,
            before_seq,
        )

    def _load_transcript_file(
        self, root: Path, session_id: str, path: Path, before_seq: int | None
    ) -> TranscriptDetail:
        """Build a detail window without blocking the caller's event loop."""
        read = transcripts_mod.read_events(path)
        all_events = read.events
        events = all_events
        if before_seq is not None:
            events = [event for event in events if event.seq < before_seq]
        window = events[-DETAIL_WINDOW:]
        return TranscriptDetail(
            events=window,
            tokens=transcripts_mod.token_totals(all_events),
            model=transcripts_mod.last_model(all_events),
            todos=transcripts_mod.load_todos(root, session_id),
            total_events=len(all_events),
            earliest_seq=window[0].seq if window else 0,
            skipped=read.skipped,
        )
