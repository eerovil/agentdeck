"""ClaudeCodeProvider — translates a $CLAUDE_CONFIG_DIR into agentdeck sessions.

Discovers sessions (live from the pid registry, idle from transcript files),
reads their transcripts, surfaces per-account usage limits, and derives the
claude.ai/code deep link for cloud/RC-spawned sessions. Existing CLI chats are
read-only here (reply on claude.ai via the deep link); sessions the deck itself
spawned as worker processes can be started and driven from the UI via the
runtime service (new chat, send, steer, interrupt).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from ...action_context import current_client_action_id
from ...models import (
    CONTROL_CAPABILITIES,
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
    event_progress_at,
    runtime_control_capabilities,
    runtime_turn_state,
    turn_stalled,
)
from .._filecache import FileCache, mtime_sig, size_mtime_sig
from ..base import ModelChoice, SessionProvider
from . import history as history_mod
from . import kanban as kanban_mod
from . import registry as registry_mod
from . import transcripts as transcripts_mod
from .usage import ClaudeUsagePoller, fetch_usage_once
from .worker_client import ClaudeWorkerClient

if TYPE_CHECKING:
    from ...state import AppState

# Max events rendered on a detail page; older ones fetched via "load earlier".
DETAIL_WINDOW = 400

log = logging.getLogger(__name__)

# Upper bound on idle sessions surfaced per account in v0.1 — the projects/ dir
# can hold thousands of transcripts; show the most recently active.
MAX_IDLE_SESSIONS = 200

# Brief write recency resolves only ambiguous transcript events. Structural or
# provider lifecycle keeps an Active Turn working beyond this window.
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


def _pending_from_dict(data: object) -> PendingInteraction | None:
    """Rebuild a PendingInteraction from the neutral dict the runtime publishes in
    the worker snapshot (the runtime does the Claude wire-schema mapping — see
    worker._normalize_interaction). The model owns the schema, so this can no
    longer silently drop cwd/url/secret/option-value the way it used to."""
    return PendingInteraction.from_dict(data)


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


def _list_subagents(config_dir: Path) -> dict[str, tuple[Path, str]]:
    """Map subagent id -> (newest transcript path, parent session uuid).

    Claude Task/Agent-tool subagents live at
    ``projects/<slug>/<parent-uuid>/subagents/agent-*.jsonl`` (``isSidechain``),
    which the top-level ``<slug>/<uuid>.jsonl`` glob never reaches. The parent
    uuid is the directory that contains the ``subagents/`` folder.
    """
    out: dict[str, tuple[Path, str]] = {}
    projects = config_dir / "projects"
    if not projects.is_dir():
        return out
    for path in projects.glob("*/*/subagents/*.jsonl"):
        sid = path.stem
        prev = out.get(sid)
        if prev is None or path.stat().st_mtime > prev[0].stat().st_mtime:
            out[sid] = (path, path.parent.parent.name)
    return out


def _subagent_task(path: Path) -> str | None:
    """The subagent's first user prompt (its Task description).

    ``transcript_meta`` intentionally drops ``isSidechain`` user lines, so a
    subagent transcript yields no ``first_user``; read the first user text here
    directly so the nested row shows the task instead of the ``agent-<hash>`` id.
    """
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if obj.get("type") != "user":
                    continue
                content = obj.get("message", {}).get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = (block.get("text") or "").strip()
                            if text:
                                return text
    except OSError:
        return None
    return None


class ClaudeCodeProvider(SessionProvider):
    provider_id = "claude_code"
    # New chats spawn deck-owned worker processes via the runtime service. When
    # that service has no Claude workers (feature off), start_session returns a
    # clean error and the provider stays a read-only transcript viewer.
    supports_new_session = True
    # Aliases (not pinned ids) so the list survives model version bumps; the CLI
    # resolves them. Applied via --model at worker spawn (new chat only).
    selectable_models = (
        ModelChoice("opus", "Opus 4.8"),
        ModelChoice("sonnet", "Sonnet"),
        ModelChoice("haiku", "Haiku"),
        ModelChoice("fable", "Fable 5"),
    )

    def __init__(self) -> None:
        # Cheap transcript reads memoized by file signature so idle transcripts
        # aren't re-parsed every scan. Meta/context invalidate on mtime; last-event
        # (read by the liveness sweep every few seconds per live session) needs the
        # finer (size, mtime_ns) signature to catch a same-second rewrite.
        self._meta_cache: FileCache[transcripts_mod.TranscriptMeta] = FileCache(mtime_sig)
        self._last_ev_cache: FileCache[TranscriptEvent | None] = FileCache(size_mtime_sig)
        self._ctx_cache: FileCache[int | None] = FileCache(mtime_sig)
        # Resolves kanban dispatch prompts to real GitHub issue titles.
        self._kanban = kanban_mod.KanbanTitleCache()
        # Deck-owned worker clients (one per account). Each tracks runtime
        # availability so new-chat capability can change without a web restart.
        self._workers: dict[str, ClaudeWorkerClient] = {}
        self._states: dict[str, object] = {}
        self._watch_tasks: dict[str, asyncio.Task] = {}

    # --- deck-owned workers (optional; runtime-gated) ------------------

    async def start_account(self, account: Account, state) -> None:
        client = ClaudeWorkerClient(
            account,
            on_change=lambda: self._runtime_changed(account, state),
        )
        self._workers[account.key] = client
        self._states[account.key] = state
        await client.probe()
        self._watch_tasks[account.key] = asyncio.create_task(
            self._watch(account, client), name=f"claude-workers:{account.key}"
        )

    async def _watch(self, account: Account, client: ClaudeWorkerClient) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await client.refresh()  # callback reprojects cached Sessions on change
            except Exception as exc:  # noqa: BLE001 -- reconnect on the next poll
                log.debug("claude worker refresh failed for %s: %s", account.key, exc)

    async def stop_account(self, account: Account) -> None:
        task = self._watch_tasks.pop(account.key, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._states.pop(account.key, None)
        client = self._workers.pop(account.key, None)
        if client is not None:
            await client.stop()

    def can_start_session(self, account: Account) -> bool:
        client = self._workers.get(account.key)
        return bool(client and client.available)

    def _actionable_interaction(
        self, account: Account, session_id: str
    ) -> PendingInteraction | None:
        workers = self._workers.get(account.key)
        if workers is None or not workers.available or not workers.owns(session_id):
            return None
        return _pending_from_dict(workers.pending_interaction(session_id))

    def _runtime_changed(self, account: Account, state) -> None:
        sessions = state.sessions_for_account(account.key)
        # sweep_liveness applies the refresh and publishes once through AppState
        # if any owned worker's projection changed.
        self.sweep_liveness(account, sessions, state)

    def _project_owned_worker(
        self,
        account: Account,
        session: Session,
        *,
        transcript_path: Path | None,
        last_activity: datetime | None,
    ) -> bool:
        """Overlay one owned worker's runtime state onto discovered metadata."""
        workers = self._workers.get(account.key)
        if workers is None or not workers.owns(session.session_id):
            return False
        active = workers.turn_active(session.session_id)
        interaction = self._actionable_interaction(account, session.session_id)
        activity, stalled, progress = self._turn_state(
            True,
            transcript_path,
            last_activity,
            lifecycle_active=active,
        )
        session.status = (
            SessionStatus.LIVE if workers.live(session.session_id) else SessionStatus.IDLE
        )
        session.lifecycle_active = active
        session.stalled, session.thinking = runtime_turn_state(
            active_turn=active,
            actionable_interaction=interaction is not None,
            stalled_evidence=stalled,
        )
        session.activity = activity if session.thinking else None
        session.last_progress = progress
        session.show_when_idle = True
        # Keep the read-only capabilities derived from the transcript; strip the
        # whole control set and reapply the current live projection so a stale
        # control capability cannot linger across a runtime becoming unavailable.
        session.capabilities = (session.capabilities - CONTROL_CAPABILITIES) | (
            runtime_control_capabilities(
                available=workers.available,
                active_turn=active,
                actionable_interaction=(
                    interaction is not None
                ),
            )
        )
        return True

    @staticmethod
    def _delegation_permission_mode(sandbox: str | None) -> str | None:
        # Claude has no Codex-style filesystem sandbox. Plan mode is its native
        # read-only equivalent; everything else — interactive dashboard chats
        # (sandbox is None) AND explicit workspace-write delegations — returns
        # None to inherit the account's configured worker permission_mode (e.g.
        # bypassPermissions for an autonomous account). Forcing "default" here
        # would override that config and leave a headless worker stalling on the
        # first approval-gated tool, since no permission-prompt tool is wired.
        if sandbox == "read-only":
            return "plan"
        return None

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
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        # A retried new-chat request carries the same client-action id. Derive a
        # stable worker key + delivery id from it so the retry maps to exactly one
        # worker (deduped at the deliver layer) instead of spawning a duplicate.
        # Interactive callers without an action id keep a fresh random key.
        client_action_id = current_client_action_id()
        if client_action_id:
            key = f"chat-{hashlib.sha256(client_action_id.encode()).hexdigest()[:12]}"
        else:
            key = f"chat-{uuid.uuid4().hex[:8]}"
        result = await client.deliver(
            key,
            message,
            cwd=str(cwd),
            fresh=True,
            images=[str(path) for path in images or []],
            model=model,
            permission_mode=self._delegation_permission_mode(sandbox),
            delivery_id=client_action_id,
        )
        if not result.accepted and result.reason == "delivery_id_conflict":
            # Same action id, different request payload — a genuine conflict, not
            # a benign retry; surface it distinctly rather than as a raw reject.
            return InjectResult(False, "client_action_conflict")
        return result

    async def _deliver_to_session(
        self,
        account: Account,
        session: Session,
        message: str,
        images: list[Path] | None = None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        # A retried dashboard send carries the same client-action id (the injector
        # wraps this call in client_action_context). Pass it as the delivery id so
        # a retry dedups at the deliver layer instead of writing the message twice.
        client_action_id = current_client_action_id()
        result = await client.send(
            session.session_id,
            message,
            images=[str(path) for path in images or []],
            delivery_id=client_action_id,
        )
        if not result.accepted and result.reason == "delivery_id_conflict":
            return InjectResult(False, "client_action_conflict")
        return result

    async def inject(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
    ) -> InjectResult:
        return await self._deliver_to_session(account, session, message, images)

    async def steer(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        images: list[Path] | None = None,
    ) -> InjectResult:
        return await self._deliver_to_session(account, session, message, images)

    async def interrupt(self, account: Account, session: Session) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.interrupt(session.session_id)

    async def wait_for_session(
        self,
        account: Account,
        session_id: str,
        *,
        timeout_s: float,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None or not client.owns(session_id):
            return InjectResult(False, "this session is not a deck-owned worker")
        return await client.wait_for_turn(session_id, timeout_s=timeout_s)

    async def session_result(self, account: Account, session_id: str) -> str | None:
        path = _list_transcripts(account.root).get(session_id)
        if path is None:
            return None
        meta = await asyncio.to_thread(transcripts_mod.transcript_meta, path)
        return meta.last_text if meta.last_role == "agent" else None

    async def _answer_actionable(
        self,
        account: Account,
        session: Session,
        interaction: PendingInteraction,
        *,
        answers,
        decision: str | None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        return await client.answer(
            session.session_id, interaction.id, answers=answers, decision=decision
        )

    def _cached_meta(self, path: Path) -> transcripts_mod.TranscriptMeta:
        return self._meta_cache.get(
            path, transcripts_mod.transcript_meta, transcripts_mod.TranscriptMeta()
        )

    def _cached_last_event(self, path: Path) -> TranscriptEvent | None:
        return self._last_ev_cache.get(path, transcripts_mod.last_event, None)

    def _cached_context_tokens(self, path: Path) -> int | None:
        return self._ctx_cache.get(path, transcripts_mod.last_context_tokens, None)

    def _turn_state(
        self,
        is_live: bool,
        tpath: Path | None,
        last_write: datetime | None,
        *,
        lifecycle_active: bool | None = None,
    ) -> tuple[str | None, bool, datetime | None]:
        """Normalized own-turn presentation from lifecycle + transcript evidence."""
        if last_write is None:
            return (None, False, last_write)
        last_ev = self._cached_last_event(tpath) if tpath is not None else None
        progress = event_progress_at(last_ev, last_write)
        age = (datetime.now(UTC) - progress).total_seconds() if progress else 1e9
        stalled = turn_stalled(
            live=is_live,
            lifecycle_active=lifecycle_active,
            last_ev=last_ev,
            age_s=age,
        )
        label = activity_label(
            is_live,
            age < THINKING_WINDOW_S,
            last_ev,
            age,
            lifecycle_active=lifecycle_active,
        )
        return (label, stalled, progress)

    def _activity(self, is_live: bool, tpath: Path | None, last_write) -> str | None:
        """Activity label for a live session, from its open turn (cheap, mtime
        -cached tail read). Same logic as the detail page, so list and detail
        agree."""
        return self._turn_state(is_live, tpath, last_write)[0]

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
        # Deck-owned workers are headless (absent from the CLI registry) and a
        # freshly spawned one may not have flushed its transcript yet — keep its
        # session so it appears immediately instead of vanishing until the first
        # transcript write lands.
        workers = self._workers.get(account.key)
        if workers is not None:
            keep |= set(workers.owned_session_ids())

        # Kanban worker sessions carry a fixed dispatch prompt instead of a real
        # title; resolve the referenced GitHub issue title (cached) up front so
        # the build loop below can substitute it. Best-effort — a miss just
        # leaves the existing title fallback in place.
        krefs = []
        for sid in keep:
            tpath = transcripts.get(sid)
            if tpath is None:
                continue
            first_user = self._cached_meta(tpath).first_prompt
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
                meta = self._cached_meta(tpath)
                ai_title = meta.title
                last_prompt = meta.last_prompt
                first_user = meta.first_prompt
                tcwd = meta.cwd
                last_text = meta.last_text
                last_role = meta.last_role

            cwd: Path | None = None
            if entry and entry.cwd:
                cwd = entry.cwd
            elif h and h.project:
                cwd = Path(h.project)
            elif tcwd:
                # The registry entry is gone once a session is idle; the
                # transcript's cwd is what keeps idle sessions injectable.
                cwd = Path(tcwd)
            elif workers is not None and workers.cwd_for(sid):
                # Freshly spawned owned worker with no transcript yet — take the
                # cwd from the runtime snapshot so the card isn't cwd-less.
                cwd = Path(workers.cwd_for(sid))
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
            activity, stalled, last_progress = self._turn_state(
                is_live, tpath, last_activity
            )
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

            session = Session(
                key=f"{account.key}:{sid}",
                account_key=account.key,
                session_id=sid,
                status=status,
                thinking=thinking,
                stalled=stalled,
                activity=activity,
                cwd=cwd,
                title=title,
                initial_prompt=first_user,
                last_prompt=last_prompt,
                last_text=last_text,
                last_role=last_role,
                question=question,
                kind=entry.kind if entry else None,
                worker_type=wtype,
                is_delegated=wtype != "you",
                issue_url=issue_url,
                issue_status=issue_status,
                issue_status_kind=issue_status_kind,
                pid=entry.pid if (entry and is_live) else None,
                proc_start=entry.proc_start if entry else None,
                started_at=entry.started_at if entry else None,
                last_activity=last_activity,
                last_progress=last_progress,
                context_tokens=context_tokens,
                deep_link=deep_link,
                deep_link_label="open in claude.ai" if deep_link else None,
                show_when_idle=False,
                capabilities=frozenset(caps),
            )
            self._project_owned_worker(
                account,
                session,
                transcript_path=tpath,
                last_activity=last_activity,
            )
            sessions.append(session)

        # Subagent pass: Task/Agent-tool subagents (isSidechain) nest under their
        # parent session as compact child rows instead of being invisible. Only
        # nesting under a currently-shown parent already bounds this to recent
        # work — no extra age gate needed.
        built_ids = {s.session_id for s in sessions}
        for sub_sid, (sub_path, parent_uuid) in _list_subagents(root).items():
            if parent_uuid not in built_ids or sub_sid in built_ids:
                continue  # only nest under a shown parent; never shadow a real session
            last_activity = _mtime(sub_path)
            sub_meta = self._cached_meta(sub_path)
            ai_title, sub_text, sub_role = sub_meta.title, sub_meta.last_text, sub_meta.last_role
            # transcript_meta drops isSidechain user lines, so read the subagent's
            # own first prompt (the Task description) for a human-readable title.
            task = _subagent_task(sub_path)
            activity, stalled, last_progress = self._turn_state(
                True, sub_path, last_activity
            )
            thinking = activity is not None
            sessions.append(
                Session(
                    key=f"{account.key}:{sub_sid}",
                    account_key=account.key,
                    session_id=sub_sid,
                    status=SessionStatus.LIVE if thinking else SessionStatus.IDLE,
                    thinking=thinking,
                    stalled=stalled,
                    activity=activity,
                    title=ai_title or task or sub_sid,
                    initial_prompt=task,
                    last_text=sub_text,
                    last_role=sub_role,
                    kind="subagent",
                    worker_type="you",
                    # Only an actively-working subagent is attention-worthy; a
                    # quiet/finished one stays nested but out of Deckhand triage
                    # (and the working count) so it can't flood the deck.
                    is_delegated=not thinking,
                    parent_session_key=f"{account.key}:{parent_uuid}",
                    last_activity=last_activity,
                    last_progress=last_progress,
                    show_when_idle=True,
                    capabilities=frozenset({Capability.TRANSCRIPT}),
                )
            )
        return sessions

    def sweep_liveness(
        self, account: Account, sessions: list[Session], state: AppState
    ) -> list[Session]:
        """Cheap recheck (~every 10s): flip LIVE↔IDLE and refresh the activity
        (busy) state from the open turn, so both update between full scans."""
        entries = {e.session_id: e for e in registry_mod.read_registry(account.root)}
        workers = self._workers.get(account.key)

        def project(s: Session) -> None:
            # Subagents have no process-registry ground truth; the sweep would
            # force them IDLE and flicker their state. Leave them to the full
            # scan (which derives liveness from transcript mtime).
            if s.parent_session_key is not None:
                return
            entry = entries.get(s.session_id)
            owned = workers is not None and workers.owns(s.session_id)
            live_now = workers.live(s.session_id) if owned else bool(
                entry and registry_mod.is_alive(entry)
            )
            status_now = SessionStatus.LIVE if live_now else SessionStatus.IDLE
            activity_now = None
            stalled_now = False
            last_progress_now = s.last_progress
            last_prompt_now = s.last_prompt
            last_text_now = s.last_text
            last_role_now = s.last_role
            context_now = s.context_tokens
            last_ev_now = None
            tp = None
            if live_now:
                tp = self._transcript_path(account, s)
                activity_now, stalled_now, last_progress_now = self._turn_state(
                    True, tp, _mtime(tp) if tp else None
                )
                if tp is not None:
                    # keep the list's messages as fresh as the detail tail — this
                    # is the mtime-cached meta, so idle transcripts cost nothing.
                    tmeta = self._cached_meta(tp)
                    last_prompt_now = tmeta.last_prompt or s.last_prompt
                    last_text_now = tmeta.last_text or s.last_text
                    last_role_now = tmeta.last_role or s.last_role
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
            s.status = status_now
            s.thinking = thinking_now
            s.stalled = stalled_now
            s.lifecycle_active = None
            s.activity = activity_now
            s.last_activity = _mtime(tp) if tp is not None else s.last_activity
            s.last_progress = last_progress_now
            s.last_prompt = last_prompt_now
            s.last_text = last_text_now
            s.last_role = last_role_now
            s.question = question_now
            s.context_tokens = context_now
            s.pid = pid_now
            s.deep_link = deep_link_now
            s.show_when_idle = False
            capabilities = set(s.capabilities)
            capabilities.difference_update(
                {
                    Capability.INJECT,
                    Capability.STEER,
                    Capability.INTERRUPT,
                    Capability.INTERACT,
                }
            )
            s.capabilities = frozenset(capabilities)
            s.kind = entry.kind if entry else s.kind
            self._project_owned_worker(
                account,
                s,
                transcript_path=tp,
                last_activity=_mtime(tp) if tp is not None else s.last_activity,
            )

        return state.apply_session_changes(sessions, project)

    # --- usage ---------------------------------------------------------

    async def fetch_usage(self, account: Account) -> UsageSnapshot | None:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                return await fetch_usage_once(account, client)
        except Exception as exc:  # noqa: BLE001 — one-shot helper, never fatal
            log.debug("fetch_usage failed for %s: %s", account.key, exc)
            return None

    def make_usage_poller(self, account: Account, state, bus, **kwargs):
        return ClaudeUsagePoller(
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
            [
                *projects.glob(f"*/{session.session_id}.jsonl"),
                # subagent (isSidechain) transcripts live one level deeper
                *projects.glob(f"*/*/subagents/{session.session_id}.jsonl"),
            ],
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
        return [event for event in read.events if event.seq > after_seq]

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        path = self._transcript_path(account, session)
        return (
            await asyncio.to_thread(transcripts_mod.last_event, path)
            if path is not None
            else None
        )

    async def recent_conversation(
        self, account: Account, session: Session, limit: int = 4
    ) -> list[TranscriptEvent]:
        path = self._transcript_path(account, session)
        if path is None:
            return []
        return await asyncio.to_thread(transcripts_mod.recent_conversation, path, limit=limit)

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
