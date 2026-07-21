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

import httpx

from ...action_context import current_client_action_id
from ...models import (
    Account,
    Capability,
    InjectResult,
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
    Session,
    SessionStatus,
    TokenTotals,
    TranscriptDetail,
    TranscriptEvent,
    UsageSnapshot,
    activity_label,
)
from ..base import ModelChoice, SessionProvider
from . import history as history_mod
from . import kanban as kanban_mod
from . import registry as registry_mod
from . import transcripts as transcripts_mod
from .usage import UsagePoller, fetch_usage_once
from .worker_client import ClaudeWorkerClient

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


def _pending_from_dict(data: object) -> PendingInteraction | None:
    """Rebuild a PendingInteraction from the neutral dict the runtime publishes in
    the worker snapshot (the runtime does the Claude wire-schema mapping — see
    worker._normalize_interaction). A pure deserializer, symmetric to Codex's."""
    if not isinstance(data, dict) or not isinstance(data.get("id"), str):
        return None
    questions = tuple(
        InteractionQuestion(
            id=str(q.get("id")),
            header=q.get("header") or "",
            prompt=q.get("prompt") or "",
            options=tuple(
                InteractionOption(
                    label=o.get("label") or "", description=o.get("description") or ""
                )
                for o in (q.get("options") or [])
                if isinstance(o, dict)
            ),
            allow_other=bool(q.get("allow_other")),
            multiselect=bool(q.get("multiselect")),
        )
        for q in (data.get("questions") or [])
        if isinstance(q, dict)
    )
    return PendingInteraction(
        id=data["id"],
        kind=data.get("kind") or "question",
        thread_id=data.get("thread_id") or "",
        turn_id=None,
        title=data.get("title") or "",
        message=data.get("message") if isinstance(data.get("message"), str) else None,
        questions=questions,
        command=data.get("command") if isinstance(data.get("command"), str) else None,
        decisions=tuple(str(d) for d in (data.get("decisions") or [])),
    )


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
    )

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
        # Deck-owned worker clients (one per account). Each tracks runtime
        # availability so new-chat capability can change without a web restart.
        self._workers: dict[str, ClaudeWorkerClient] = {}
        self._states: dict[str, object] = {}
        self._watch_tasks: dict[str, asyncio.Task] = {}

    # --- deck-owned workers (optional; runtime-gated) ------------------

    async def start_account(self, account: Account, state) -> None:
        client = ClaudeWorkerClient(account, on_change=lambda: state.bus.publish("sessions"))
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
                await client.refresh()  # publishes "sessions" itself on change
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
        key = client.key_for(session.session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        # A retried dashboard send carries the same client-action id (the injector
        # wraps this call in client_action_context). Pass it as the delivery id so
        # a retry dedups at the deliver layer instead of writing the message twice.
        client_action_id = current_client_action_id()
        result = await client.deliver(
            key,
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
        key = client.key_for(session.session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await client.interrupt(key)

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
        last_text, last_role = meta[4], meta[5]
        return last_text if last_role == "agent" else None

    def owns_session(self, account: Account, session: Session) -> bool:
        client = self._workers.get(account.key)
        return bool(client and client.owns(session.session_id))

    def pending_interaction(
        self, account: Account, session: Session
    ) -> PendingInteraction | None:
        client = self._workers.get(account.key)
        if client is None:
            return None
        raw = client.pending_interaction(session.session_id)
        if raw is None:
            return None
        return _pending_from_dict(raw)

    async def answer_interaction(
        self,
        account: Account,
        session: Session,
        interaction_id: str,
        *,
        answers,
        decision: str | None,
    ) -> InjectResult:
        client = self._workers.get(account.key)
        if client is None:
            return InjectResult(False, "Claude workers are not enabled on the runtime service")
        key = client.key_for(session.session_id)
        if key is None:
            return InjectResult(False, "this session is not a deck-owned worker")
        return await client.answer(
            key, interaction_id, answers=dict(answers), decision=decision
        )

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

            # Deck-owned worker sessions can be driven: send a message any time
            # (deliver steers/queues/revives), and stop/steer while a turn runs.
            # Headless workers don't appear in the CLI registry, so the worker's
            # own turn state — not registry liveness — decides LIVE here.
            owned = workers is not None and workers.owns(sid)
            show_when_idle = False
            if owned:
                show_when_idle = True
                status = SessionStatus.LIVE if workers.live(sid) else SessionStatus.IDLE
                thinking = workers.turn_active(sid)
                # Only offer control actions while the runtime is actually
                # reachable. `_owned` is a last-known snapshot, so inject/steer/
                # interrupt granted from it while the runtime is unreachable would
                # target a worker we can no longer talk to; suppress them until the
                # snapshot is re-ingested on reconnect.
                if workers.available:
                    caps.add(Capability.INJECT)
                    if workers.turn_active(sid):
                        caps.update({Capability.STEER, Capability.INTERRUPT})
                        # Headless workers aren't in the CLI registry, so is_live
                        # was False and activity stayed None; recompute against the
                        # real turn state so the UI shows a live activity label.
                        activity = self._activity(True, tpath, last_activity) or activity

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
                    context_tokens=context_tokens,
                    deep_link=deep_link,
                    deep_link_label="open in claude.ai" if deep_link else None,
                    show_when_idle=show_when_idle,
                    capabilities=frozenset(caps),
                )
            )

        # Subagent pass: Task/Agent-tool subagents (isSidechain) nest under their
        # parent session as compact child rows instead of being invisible. Only
        # nesting under a currently-shown parent already bounds this to recent
        # work — no extra age gate needed.
        now = datetime.now(UTC)
        built_ids = {s.session_id for s in sessions}
        for sub_sid, (sub_path, parent_uuid) in _list_subagents(root).items():
            if parent_uuid not in built_ids or sub_sid in built_ids:
                continue  # only nest under a shown parent; never shadow a real session
            last_activity = _mtime(sub_path)
            age = (now - last_activity).total_seconds() if last_activity else 1e9
            ai_title, _sp, _sf, _tcwd, sub_text, sub_role = self._cached_meta(sub_path)
            # transcript_meta drops isSidechain user lines, so read the subagent's
            # own first prompt (the Task description) for a human-readable title.
            task = _subagent_task(sub_path)
            thinking = age < THINKING_WINDOW_S
            sessions.append(
                Session(
                    key=f"{account.key}:{sub_sid}",
                    account_key=account.key,
                    session_id=sub_sid,
                    status=SessionStatus.LIVE if thinking else SessionStatus.IDLE,
                    thinking=thinking,
                    activity=self._activity(thinking, sub_path, last_activity),
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
                    show_when_idle=True,
                    capabilities=frozenset({Capability.TRANSCRIPT}),
                )
            )
        return sessions

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Cheap recheck (~every 10s): flip LIVE↔IDLE and refresh the activity
        (busy) state from the open turn, so both update between full scans."""
        entries = {e.session_id: e for e in registry_mod.read_registry(account.root)}
        changed: list[Session] = []
        workers = self._workers.get(account.key)
        for s in sessions:
            # Subagents have no process-registry ground truth; the sweep would
            # force them IDLE and flicker their state. Leave them to the full
            # scan (which derives liveness from transcript mtime).
            if s.parent_session_key is not None:
                continue
            entry = entries.get(s.session_id)
            owned = workers is not None and workers.owns(s.session_id)
            live_now = workers.live(s.session_id) if owned else bool(
                entry and registry_mod.is_alive(entry)
            )
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
            thinking_now = workers.turn_active(s.session_id) if owned else activity_now is not None
            pid_now = entry.pid if (entry and live_now) else None
            deep_link_now = (
                registry_mod.session_deep_link(entry.pid) if (entry and live_now) else None
            )
            capabilities_now = set(s.capabilities)
            if owned and workers.available:
                capabilities_now.add(Capability.INJECT)
                if thinking_now:
                    capabilities_now.update({Capability.STEER, Capability.INTERRUPT})
                else:
                    capabilities_now.difference_update(
                        {Capability.STEER, Capability.INTERRUPT}
                    )
            elif owned:
                # Runtime unreachable: strip control actions granted from the stale
                # snapshot until it reconnects (mirrors scan_sessions).
                capabilities_now.difference_update(
                    {Capability.INJECT, Capability.STEER, Capability.INTERRUPT}
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
                or frozenset(capabilities_now) != s.capabilities
                or (owned and not s.show_when_idle)
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
                s.show_when_idle = owned or s.show_when_idle
                s.capabilities = frozenset(capabilities_now)
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
