"""CodexProvider — translates a CODEX_HOME into agentdeck sessions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

from ...models import (
    Account,
    Capability,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
    detailed_activity_label,
)
from ..base import SessionProvider
from . import transcripts as transcripts_mod
from .appserver import CodexAppServer
from .inject import (
    inject_session,
    is_injectable_rollout,
)
from .inject import (
    start_session as start_codex_session,
)
from .usage import UsagePoller, fetch_usage_once

DETAIL_WINDOW = 400
MAX_SESSIONS = 200
LIVE_WINDOW_S = 30.0

log = logging.getLogger(__name__)


def _display_kind(kind: str | None) -> str | None:
    """Return only source kinds that add useful information to a card."""
    return kind if kind == "exec" else None


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
    supports_new_session = True

    def __init__(self) -> None:
        self._meta_cache: dict[str, tuple[float, transcripts_mod.TranscriptMeta]] = {}
        self._last_ev_cache: dict[str, tuple[float, TranscriptEvent | None]] = {}
        self._paths: dict[tuple[str, str], Path] = {}
        self._clients: dict[str, CodexAppServer] = {}
        self._states = {}

    async def start_account(self, account: Account, state) -> None:
        client = CodexAppServer(
            account,
            on_change=lambda thread_id: self._runtime_changed(account, state, thread_id),
        )
        self._clients[account.key] = client
        self._states[account.key] = state
        await client.start()

    async def stop_account(self, account: Account) -> None:
        client = self._clients.pop(account.key, None)
        self._states.pop(account.key, None)
        if client is not None:
            await client.stop()

    def _runtime_changed(self, account: Account, state, thread_id: str) -> None:
        session = state.sessions.get(f"{account.key}:{thread_id}")
        client = self._clients.get(account.key)
        if session is None or client is None or not client.owns(thread_id):
            state.bus.publish("sessions")
            return
        active = client.active_turn(thread_id) is not None
        interaction = client.interaction(thread_id)
        path = self._transcript_path(account, session)
        last_event = self._cached_last_event(path) if path is not None else None
        session.status = SessionStatus.LIVE if active else SessionStatus.IDLE
        session.thinking = active and interaction is None
        session.activity = (
            "Waiting for you"
            if interaction
            else (
                detailed_activity_label("Using tools", last_event)
                if active and last_event is not None and last_event.tool_name
                else ("Working" if active else None)
            )
        )
        session.question = self._interaction_summary(interaction)
        session.kind = "appServer"
        session.capabilities = self._runtime_capabilities(account, thread_id)
        state.bus.publish("sessions")

    @staticmethod
    def _interaction_summary(interaction: PendingInteraction | None) -> str | None:
        if interaction is None:
            return None
        if interaction.questions:
            return " ".join(question.prompt for question in interaction.questions)
        return interaction.message or interaction.title

    def _runtime_capabilities(self, account: Account, thread_id: str) -> frozenset[Capability]:
        client = self._clients.get(account.key)
        capabilities = {Capability.TRANSCRIPT}
        if client is None or not client.owns(thread_id):
            return frozenset(capabilities)
        active = client.active_turn(thread_id) is not None
        capabilities.add(Capability.INJECT)
        if active:
            capabilities.update({Capability.STEER, Capability.INTERRUPT})
        if client.interaction(thread_id) is not None:
            capabilities.add(Capability.INTERACT)
        return frozenset(capabilities)

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
            meta = self._cached_meta(path)
            if not (meta.is_approval_review or meta.is_subagent):
                return path
        for candidate in _list_rollouts(account.root):
            meta = self._cached_meta(candidate)
            if (
                meta.session_id == session.session_id
                and not meta.is_approval_review
                and not meta.is_subagent
            ):
                self._paths[(account.key, session.session_id)] = candidate
                return candidate
        return None

    def _capabilities(
        self, account: Account, session_id: str, path: Path, meta, status: SessionStatus
    ) -> frozenset[Capability]:
        client = self._clients.get(account.key)
        if client is not None and client.owns(session_id):
            return self._runtime_capabilities(account, session_id)
        capabilities = {Capability.TRANSCRIPT}
        cwd_exists = bool(meta.cwd and Path(meta.cwd).is_dir())
        if status == SessionStatus.IDLE and cwd_exists and is_injectable_rollout(path, meta.kind):
            capabilities.add(Capability.INJECT)
        return frozenset(capabilities)

    def _derived_state(
        self, path: Path, last_activity: datetime | None
    ) -> tuple[SessionStatus, bool, str | None]:
        if last_activity is None:
            return (SessionStatus.IDLE, False, None)
        age = max(0.0, (datetime.now(UTC) - last_activity).total_seconds())
        live = age < LIVE_WINDOW_S
        status = SessionStatus.LIVE if live else SessionStatus.IDLE
        last_event = self._cached_last_event(path)
        activity = detailed_activity_label(activity_label(live, live, last_event, age), last_event)
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
            # Internal helpers reuse the parent chat's session_id. Reject both
            # legacy approval-review and structured sub-agent rollouts before
            # ID deduplication or the newest helper can replace the real chat.
            if meta.is_approval_review or meta.is_subagent:
                continue
            session_id = meta.session_id
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            current_paths[(account.key, session_id)] = path
            last_activity = _mtime(path)
            status, thinking, activity = self._derived_state(path, last_activity)
            last_event = self._cached_last_event(path)
            client = self._clients.get(account.key)
            interaction = None
            if client is not None and client.owns(session_id):
                active = client.active_turn(session_id) is not None
                interaction = client.interaction(session_id)
                status = SessionStatus.LIVE if active else SessionStatus.IDLE
                thinking = active and interaction is None
                activity = (
                    "Waiting for you"
                    if interaction
                    else (
                        detailed_activity_label("Using tools", last_event)
                        if active and last_event is not None and last_event.tool_name
                        else ("Working" if active else None)
                    )
                )
            sessions.append(
                Session(
                    key=f"{account.key}:{session_id}",
                    account_key=account.key,
                    session_id=session_id,
                    status=status,
                    thinking=thinking,
                    activity=activity,
                    question=self._interaction_summary(interaction),
                    cwd=Path(meta.cwd) if meta.cwd else None,
                    title=meta.title,
                    last_prompt=meta.last_prompt,
                    last_text=meta.last_text,
                    last_role=meta.last_role,
                    model=meta.model,
                    kind=(
                        "appServer"
                        if client is not None and client.owns(session_id)
                        else _display_kind(meta.kind)
                    ),
                    worker_type="you",
                    started_at=meta.started_at,
                    last_activity=last_activity,
                    tokens=meta.tokens,
                    context_tokens=meta.context_tokens,
                    show_when_idle=True,
                    capabilities=self._capabilities(account, session_id, path, meta, status),
                )
            )
        self._paths = {key: value for key, value in self._paths.items() if key[0] != account.key}
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
            last_event = self._cached_last_event(path)
            client = self._clients.get(account.key)
            interaction = None
            if client is not None and client.owns(session.session_id):
                active = client.active_turn(session.session_id) is not None
                interaction = client.interaction(session.session_id)
                status = SessionStatus.LIVE if active else SessionStatus.IDLE
                thinking = active and interaction is None
                activity = (
                    "Waiting for you"
                    if interaction
                    else (
                        detailed_activity_label("Using tools", last_event)
                        if active and last_event is not None and last_event.tool_name
                        else ("Working" if active else None)
                    )
                )
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
                self._capabilities(account, session.session_id, path, meta, status),
                self._interaction_summary(interaction),
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
                session.capabilities,
                session.question,
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
                session.capabilities,
                session.question,
            ) = values
            changed.append(session)
        return changed

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        try:
            return await fetch_usage_once(account)
        except Exception as exc:  # noqa: BLE001 -- one failed usage read is non-fatal
            log.debug("fetch_usage failed for %s: %s", account.key, exc)
            return None

    def make_usage_poller(self, account: Account, state, bus, **kwargs):
        return UsagePoller(
            account,
            state,
            interval_s=kwargs.get("interval_s", 300.0),
            cache_dir=kwargs.get("cache_dir"),
        )

    async def inject(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is not None and client.owns(session.session_id):
            return await client.queue_turn(session.session_id, message, images=images)
        path = self._transcript_path(account, session)
        if path is None:
            return InjectResult(False, "session rollout no longer exists")
        return await inject_session(
            account,
            session,
            path,
            message,
            timeout_s=timeout_s,
            images=images,
        )

    async def start_session(
        self,
        account: Account,
        cwd: Path,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is not None:
            result = await client.start_thread(
                cwd,
                message,
                images=images,
                sandbox=sandbox,
                model=model,
                approval_policy=approval_policy,
            )
            if result.accepted and result.session_id:
                state = self._states.get(account.key)
                if state is not None:
                    state.update_session(
                        Session(
                            key=f"{account.key}:{result.session_id}",
                            account_key=account.key,
                            session_id=result.session_id,
                            status=SessionStatus.LIVE,
                            thinking=True,
                            activity="Working",
                            cwd=cwd,
                            title=message[:200],
                            last_prompt=message,
                            last_role="user",
                            kind="appServer",
                            worker_type="you",
                            started_at=datetime.now(UTC),
                            last_activity=datetime.now(UTC),
                            show_when_idle=True,
                            capabilities=self._runtime_capabilities(account, result.session_id),
                        )
                    )
            return result
        return await start_codex_session(
            account,
            cwd,
            message,
            timeout_s=timeout_s,
            images=images,
        )

    async def wait_for_session(
        self,
        account: Account,
        session_id: str,
        *,
        timeout_s: float,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            # The fallback ``codex exec`` start call only returns after its turn.
            return InjectResult(True)
        try:
            return await asyncio.wait_for(
                client.wait_for_thread(session_id),
                timeout=timeout_s,
            )
        except TimeoutError:
            return InjectResult(False, "Codex delegation timed out")

    async def session_result(self, account: Account, session_id: str) -> str | None:
        session = Session(
            key=f"{account.key}:{session_id}",
            account_key=account.key,
            session_id=session_id,
            status=SessionStatus.IDLE,
        )
        path = await asyncio.to_thread(self._transcript_path, account, session)
        if path is None:
            return None
        meta = await asyncio.to_thread(transcripts_mod.transcript_meta, path)
        # Prefer the turn's canonical final message (task_complete.last_agent_message);
        # fall back to the last assistant item only if no completed turn is present.
        return meta.last_agent_message or meta.last_text

    def pending_interaction(self, account: Account, session: Session) -> PendingInteraction | None:
        client = self._clients.get(account.key)
        return client.interaction(session.session_id) if client is not None else None

    def owns_session(self, account: Account, session: Session) -> bool:
        client = self._clients.get(account.key)
        return bool(client and client.owns(session.session_id))

    async def steer(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        images: list[Path] | None = None,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            return InjectResult(False, "Codex app-server is unavailable")
        return await client.steer(session.session_id, message, images=images)

    async def interrupt(self, account: Account, session: Session) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            return InjectResult(False, "Codex app-server is unavailable")
        return await client.interrupt(session.session_id)

    async def answer_interaction(
        self,
        account: Account,
        session: Session,
        interaction_id: str,
        *,
        answers,
        decision: str | None,
    ) -> InjectResult:
        client = self._clients.get(account.key)
        if client is None:
            return InjectResult(False, "Codex app-server is unavailable")
        return await client.answer(
            session.session_id,
            interaction_id,
            answers=answers,
            decision=decision,
        )

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
