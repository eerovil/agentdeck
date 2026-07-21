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
    SubagentProgress,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
    detailed_activity_label,
)
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
from .usage import UsagePoller, fetch_usage_once

DETAIL_WINDOW = 400
MAX_SESSIONS = 200
LIVE_WINDOW_S = 30.0
SUBAGENT_QUIET_S = 300.0
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
        self._meta_cache: dict[str, tuple[float, transcripts_mod.TranscriptMeta]] = {}
        self._last_ev_cache: dict[str, tuple[float, TranscriptEvent | None]] = {}
        self._delegation_cache: dict[str, tuple[int, int, frozenset[str]]] = {}
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
                log.debug("Codex runtime refresh failed for %s: %s", account.key, exc)

    async def stop_account(self, account: Account) -> None:
        task = self._runtime_tasks.pop(account.key, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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

    def _cached_delegated_session_keys(self, path: Path) -> frozenset[str]:
        try:
            stat = path.stat()
        except OSError:
            return frozenset()
        signature = (stat.st_size, stat.st_mtime_ns)
        hit = self._delegation_cache.get(str(path))
        if hit is not None and hit[:2] == signature:
            return hit[2]
        keys = transcripts_mod.delegated_session_keys(path)
        self._delegation_cache[str(path)] = (*signature, keys)
        return keys

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
                        child_active = meta.task_active and age < SUBAGENT_QUIET_S
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
                    initial_prompt=meta.first_prompt,
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
                    tokens=meta.tokens,
                    context_tokens=meta.context_tokens,
                    show_when_idle=True,
                    capabilities=self._capabilities(account, session_id, path, meta, status),
                )
            )
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
            session.subagent_count = sum(
                agent.status in ("working", "quiet") for agent in agents
            )
        self._paths = {key: value for key, value in self._paths.items() if key[0] != account.key}
        self._paths.update(current_paths)
        # Publish this account's delegation links so cross-provider children
        # (e.g. a Codex chat that delegated a Claude session) nest correctly.
        state = self._states.get(account.key)
        if state is not None:
            state.set_delegation_parents(account.key, delegation_parent)
        return sessions

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Refresh recency-derived status and mtime-cached card metadata."""
        changed = []
        for session in sessions:
            # Subagent child sessions: _transcript_path rejects is_subagent
            # rollouts, so the sweep can't refresh them. Leave their state to the
            # full scan, which builds them from the spawned rollout directly.
            if session.parent_session_key is not None:
                continue
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
            return await client.compact(session.session_id)
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
