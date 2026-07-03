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
    ChatHandle,
    InjectResult,
    Session,
    SessionStatus,
    TranscriptEvent,
    UsageSnapshot,
)
from ..base import SessionProvider
from . import history as history_mod
from . import registry as registry_mod
from .usage import UsagePoller, fetch_usage_once

log = logging.getLogger(__name__)

# Upper bound on idle sessions surfaced per account in v0.1 — the projects/ dir
# can hold thousands of transcripts; show the most recently active.
MAX_IDLE_SESSIONS = 200


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

            last_activity = _mtime(tpath) if tpath else (entry.started_at if entry else None)

            status = SessionStatus.LIVE if is_live else SessionStatus.IDLE
            caps: set[Capability] = set()
            if tpath is not None:
                caps.add(Capability.TRANSCRIPT)  # readable from v0.2

            sessions.append(
                Session(
                    key=f"{account.key}:{sid}",
                    account_key=account.key,
                    session_id=sid,
                    status=status,
                    cwd=cwd,
                    title=h.title if h else None,
                    last_prompt=h.last_prompt if h else None,
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
        """Cheap recheck: flip LIVE→IDLE for sessions whose pid has died."""
        entries = {e.session_id: e for e in registry_mod.read_registry(account.root)}
        changed: list[Session] = []
        for s in sessions:
            entry = entries.get(s.session_id)
            live_now = bool(entry and registry_mod.is_alive(entry))
            status_now = SessionStatus.LIVE if live_now else SessionStatus.IDLE
            if status_now != s.status or (live_now and s.pid != entry.pid):
                s.status = status_now
                s.pid = entry.pid if (entry and live_now) else None
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

    # --- v0.2 / v0.3 (not yet implemented) -----------------------------

    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]:
        raise NotImplementedError("transcript parsing lands in v0.2")

    async def inject(self, account: Account, session: Session, message: str) -> InjectResult:
        raise NotImplementedError("message injection lands in v0.3")

    async def open_chat(self, account: Account, session: Session) -> ChatHandle:
        raise NotImplementedError("interactive chat lands in v0.3")
