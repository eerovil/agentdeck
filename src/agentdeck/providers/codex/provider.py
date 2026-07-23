"""CodexProvider — translates a CODEX_HOME into agentdeck sessions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ...models import (
    STALL_S,
    Account,
    Capability,
    InjectResult,
    PendingInteraction,
    Session,
    SessionStatus,
    SubagentProgress,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
    detailed_activity_label,
    event_progress_at,
    event_turn_open,
    runtime_control_capabilities,
    runtime_turn_state,
    turn_stalled,
)
from .._filecache import FileCache, mtime_sig, size_mtime_sig
from ..base import ModelChoice, SessionProvider
from . import transcripts as transcripts_mod
from .inject import (
    inject_session,
    is_injectable_rollout,
)
from .inject import (
    start_session as start_codex_session,
)
from .runtime_client import CodexRuntimeClient
from .usage import CodexUsagePoller, fetch_usage_once

if TYPE_CHECKING:
    from ...state import AppState

DETAIL_WINDOW = 400
MAX_SESSIONS = 200
LIVE_WINDOW_S = 30.0
SUBAGENT_QUIET_S = 300.0
SUBAGENT_STALL_S = STALL_S
RECENT_SUBAGENT_S = 30 * 60.0
MAX_SUBAGENTS_SHOWN = 4

log = logging.getLogger(__name__)


def _display_kind(kind: str | None) -> str | None:
    """Return only source kinds that add useful information to a card."""
    return kind if kind == "exec" else None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def _all_rollouts(root: Path) -> list[Path]:
    sessions = root / "sessions"
    if not sessions.is_dir():
        return []
    return list(sessions.glob("*/*/*/rollout-*.jsonl"))


def _list_rollouts(root: Path) -> list[Path]:
    found = []
    for path in _all_rollouts(root):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found[:MAX_SESSIONS]]


def _compact_line(value: str | None, limit: int = 220) -> str | None:
    if not value:
        return None
    line = next((part.strip() for part in value.splitlines() if part.strip()), "")
    return line[:limit] or None


class CodexProvider(SessionProvider):
    """Read Codex CLI rollouts.

    Codex exposes no process-to-session PID registry. LIVE therefore means the
    rollout was written within ``LIVE_WINDOW_S``; a quiet process waiting for
    input may be reported IDLE, and a just-finished process briefly remains LIVE.
    """

    provider_id = "codex"
    supports_new_session = True
    # Chosen at thread/start (new chat only); the app-server turn protocol has no
    # model field, so a running thread keeps its creation model.
    selectable_models = (
        ModelChoice("gpt-5.6-luna", "Luna"),
        ModelChoice("gpt-5.6-terra", "Terra"),
        ModelChoice("gpt-5.6-sol", "Sol"),
    )

    def __init__(self) -> None:
        self._meta_cache: FileCache[transcripts_mod.TranscriptMeta] = FileCache(mtime_sig)
        self._last_ev_cache: FileCache[TranscriptEvent | None] = FileCache(size_mtime_sig)
        self._delegation_cache: FileCache[frozenset[str]] = FileCache(size_mtime_sig)
        self._paths: dict[tuple[str, str], Path] = {}
        self._clients: dict[str, CodexRuntimeClient] = {}
        self._states = {}
        self._runtime_tasks: dict[str, asyncio.Task] = {}
        self._subagent_names: dict[str, str] = {}
        # Track per-account runtime-refresh health so a persistent outage warns
        # once (on the healthy->failing edge) instead of every scan interval.
        self._refresh_ok: dict[str, bool] = {}

    async def start_account(self, account: Account, state) -> None:
        client = CodexRuntimeClient(
            account,
            on_change=lambda thread_id: self._runtime_changed(account, state, thread_id),
        )
        self._clients[account.key] = client
        self._states[account.key] = state
        await client.start()
        self._refresh_ok[account.key] = True
        self._runtime_tasks[account.key] = asyncio.create_task(
            self._watch_runtime(account, state, client),
            name=f"codex-runtime:{account.key}",
        )

    async def _watch_runtime(self, account, state, client: CodexRuntimeClient) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await client.refresh()
            except Exception as exc:  # noqa: BLE001 -- reconnect on the next poll
                was_ok = self._refresh_ok.get(account.key, True)
                self._refresh_ok[account.key] = False
                if was_ok:
                    # Set the flag first so the reprojection withdraws controls,
                    # then let AppState publish once if any session changed.
                    self._reproject_runtime_sessions(account, state, client)
                log.debug("Codex runtime refresh failed for %s: %s", account.key, exc)
            else:
                was_ok = self._refresh_ok.get(account.key, True)
                self._refresh_ok[account.key] = True
                if not was_ok:
                    self._reproject_runtime_sessions(account, state, client)

    def _reproject_runtime_sessions(
        self, account: Account, state, client: CodexRuntimeClient
    ) -> list[Session]:
        targets = [
            session
            for session in state.sessions.values()
            if session.account_key == account.key
            and session.parent_session_key is None
            and (client.owns(session.session_id) or session.kind == "appServer")
        ]
        return state.apply_session_changes(
            targets, lambda session: self._project_runtime_session(account, session)
        )

    async def stop_account(self, account: Account) -> None:
        task = self._runtime_tasks.pop(account.key, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        client = self._clients.pop(account.key, None)
        self._states.pop(account.key, None)
        self._refresh_ok.pop(account.key, None)
        if client is not None:
            await client.stop()

    def _runtime_changed(self, account: Account, state, thread_id: str) -> None:
        session = state.sessions.get(f"{account.key}:{thread_id}")
        targets = [session] if session is not None else []
        state.apply_session_changes(
            targets, lambda session: self._project_runtime_session(account, session)
        )

    def _project_runtime_session(
        self,
        account: Account,
        session: Session,
        path: Path | None = None,
    ) -> bool:
        """Project transcript baseline plus any owned runtime overlay."""
        path = path or self._transcript_path(account, session)
        if path is not None:
            last_activity = _mtime(path)
            meta = self._cached_meta(path)
            lifecycle_active = (
                meta.task_active if meta.task_status is not None else None
            )
            status, thinking, activity, stalled, last_progress = self._derived_state(
                path,
                last_activity,
                lifecycle_active=lifecycle_active,
                turn_started_at=meta.task_started_at,
            )
            session.status = status
            session.thinking = thinking
            session.stalled = stalled
            session.lifecycle_active = lifecycle_active
            session.activity = activity
            session.last_activity = last_activity
            session.last_progress = last_progress
            session.last_prompt = meta.last_prompt
            session.last_text = meta.last_text
            session.last_role = meta.last_role
            session.model = meta.model
            session.tokens = meta.tokens
            session.context_tokens = meta.context_tokens
            session.capabilities = self._capabilities(
                account, session.session_id, path, meta, status
            )
            session.kind = _display_kind(meta.kind)
            last_event = self._cached_last_event(path)
        else:
            session.status = SessionStatus.IDLE
            session.thinking = False
            session.stalled = False
            session.lifecycle_active = None
            session.activity = None
            session.capabilities = frozenset()
            session.kind = None
            last_event = None
        session.question = None
        client = self._clients.get(account.key)
        if client is None or not client.owns(session.session_id):
            return False
        active = client.active_turn(session.session_id) is not None
        interaction = self._actionable_interaction(account, session.session_id)
        progress = event_progress_at(
            last_event,
            session.last_progress or session.last_activity or session.started_at,
        )
        if active and progress is None:
            # First runtime observation before a transcript exists: establish a
            # durable local progress anchor once, then retain it across sweeps.
            progress = datetime.now(UTC)
        age = (datetime.now(UTC) - progress).total_seconds() if progress else 1e9
        stalled = turn_stalled(
            live=True,
            lifecycle_active=active,
            last_ev=last_event,
            age_s=age,
        )
        session.status = SessionStatus.LIVE if active else SessionStatus.IDLE
        session.lifecycle_active = active
        session.stalled, session.thinking = runtime_turn_state(
            active_turn=active,
            actionable_interaction=interaction is not None,
            stalled_evidence=stalled,
        )
        session.last_progress = progress
        session.activity = (
            "Waiting for you"
            if interaction
            else detailed_activity_label(
                activity_label(
                    active,
                    active,
                    last_event,
                    age,
                    lifecycle_active=active,
                ),
                last_event,
            )
        )
        session.question = self._interaction_summary(interaction)
        session.kind = "appServer"
        session.capabilities = self._runtime_capabilities(account, session.session_id)
        return True

    @staticmethod
    def _interaction_summary(interaction: PendingInteraction | None) -> str | None:
        if interaction is None:
            return None
        if interaction.questions:
            return " ".join(question.prompt for question in interaction.questions)
        return interaction.message or interaction.title

    def _actionable_interaction(
        self, account: Account, thread_id: str
    ) -> PendingInteraction | None:
        client = self._clients.get(account.key)
        if (
            client is None
            or not self._refresh_ok.get(account.key, True)
            or not client.owns(thread_id)
        ):
            return None
        return client.interaction(thread_id)

    def _runtime_capabilities(self, account: Account, thread_id: str) -> frozenset[Capability]:
        client = self._clients.get(account.key)
        available = (
            client is not None
            and self._refresh_ok.get(account.key, True)
            and client.owns(thread_id)
        )
        return frozenset({Capability.TRANSCRIPT}) | runtime_control_capabilities(
            available=available,
            active_turn=available and client.active_turn(thread_id) is not None,
            actionable_interaction=self._actionable_interaction(account, thread_id)
            is not None,
        )

    def _cached_meta(self, path: Path) -> transcripts_mod.TranscriptMeta:
        return self._meta_cache.get(
            path, transcripts_mod.transcript_meta, transcripts_mod.TranscriptMeta()
        )

    def _cached_last_event(self, path: Path) -> TranscriptEvent | None:
        return self._last_ev_cache.get(path, transcripts_mod.last_event, None)

    def _cached_delegated_session_keys(self, path: Path) -> frozenset[str]:
        return self._delegation_cache.get(
            path, transcripts_mod.delegated_session_keys, frozenset()
        )

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
        self,
        path: Path,
        last_activity: datetime | None,
        *,
        lifecycle_active: bool | None = None,
        turn_started_at: datetime | None = None,
    ) -> tuple[SessionStatus, bool, str | None, bool, datetime | None]:
        if last_activity is None:
            return (SessionStatus.IDLE, False, None, False, None)
        write_age = max(0.0, (datetime.now(UTC) - last_activity).total_seconds())
        live = write_age < LIVE_WINDOW_S
        status = SessionStatus.LIVE if live else SessionStatus.IDLE
        last_event = self._cached_last_event(path)
        progress = event_progress_at(last_event, last_activity)
        if lifecycle_active and turn_started_at is not None and (
            progress is None or progress < turn_started_at
        ):
            progress = turn_started_at
        age = max(0.0, (datetime.now(UTC) - progress).total_seconds()) if progress else 1e9
        # Unowned rollouts have no process lifecycle feed. Structural open-turn
        # evidence therefore sustains Working through quiet tool gaps.
        evidence_available = (
            live or lifecycle_active is True or event_turn_open(last_event) is True
        )
        stalled = turn_stalled(
            live=evidence_available,
            lifecycle_active=lifecycle_active,
            last_ev=last_event,
            age_s=age,
        )
        activity = detailed_activity_label(
            activity_label(
                evidence_available,
                live,
                last_event,
                age,
                lifecycle_active=lifecycle_active,
            ),
            last_event,
        )
        return (status, activity is not None, activity, stalled, progress)

    @staticmethod
    def _apply_model(events: list[TranscriptEvent], model: str | None) -> list[TranscriptEvent]:
        for event in events:
            if event.role == "assistant" and event.model is None:
                event.model = model
        return events

    def _decorate_events(
        self, events: list[TranscriptEvent], model: str | None
    ) -> list[TranscriptEvent]:
        self._apply_model(events, model)
        for event in events:
            for agent_id, nickname in event.subagent_identities:
                self._subagent_names[agent_id] = nickname
            if event.subagent_id and event.subagent_name is None:
                event.subagent_name = self._subagent_names.get(event.subagent_id)
        return events

    def watch_paths(self, account: Account) -> list[Path]:
        sessions = account.root / "sessions"
        return [sessions] if sessions.exists() else []

    async def scan_sessions(self, account: Account) -> list[Session]:
        client = self._clients.get(account.key)
        if client is not None:
            try:
                await client.refresh()
            except Exception as exc:  # noqa: BLE001 -- transcript scan remains useful
                # Warn only on the healthy->failing edge: a dead runtime client
                # freezes cached liveness (finished sessions stay "working"), so
                # it must be visible, but scan runs every few seconds and a real
                # outage would otherwise flood the log.
                if self._refresh_ok.get(account.key, True):
                    log.warning(
                        "Codex runtime unavailable during scan for %s: %s",
                        account.key,
                        exc,
                    )
                self._refresh_ok[account.key] = False
            else:
                if not self._refresh_ok.get(account.key, True):
                    log.info("Codex runtime scan recovered for %s", account.key)
                self._refresh_ok[account.key] = True
        sessions = []
        subagents: dict[str, list[SubagentProgress]] = {}
        turn_starts: dict[str, datetime] = {}
        current_paths: dict[tuple[str, str], Path] = {}
        seen: set[str] = set()
        rollouts = [
            (path, self._cached_meta(path)) for path in _list_rollouts(account.root)
        ]
        # A parent chat that delegated a Codex session via AgentDeck records a
        # "delegation: running (/sessions/<child-key>)" marker in its own rollout.
        # Collect both the flat set (for is_delegated) and the child->parent map
        # so delegated sessions nest under the chat that spawned them.
        delegated_session_keys: set[str] = set()
        delegation_parent: dict[str, str] = {}
        for path in _all_rollouts(account.root):
            children = self._cached_delegated_session_keys(path)
            if not children:
                continue
            delegated_session_keys.update(children)
            parent_meta = self._cached_meta(path)
            if parent_meta.is_subagent or not parent_meta.session_id:
                continue
            parent_key = f"{account.key}:{parent_meta.session_id}"
            for child_key in children:
                if child_key != parent_key:
                    delegation_parent.setdefault(child_key, parent_key)
        for path, meta in rollouts:
            if meta.is_spawned_subagent:
                if meta.agent_id and meta.agent_nickname:
                    self._subagent_names[meta.agent_id] = meta.agent_nickname
                last_activity = _mtime(path)
                age = (
                    (datetime.now(UTC) - last_activity).total_seconds()
                    if last_activity
                    else 1e9
                )
                if meta.session_id and (meta.task_active or age < RECENT_SUBAGENT_S):
                    status = (
                        ("working" if age < SUBAGENT_QUIET_S else "quiet")
                        if meta.task_active
                        else (meta.task_status or "finished")
                    )
                    subagents.setdefault(meta.session_id, []).append(
                        SubagentProgress(
                            agent_id=meta.agent_id or path.stem[-36:],
                            nickname=meta.agent_nickname,
                            role=meta.agent_role,
                            task=meta.title,
                            status=status,
                            result=_compact_line(meta.last_agent_message or meta.last_text),
                            started_at=meta.started_at,
                            updated_at=last_activity,
                        )
                    )
                    # Also surface it as a real child session so it nests under
                    # the parent in the list tree. A spawned rollout reuses the
                    # parent's session_id (meta.session_id); its OWN id is
                    # meta.agent_id (session_meta.id).
                    child_id = meta.agent_id
                    if child_id and child_id not in seen:
                        seen.add(child_id)
                        # "Active" means it actually wrote recently — the
                        # task_active flag alone gets stuck True on a subagent
                        # that died mid-task, showing "Working" for hours and
                        # flooding the working count + Deckhand stalls.
                        child_active = meta.task_active and age < SUBAGENT_STALL_S
                        sessions.append(
                            Session(
                                key=f"{account.key}:{child_id}",
                                account_key=account.key,
                                session_id=child_id,
                                status=(
                                    SessionStatus.LIVE if child_active else SessionStatus.IDLE
                                ),
                                thinking=child_active,
                                activity="Working" if child_active else None,
                                title=meta.agent_nickname or meta.title,
                                last_text=meta.last_text,
                                last_role=meta.last_role,
                                cwd=Path(meta.cwd) if meta.cwd else None,
                                model=meta.model,
                                kind="subagent",
                                worker_type="you",
                                # quiet/finished/stuck subagents stay nested (under
                                # a visible parent) but out of Deckhand triage +
                                # the working count; only a recently-active one is
                                # attention-worthy.
                                is_delegated=not child_active,
                                parent_session_key=f"{account.key}:{meta.session_id}",
                                started_at=meta.started_at,
                                last_activity=last_activity,
                                show_when_idle=True,
                            )
                        )
                        current_paths[(account.key, child_id)] = path
            # Internal helpers reuse the parent chat's session_id. Reject both
            # legacy approval-review and structured sub-agent rollouts before
            # ID deduplication or the newest helper can replace the real chat.
            if meta.is_approval_review or meta.is_subagent:
                continue
            session_id = meta.session_id
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            if meta.task_started_at:
                turn_starts[session_id] = meta.task_started_at
            current_paths[(account.key, session_id)] = path
            last_activity = _mtime(path)
            lifecycle_active = (
                meta.task_active if meta.task_status is not None else None
            )
            status, thinking, activity, stalled, last_progress = self._derived_state(
                path,
                last_activity,
                lifecycle_active=lifecycle_active,
                turn_started_at=meta.task_started_at,
            )
            session = Session(
                key=f"{account.key}:{session_id}",
                account_key=account.key,
                session_id=session_id,
                status=status,
                thinking=thinking,
                stalled=stalled,
                lifecycle_active=lifecycle_active,
                activity=activity,
                cwd=Path(meta.cwd) if meta.cwd else None,
                title=meta.title,
                initial_prompt=meta.first_prompt,
                last_prompt=meta.last_prompt,
                last_text=meta.last_text,
                last_role=meta.last_role,
                model=meta.model,
                kind=_display_kind(meta.kind),
                worker_type="you",
                is_delegated=(
                    meta.kind == "exec"
                    or f"{account.key}:{session_id}" in delegated_session_keys
                ),
                # An AgentDeck-delegated session nests under the chat that
                # delegated it (same treatment as a spawned subagent).
                parent_session_key=delegation_parent.get(
                    f"{account.key}:{session_id}"
                ),
                started_at=meta.started_at,
                last_activity=last_activity,
                last_progress=last_progress,
                tokens=meta.tokens,
                context_tokens=meta.context_tokens,
                show_when_idle=True,
                capabilities=self._capabilities(account, session_id, path, meta, status),
            )
            self._project_runtime_session(account, session, path)
            sessions.append(session)
        for session in sessions:
            agents = subagents.get(session.session_id, [])
            turn_started = turn_starts.get(session.session_id)
            if turn_started is not None:
                agents = [
                    agent
                    for agent in agents
                    if agent.started_at is None or agent.started_at >= turn_started
                ]
            agents.sort(
                key=lambda item: (
                    item.status not in ("working", "quiet"),
                    -(item.updated_at.timestamp() if item.updated_at else 0.0),
                )
            )
            session.subagents = tuple(agents[:MAX_SUBAGENTS_SHOWN])
            now = datetime.now(UTC)
            active_agent_progress = [
                stamp
                for agent in agents
                if agent.status in ("working", "quiet")
                and (stamp := agent.updated_at or agent.started_at) is not None
                and (now - stamp).total_seconds() < SUBAGENT_STALL_S
            ]
            session.subagent_count = len(active_agent_progress)
        self._paths = {key: value for key, value in self._paths.items() if key[0] != account.key}
        self._paths.update(current_paths)
        # Publish this account's delegation links so cross-provider children
        # (e.g. a Codex chat that delegated a Claude session) nest correctly.
        state = self._states.get(account.key)
        if state is not None:
            state.set_delegation_parents(account.key, delegation_parent)
        return sessions

    def sweep_liveness(
        self, account: Account, sessions: list[Session], state: AppState
    ) -> list[Session]:
        """Refresh recency-derived status and mtime-cached card metadata."""

        def project(session: Session) -> None:
            # Subagent child sessions: _transcript_path rejects is_subagent
            # rollouts, so the sweep can't refresh them. Leave their state to the
            # full scan, which builds them from the spawned rollout directly.
            if session.parent_session_key is not None:
                return
            self._project_runtime_session(account, session)

        return state.apply_session_changes(sessions, project)

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        try:
            return await fetch_usage_once(account)
        except Exception as exc:  # noqa: BLE001 -- one failed usage read is non-fatal
            log.debug("fetch_usage failed for %s: %s", account.key, exc)
            return None

    def make_usage_poller(self, account: Account, state, bus, **kwargs):
        return CodexUsagePoller(
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
        command = message.split(maxsplit=1)
        if command and command[0] == "/compact":
            if len(command) != 1:
                return InjectResult(False, "usage: /compact")
            if images:
                return InjectResult(False, "/compact does not accept image attachments")
            if client is None or not client.owns(session.session_id):
                return InjectResult(
                    False,
                    "/compact is available only for AgentDeck-owned Codex chats",
                )
            result = await client.compact(session.session_id)
            if not result.accepted:
                return result
            return InjectResult(
                True,
                result.reason,
                result.session_id,
                transcript_expected=False,
            )
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
                            initial_prompt=message,
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
        if Capability.INTERACT not in session.capabilities:
            return None
        return self._actionable_interaction(account, session.session_id)

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
        if Capability.INTERACT not in session.capabilities:
            return InjectResult(False, "interaction is unavailable")
        interaction = self._actionable_interaction(account, session.session_id)
        if interaction is None or interaction.id != interaction_id:
            return InjectResult(False, "interaction is no longer pending")
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

    async def transcript_image(
        self, account: Account, session: Session, seq: int, image_index: int
    ) -> tuple[str, bytes] | None:
        path = self._transcript_path(account, session)
        return (
            await asyncio.to_thread(transcripts_mod.transcript_image, path, seq, image_index)
            if path is not None
            else None
        )

    def _read_transcript_file(self, path: Path, after_seq: int) -> list[TranscriptEvent]:
        """Read and filter a transcript outside the caller's event loop."""
        read = transcripts_mod.read_events(path)
        events = [event for event in read.events if event.seq > after_seq]
        return self._decorate_events(events, self._cached_meta(path).model)

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
        events = self._decorate_events(read.events, self._cached_meta(path).model)
        return (events, read.byte_offset, read.seq)

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        path = self._transcript_path(account, session)
        if path is None:
            return None
        event = self._cached_last_event(path)
        if event is not None and event.role == "assistant" and event.model is None:
            event.model = self._cached_meta(path).model
        return event

    async def recent_conversation(
        self, account: Account, session: Session, limit: int = 4
    ) -> list[TranscriptEvent]:
        path = self._transcript_path(account, session)
        if path is None:
            return []
        return await asyncio.to_thread(transcripts_mod.recent_conversation, path, limit=limit)

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
        detail = await asyncio.to_thread(
            self._load_transcript_file,
            path,
            before_seq,
        )
        self._decorate_events(detail.events, detail.model)
        return detail

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
