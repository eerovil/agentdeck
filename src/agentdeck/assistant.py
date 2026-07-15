"""Codex-powered orchestration advice for the dashboard."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, AssistantConfig
from .git_context import GitContext, GitContextResolver
from .models import Account, PendingInteraction, Session
from .providers import PROVIDERS
from .state import AppState

log = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("assistant_output.schema.json")
_MAX_CONTEXT_CHARS = 1_000
_INSIGHT_PR_SEQUENCE_RE = re.compile(
    r"\b(?:PR|PRs|pull request|pull requests)\s+"
    r"((?:#?\d+)(?:\s*(?:,|and|&)\s*#?\d+)*)",
    re.IGNORECASE,
)
_INSIGHT_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_UNSAFE_AUTO_ANSWER_RE = re.compile(
    r"\b(?:delete|destroy|drop|erase|overwrite|force[- ]?push|purchase|pay)\b",
    re.IGNORECASE,
)
_PR_TITLE_WORD_RE = re.compile(r"[^\W_][\w.+-]*", re.UNICODE)
_PR_HEADLINE_PREFIX_RE = re.compile(
    r"^(?:(?:open|draft)\s+)?(?:PR|pull request)\s*(?:#\d+)?\s*",
    re.IGNORECASE,
)
_GENERIC_PR_TITLE_WORDS = {
    "add",
    "allow",
    "change",
    "create",
    "ensure",
    "fix",
    "harden",
    "implement",
    "improve",
    "make",
    "pr",
    "refactor",
    "remove",
    "support",
    "update",
}
_PROJECT_DISPLAY_NAMES = {"agentdeck": "AgentDeck", "sos": "SOS"}


@dataclass(frozen=True)
class AssistantAnswer:
    question_id: str
    values: tuple[str, ...]


@dataclass(frozen=True)
class AssistantInsight:
    session_key: str
    kind: str
    headline: str
    detail: str
    interaction_id: str | None = None
    coordination_key: str | None = None
    answers: tuple[AssistantAnswer, ...] = ()
    safe_to_auto_answer: bool = False
    confidence: float = 0.0


@dataclass(frozen=True)
class AssistantAction:
    session_key: str
    text: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class AssistantHandledItem:
    session_key: str
    headline: str


@dataclass(frozen=True)
class AssistantView:
    state: str = "idle"
    summary: str = "Waiting for session activity."
    insights: tuple[AssistantInsight, ...] = ()
    actions: tuple[AssistantAction, ...] = ()
    analyzed_at: datetime | None = None
    error: str | None = None


Runner = Callable[[Account, AssistantConfig, str], Awaitable[dict[str, Any]]]


def _trim(value: str | None) -> str | None:
    if not value:
        return None
    return value[-_MAX_CONTEXT_CHARS:]


async def _terminate_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await process.wait()


async def run_codex(account: Account, config: AssistantConfig, prompt: str) -> dict[str, Any]:
    """Run one read-only, ephemeral Codex analysis and return its JSON result."""
    env = os.environ.copy()
    env["CODEX_HOME"] = str(account.root)
    with tempfile.TemporaryDirectory(prefix="agentdeck-assistant-") as tmp:
        output = Path(tmp) / "result.json"
        args = [
            "codex",
            "--ask-for-approval",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--output-schema",
            str(_SCHEMA_PATH),
            "--output-last-message",
            str(output),
            "--skip-git-repo-check",
        ]
        if config.model:
            args.extend(("--model", config.model))
        args.append("-")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(Path.cwd()),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError(f"could not start Codex: {exc}") from exc
        try:
            await asyncio.wait_for(
                process.communicate((prompt + "\n").encode()), timeout=config.timeout_s
            )
        except TimeoutError as exc:
            await _terminate_group(process)
            raise RuntimeError("Codex assistant timed out") from exc
        except asyncio.CancelledError:
            await _terminate_group(process)
            raise
        if process.returncode != 0:
            # Codex stderr can echo the complete prompt, including transcript excerpts.
            # Keep operator-visible and journal errors useful without persisting chat data.
            raise RuntimeError(
                f"Codex assistant exited without an answer (status {process.returncode})"
            )
        try:
            value = json.loads(output.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("Codex assistant returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Codex assistant returned an invalid result")
        return value


class AssistantService:
    """Debounce session changes into low-cost orchestration analyses."""

    def __init__(
        self,
        config: AppConfig,
        state: AppState,
        *,
        runner: Runner = run_codex,
        context_resolver: GitContextResolver | None = None,
    ) -> None:
        self.config = config.assistant
        self.state = state
        self.accounts = config.build_accounts()
        self.runner = runner
        self.context_resolver = context_resolver or GitContextResolver()
        self.contexts: dict[str, GitContext] = {}
        self.view = AssistantView(
            summary=("Starting orchestration assistant…" if self.config.enabled else "Disabled")
        )
        self._task: asyncio.Task | None = None
        self._session_watch_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._known_visible_session_keys: set[str] = set()
        self._last_signature: str | None = None
        self._last_run = 0.0
        self.refresh_status: str | None = None
        self._manual_refresh_pending = False
        self._answered_interactions: set[str] = set()
        self._evidence_signatures: dict[str, str] = {}
        self.analysis_session_count = 0
        self.total_session_count = 0
        checkpoint = state.db.load_assistant_checkpoint() if state.db else None
        restored = self._restore_checkpoint(checkpoint)
        if restored is not None:
            self.view, self._evidence_signatures, self._last_signature = restored
        self._force = self._last_signature is None
        handled = state.db.load_assistant_handled() if state.db else {}
        self._handled = {session_key: record[0] for session_key, record in handled.items()}
        self._handled_insights = {
            session_key: AssistantInsight(
                session_key=session_key,
                kind=kind,
                headline=headline,
                detail=detail or "",
            )
            for session_key, (_, kind, headline, detail) in handled.items()
            if kind is not None and headline is not None
        }

    @staticmethod
    def _restore_checkpoint(
        payload: dict[str, Any] | None,
    ) -> tuple[AssistantView, dict[str, str], str | None] | None:
        if not payload or payload.get("version") != 1:
            return None
        try:
            raw_view = payload["view"]
            insights = []
            for item in raw_view.get("insights", []):
                answers = tuple(
                    AssistantAnswer(str(answer["question_id"]), tuple(answer["values"]))
                    for answer in item.get("answers", [])
                )
                insights.append(
                    AssistantInsight(
                        session_key=str(item["session_key"]),
                        kind=str(item["kind"]),
                        headline=str(item["headline"]),
                        detail=str(item["detail"]),
                        interaction_id=item.get("interaction_id"),
                        coordination_key=item.get("coordination_key"),
                        answers=answers,
                        safe_to_auto_answer=bool(item.get("safe_to_auto_answer", False)),
                        confidence=float(item.get("confidence", 0.0)),
                    )
                )
            actions = tuple(
                AssistantAction(
                    session_key=str(item["session_key"]),
                    text=str(item["text"]),
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
                for item in raw_view.get("actions", [])
            )
            analyzed_at = raw_view.get("analyzed_at")
            view = AssistantView(
                state="ready",
                summary=str(raw_view["summary"]),
                insights=tuple(insights),
                actions=actions,
                analyzed_at=(datetime.fromisoformat(analyzed_at) if analyzed_at else None),
            )
            evidence = {
                str(key): str(value)
                for key, value in payload.get("evidence_signatures", {}).items()
            }
            signature = payload.get("analysis_signature")
            return view, evidence, str(signature) if signature is not None else None
        except (KeyError, TypeError, ValueError):
            log.warning("Ignoring invalid Deckhand checkpoint")
            return None

    def _checkpoint_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "view": {
                "state": "ready",
                "summary": self.view.summary,
                "insights": [
                    {
                        "session_key": insight.session_key,
                        "kind": insight.kind,
                        "headline": insight.headline,
                        "detail": insight.detail,
                        "interaction_id": insight.interaction_id,
                        "coordination_key": insight.coordination_key,
                        "answers": [
                            {
                                "question_id": answer.question_id,
                                "values": list(answer.values),
                            }
                            for answer in insight.answers
                        ],
                        "safe_to_auto_answer": insight.safe_to_auto_answer,
                        "confidence": insight.confidence,
                    }
                    for insight in self.view.insights
                ],
                "actions": [
                    {
                        "session_key": action.session_key,
                        "text": action.text,
                        "created_at": action.created_at.isoformat(),
                    }
                    for action in self.view.actions
                ],
                "analyzed_at": (
                    self.view.analyzed_at.isoformat() if self.view.analyzed_at else None
                ),
            },
            "evidence_signatures": self._evidence_signatures,
            "analysis_signature": self._last_signature,
        }

    def _save_checkpoint(self) -> None:
        if self.state.db and self.view.state == "ready":
            self.state.db.record_assistant_checkpoint(self._checkpoint_payload())

    def _account(self) -> Account | None:
        codex = [account for account in self.accounts if account.provider_id == "codex"]
        if self.config.account_key:
            return next(
                (account for account in codex if account.key == self.config.account_key), None
            )
        return codex[0] if codex else None

    def request_refresh(self, *, manual: bool = False) -> bool:
        if not self.config.enabled:
            return False
        if manual:
            self._manual_refresh_pending = True
            self.refresh_status = "Checking current evidence…"
        self._force = True
        self._wake.set()
        return True

    async def ensure_session_context(
        self, session: Session, *, transcript_context: str | None = None
    ) -> GitContext | None:
        """Resolve git/PR metadata when a chat outside the analysis window opens."""
        existing = self.contexts.get(session.key)
        if existing is not None and not transcript_context:
            return existing
        target = session
        if transcript_context:
            target = replace(
                session,
                last_text="\n".join(
                    value for value in (session.last_text, transcript_context) if value
                ),
            )
        try:
            context = (await self.context_resolver.resolve([target])).get(session.key)
        except Exception as exc:  # noqa: BLE001 -- metadata must not break chat pages
            log.debug("Deckhand context resolve failed for %s: %s", session.key, exc)
            return None
        if context is not None and context != existing:
            self.contexts[session.key] = context
            self._discard_session_insights(session.key)
            self.request_refresh()
        return context

    def _discard_session_insights(self, session_key: str) -> None:
        """Do not retain advice produced from superseded PR attribution."""
        insights = tuple(
            insight for insight in self.view.insights if insight.session_key != session_key
        )
        if insights == self.view.insights:
            return
        self.view = AssistantView(
            state=self.view.state,
            summary=(self.view.summary if insights else "Nothing needs your attention right now."),
            insights=insights,
            actions=self.view.actions,
            analyzed_at=self.view.analyzed_at,
            error=self.view.error,
        )
        self._save_checkpoint()
        self.state.bus.publish("assistant")

    async def start(self) -> None:
        if self.config.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="orchestration-assistant")
            self._session_watch_task = asyncio.create_task(
                self._watch_sessions(), name="orchestration-assistant-sessions"
            )

    async def stop(self) -> None:
        tasks = tuple(
            task for task in (self._task, self._session_watch_task) if task is not None
        )
        self._task = None
        self._session_watch_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_sessions(self) -> None:
        """Wake the cheap eligibility check as soon as collection changes.

        Most session events are transient activity churn and stop before Git or
        Luna work. Newly visible keys bypass the normal refresh interval once.
        """
        with self.state.bus.subscribe("sessions") as subscription:
            while True:
                await subscription.get()
                self._wake.set()

    def _interaction(self, session: Session) -> PendingInteraction | None:
        account = next((item for item in self.accounts if item.key == session.account_key), None)
        if account is None:
            return None
        return PROVIDERS[account.provider_id].pending_interaction(account, session)

    def _snapshot_row(self, session: Session) -> dict[str, Any]:
        context = self.contexts.get(session.key)
        interaction = self._interaction(session)
        return {
            "session_key": session.key,
            "title": session.title,
            "cwd": str(session.cwd) if session.cwd else None,
            "state": session.display_state,
            "activity": session.activity,
            "question": session.question,
            "last_prompt": _trim(session.last_prompt),
            "last_response": _trim(session.last_text),
            "subagents": session.subagent_count,
            "git": context.as_json() if context is not None else None,
            "interaction": self._interaction_json(interaction),
        }

    def _analysis_sessions(self) -> list[Session]:
        """Prioritize blocking interactions, then the most recently active chats.

        Dashboard ordering deliberately ties active sessions to prevent card
        churn. Deckhand must not reuse that insertion-stable order: a new active
        chat would otherwise sit behind the existing analysis window forever.
        """
        visible = self.state.visible_sessions()
        blocking = [session for session in visible if self._interaction(session) is not None]
        blocking_keys = {session.key for session in blocking}
        remaining = [session for session in visible if session.key not in blocking_keys]
        remaining.sort(
            key=lambda session: (
                -(
                    (session.last_activity or session.started_at).timestamp()
                    if session.last_activity or session.started_at
                    else 0.0
                ),
                0 if session.question else 1,
            )
        )
        selected = blocking + remaining[: max(0, self.config.max_sessions - len(blocking))]
        self.analysis_session_count = len(selected)
        self.total_session_count = len(visible)
        return selected

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._snapshot_row(session) for session in self._analysis_sessions()]

    @staticmethod
    def _interaction_json(interaction: PendingInteraction | None) -> dict | None:
        if interaction is None:
            return None
        return {
            "id": interaction.id,
            "kind": interaction.kind,
            "title": interaction.title,
            "message": interaction.message,
            "command": interaction.command,
            "cwd": interaction.cwd,
            "url": interaction.url,
            "decisions": list(interaction.decisions),
            "questions": [
                {
                    "id": question.id,
                    "prompt": question.prompt,
                    "secret": question.secret,
                    "allow_other": question.allow_other,
                    "options": [option.label for option in question.options],
                    "option_details": [
                        {
                            "label": option.label,
                            "value": option.value,
                            "description": option.description,
                        }
                        for option in question.options
                    ],
                }
                for question in interaction.questions
            ],
        }

    @staticmethod
    def _evidence_signature(row: dict[str, Any]) -> str:
        """Identity of the evidence behind advice, excluding transient liveness.

        Poll-only changes such as thinking/activity/subagent counts must not make
        findings disappear or resurrect handled findings. Transcript, question,
        branch and PR changes are material and intentionally do.
        """
        stable = {
            key: value
            for key, value in row.items()
            if key not in {"state", "activity", "subagents"}
        }
        git = stable.get("git")
        if isinstance(git, dict):
            stable["git"] = {key: value for key, value in git.items() if key != "dirty"}
        return json.dumps(stable, sort_keys=True, default=str, separators=(",", ":"))

    @classmethod
    def _analysis_signature(cls, snapshot: list[dict[str, Any]]) -> str:
        """Identity of all material evidence considered by the model.

        Session collection order and transient runtime state can change between
        polls without giving Deckhand anything new to decide. Keep resolving git
        context on schedule, but invoke the model only when a chat's durable
        transcript, question, branch, or PR evidence actually changes.
        """
        evidence = {
            str(row.get("session_key", "")): cls._evidence_signature(row)
            for row in snapshot
        }
        return json.dumps(evidence, sort_keys=True, separators=(",", ":"))

    def _stabilize_insights(
        self,
        view: AssistantView,
        prior: AssistantView,
        evidence: dict[str, str],
    ) -> AssistantView:
        """Keep advice stable until its session evidence materially changes."""
        # Findings can refer to chats that temporarily fall outside max_sessions.
        # Compare those chats directly instead of treating window membership as
        # evidence that the concern was resolved.
        for old in prior.insights:
            session = self.state.sessions.get(old.session_key)
            if old.session_key not in evidence and session is not None:
                evidence[old.session_key] = self._evidence_signature(self._snapshot_row(session))
        fresh_by_session = {insight.session_key: insight for insight in view.insights}
        stabilized: list[AssistantInsight] = []
        retained = False

        # Preserve established ordering and wording when a stochastic refresh
        # omits or rephrases an unchanged chat. Fresh advice replaces it only
        # after the underlying evidence changes.
        for old in prior.insights:
            fresh = fresh_by_session.pop(old.session_key, None)
            current = evidence.get(old.session_key)
            if current is not None and current == self._evidence_signatures.get(old.session_key):
                session = self.state.sessions.get(old.session_key)
                resumed = bool(
                    session
                    and session.thinking
                    and old.kind in {"waiting", "stalled"}
                    and self._interaction(session) is None
                    and not session.question
                )
                if resumed and fresh is not None:
                    stabilized.append(fresh)
                elif not resumed:
                    stabilized.append(old)
                    retained = True
            elif fresh is not None:
                stabilized.append(fresh)
        stabilized.extend(fresh_by_session.values())

        visible = []
        for insight in stabilized:
            current = evidence.get(insight.session_key)
            handled = self._handled.get(insight.session_key)
            if handled is not None and handled == current:
                self._handled_insights[insight.session_key] = insight
                if self.state.db:
                    self.state.db.record_assistant_handled(
                        insight.session_key,
                        handled,
                        insight.kind,
                        insight.headline,
                        insight.detail,
                    )
                continue
            if handled is not None and current is not None:
                self._handled.pop(insight.session_key, None)
                self._handled_insights.pop(insight.session_key, None)
                if self.state.db:
                    self.state.db.delete_assistant_handled(insight.session_key)
            visible.append(insight)

        summary = view.summary
        if retained:
            summary = self._tracking_summary(len(visible))
        return replace(view, summary=summary, insights=tuple(visible))

    @staticmethod
    def _tracking_summary(count: int) -> str:
        if count == 0:
            return "Nothing needs your attention right now."
        if count == 1:
            return "Deckhand is tracking 1 item that still needs attention."
        return f"Deckhand is tracking {count} items that still need attention."

    def handle(self, session_key: str) -> bool:
        """Acknowledge advice until material evidence for its chat changes."""
        insight = next(
            (insight for insight in self.view.insights if insight.session_key == session_key),
            None,
        )
        if insight is None:
            return False
        signature = self._evidence_signatures.get(session_key)
        if signature is None:
            return False
        self._handled[session_key] = signature
        self._handled_insights[session_key] = insight
        if self.state.db:
            self.state.db.record_assistant_handled(
                session_key,
                signature,
                insight.kind,
                insight.headline,
                insight.detail,
            )
        insights = tuple(
            insight for insight in self.view.insights if insight.session_key != session_key
        )
        self.view = replace(
            self.view,
            summary=self._tracking_summary(len(insights)),
            insights=insights,
        )
        self._save_checkpoint()
        self.state.bus.publish("assistant")
        return True

    @property
    def handled_items(self) -> tuple[AssistantHandledItem, ...]:
        """Most recent handled card; older entries stay persisted as an undo stack."""
        for session_key in reversed(self._handled):
            insight = self._handled_insights.get(session_key)
            session = self.state.sessions.get(session_key)
            headline = (
                insight.headline
                if insight is not None
                else (session.title if session is not None and session.title else "Handled item")
            )
            return (AssistantHandledItem(session_key, headline),)
        return ()

    def handled_insight(self, session_key: str) -> AssistantInsight | None:
        if session_key not in self._handled:
            return None
        return self._handled_insights.get(session_key)

    def unhandle(self, session_key: str) -> bool:
        """Restore a handled card only while its authoritative evidence is current."""
        if session_key not in self._handled:
            return False
        self._handled.pop(session_key, None)
        if self.state.db:
            self.state.db.delete_assistant_handled(session_key)
        insight = self._handled_insights.pop(session_key, None)
        session = self.state.sessions.get(session_key)
        current = (
            self._evidence_signature(self._snapshot_row(session)) if session is not None else None
        )
        candidate = AssistantView(
            state="ready",
            insights=(insight,) if insight is not None and current is not None else (),
        )
        candidate = self._suppress_unattributed_pr_insights(candidate)
        candidate = self._suppress_terminal_pr_insights(candidate)
        candidate = self._suppress_non_actionable_insights(candidate)
        candidate = self._enrich_pr_headlines(candidate)
        if (
            current == self._evidence_signatures.get(session_key)
            and candidate.insights
            and not any(item.session_key == session_key for item in self.view.insights)
        ):
            insights = self.view.insights + candidate.insights
            self.view = replace(
                self.view,
                summary=self._tracking_summary(len(insights)),
                insights=insights,
            )
        self._save_checkpoint()
        self.request_refresh()
        self.state.bus.publish("assistant")
        return True

    @staticmethod
    def _prompt(snapshot: list[dict[str, Any]], total_sessions: int | None = None) -> str:
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        total = total_sessions if total_sessions is not None else len(snapshot)
        return f"""You are the orchestration assistant inside AgentDeck.
Analyze the supplied coding-agent dashboard snapshot. Do not use tools. Give concise,
specific advice about agents that are waiting, stuck, duplicating work, newly finished,
or need coordination. Prefer silence over generic advice.

The git and pull-request context was resolved authoritatively by AgentDeck. Treat each
pull request's status as ground truth. A merged or closed-unmerged PR is terminal: never
treat it as active work or suggest review, merge, or coordination for it. Distinguish
open and draft PRs. A chat may contain new work after an older PR became terminal, so
evaluate the current question and transcript independently from historical PRs.
Never attach a pull request from one session's git context to another session.

For an ordinary question interaction, you may suggest answers. Mark safe_to_auto_answer
true only when all answers are unambiguous choices explicitly present in the question,
reversible, low-impact, and confidently inferable from the visible context. Never mark
approvals, permissions, secrets, open-ended product choices, or destructive actions safe.
Use session_key, interaction_id, and question_id exactly as supplied. Set interaction_id
to the current interaction id only when proposing answers; otherwise set it to null. Set
coordination_key to the same short stable identifier for insights about the same underlying
PR, issue, branch, or task; use null when there is no shared coordination identity.
Return at most one insight per session. Polling is frequent, so omit an existing concern
only when the supplied evidence shows that it was resolved.
When multiple sessions concern the same underlying PR, issue, branch, or task, emit one
coordination insight anchored to the chat that should own the work, not one card per chat.
Do not report an agent merely because it is actively progressing or validating work.

Coverage: {len(snapshot)} selected sessions out of {total}. Do not make claims about
sessions outside this snapshot.

Dashboard snapshot:
{payload}
"""

    @staticmethod
    def _parse_result(raw: dict[str, Any], valid_keys: set[str]) -> AssistantView:
        summary = raw.get("summary")
        insights = []
        for item in raw.get("insights") or []:
            if not isinstance(item, dict) or item.get("session_key") not in valid_keys:
                continue
            answers = []
            for answer in item.get("answers") or []:
                if not isinstance(answer, dict) or not isinstance(answer.get("question_id"), str):
                    continue
                values = tuple(
                    value for value in answer.get("values") or [] if isinstance(value, str)
                )
                if values:
                    answers.append(AssistantAnswer(answer["question_id"], values))
            confidence = item.get("confidence")
            insights.append(
                AssistantInsight(
                    session_key=item["session_key"],
                    kind=item.get("kind") if isinstance(item.get("kind"), str) else "info",
                    headline=(
                        item.get("headline")
                        if isinstance(item.get("headline"), str)
                        else "Agent update"
                    ),
                    detail=item.get("detail") if isinstance(item.get("detail"), str) else "",
                    interaction_id=(
                        item.get("interaction_id")
                        if isinstance(item.get("interaction_id"), str)
                        else None
                    ),
                    coordination_key=(
                        item.get("coordination_key")
                        if isinstance(item.get("coordination_key"), str)
                        else None
                    ),
                    answers=tuple(answers),
                    safe_to_auto_answer=bool(item.get("safe_to_auto_answer")),
                    confidence=(float(confidence) if isinstance(confidence, (int, float)) else 0.0),
                )
            )
        return AssistantView(
            state="ready",
            summary=summary if isinstance(summary, str) else "Analysis complete.",
            insights=tuple(insights),
            analyzed_at=datetime.now(UTC),
        )

    @staticmethod
    def _pr_claims(insight: AssistantInsight) -> tuple[set[int], set[tuple[str, int]]]:
        text = f"{insight.headline}\n{insight.detail}"
        numbers = {
            int(number)
            for match in _INSIGHT_PR_SEQUENCE_RE.finditer(text)
            for number in re.findall(r"\d+", match.group(1))
        }
        repositories = {
            (repository.casefold(), int(number))
            for repository, number in _INSIGHT_PR_URL_RE.findall(text)
        }
        return numbers, repositories

    def _suppress_terminal_pr_insights(self, view: AssistantView) -> AssistantView:
        """Suppress only advice whose claimed PR work is entirely terminal."""
        insights = []
        for insight in view.insights:
            numbers, repositories = self._pr_claims(insight)
            if not numbers and not repositories:
                insights.append(insight)
                continue
            context = self.contexts.get(insight.session_key)
            pulls = context.pull_requests if context is not None else ()
            claimed = [
                pull
                for pull in pulls
                if pull.number in numbers
                or (pull.repository.casefold(), pull.number) in repositories
            ]
            if claimed and all(pull.status in {"closed", "merged"} for pull in claimed):
                continue
            insights.append(insight)
        insights = tuple(insights)
        if insights == view.insights:
            return view
        return AssistantView(
            state=view.state,
            summary=(view.summary if insights else "Nothing needs your attention right now."),
            insights=insights,
            actions=view.actions,
            analyzed_at=view.analyzed_at,
            error=view.error,
        )

    def _suppress_unattributed_pr_insights(self, view: AssistantView) -> AssistantView:
        """Reject PR claims copied from another chat in the shared snapshot."""
        insights = []
        for insight in view.insights:
            numbers, repositories = self._pr_claims(insight)
            if not numbers and not repositories:
                insights.append(insight)
                continue
            context = self.contexts.get(insight.session_key)
            pulls = context.pull_requests if context is not None else ()
            valid_numbers = {pull.number for pull in pulls}
            valid_repositories = {(pull.repository.casefold(), pull.number) for pull in pulls}
            if numbers <= valid_numbers and repositories <= valid_repositories:
                insights.append(insight)
                continue
            log.debug(
                "Deckhand suppressed cross-chat PR insight for %s: %s",
                insight.session_key,
                insight.headline,
            )
        result = tuple(insights)
        if result == view.insights:
            return view
        return replace(
            view,
            summary=self._tracking_summary(len(result)),
            insights=result,
        )

    def _suppress_non_actionable_insights(self, view: AssistantView) -> AssistantView:
        """Active progress alone is not an item that needs the operator's attention."""
        insights = tuple(
            insight
            for insight in view.insights
            if not (
                insight.kind == "info"
                and (session := self.state.sessions.get(insight.session_key)) is not None
                and session.thinking
                and self._interaction(session) is None
                and not session.question
            )
        )
        if insights == view.insights:
            return view
        return replace(view, summary=self._tracking_summary(len(insights)), insights=insights)

    @staticmethod
    def _pr_feature_word(title: str, project: str) -> str:
        for word in _PR_TITLE_WORD_RE.findall(title):
            if (
                not word.isdigit()
                and word.casefold() not in _GENERIC_PR_TITLE_WORDS
                and word.casefold() != project.casefold()
            ):
                return word
        return "Change"

    def _enrich_pr_headlines(self, view: AssistantView) -> AssistantView:
        """Prefix PR advice with its project and a compact title-derived feature word."""
        insights = []
        for insight in view.insights:
            context = self.contexts.get(insight.session_key)
            numbers, repositories = self._pr_claims(insight)
            pulls = context.pull_requests if context is not None else ()
            pull = next(
                (
                    item
                    for item in pulls
                    if item.number in numbers
                    or (item.repository.casefold(), item.number) in repositories
                ),
                None,
            )
            if pull is None:
                insights.append(insight)
                continue
            repository_name = pull.repository.rsplit("/", 1)[-1]
            project = _PROJECT_DISPLAY_NAMES.get(
                repository_name.casefold(), repository_name.replace("-", " ").title()
            )
            feature = self._pr_feature_word(pull.title, project)
            prefix = f"{project} · {feature} · PR #{pull.number}"
            if insight.headline.startswith(prefix):
                insights.append(insight)
                continue
            action = _PR_HEADLINE_PREFIX_RE.sub("", insight.headline).strip()
            insights.append(replace(insight, headline=f"{prefix} {action}".strip()))
        result = tuple(insights)
        if result == view.insights:
            return view
        return replace(view, insights=result)

    def _deduplicate_insights(self, view: AssistantView) -> AssistantView:
        """Keep one operator action per underlying PR, issue, branch, or model group."""
        seen: set[str] = set()
        insights = []
        for insight in view.insights:
            context = self.contexts.get(insight.session_key)
            numbers, repositories = self._pr_claims(insight)
            pulls = context.pull_requests if context is not None else ()
            claimed = sorted(
                f"{pull.repository.casefold()}#{pull.number}"
                for pull in pulls
                if pull.number in numbers
                or (pull.repository.casefold(), pull.number) in repositories
            )
            session = self.state.sessions.get(insight.session_key)
            if claimed:
                key = "prs:" + ",".join(claimed)
            elif session is not None and session.issue_url:
                key = f"issue:{session.issue_url.casefold()}"
            elif (
                context is not None
                and context.repository
                and context.branch
                and context.branch not in {"main", "master"}
            ):
                key = f"branch:{context.repository.casefold()}:{context.branch.casefold()}"
            elif insight.coordination_key:
                namespace = (
                    context.repository.casefold()
                    if context is not None and context.repository
                    else str(session.cwd)
                    if session is not None and session.cwd
                    else "global"
                )
                key = f"model:{namespace}:{insight.coordination_key.strip().casefold()}"
            else:
                key = ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            insights.append(insight)
        result = tuple(insights)
        if result == view.insights:
            return view
        return replace(view, summary=self._tracking_summary(len(result)), insights=result)

    async def _auto_answer(self, view: AssistantView) -> tuple[AssistantAction, ...]:
        if not self.config.auto_answer:
            return ()
        actions = []
        for insight in view.insights:
            if (
                not insight.safe_to_auto_answer
                or insight.confidence < self.config.auto_answer_confidence
            ):
                continue
            session = self.state.sessions.get(insight.session_key)
            if session is None:
                continue
            interaction = self._interaction(session)
            if (
                interaction is None
                or insight.interaction_id != interaction.id
                or not self._answers_are_safe(interaction, insight.answers)
            ):
                continue
            if interaction.id in self._answered_interactions:
                continue
            account = next(item for item in self.accounts if item.key == session.account_key)
            answers = {answer.question_id: list(answer.values) for answer in insight.answers}
            result = await PROVIDERS[account.provider_id].answer_interaction(
                account,
                session,
                interaction.id,
                answers=answers,
                decision=None,
            )
            self._answered_interactions.add(interaction.id)
            if result.accepted:
                actions.append(AssistantAction(session.key, f"Answered: {insight.headline}"))
        return tuple(actions)

    @staticmethod
    def _answers_are_safe(
        interaction: PendingInteraction | None, answers: tuple[AssistantAnswer, ...]
    ) -> bool:
        if interaction is None or interaction.kind != "question" or not interaction.questions:
            return False
        supplied = {answer.question_id: answer.values for answer in answers}
        if set(supplied) != {question.id for question in interaction.questions}:
            return False
        for question in interaction.questions:
            if question.secret or question.allow_other or not question.options:
                return False
            safety_text = "\n".join(
                (
                    question.header,
                    question.prompt,
                    *(option.label for option in question.options),
                    *(option.description for option in question.options),
                )
            )
            if _UNSAFE_AUTO_ANSWER_RE.search(safety_text):
                return False
            allowed = {
                value
                for option in question.options
                for value in (option.label, option.value)
                if value
            }
            if not supplied[question.id] or any(
                value not in allowed for value in supplied[question.id]
            ):
                return False
        return True

    async def refresh(self, snapshot: list[dict[str, Any]] | None = None) -> None:
        if snapshot is None:
            sessions = self._analysis_sessions()
            resolved = await self.context_resolver.resolve(sessions)
            live_keys = set(self.state.sessions)
            self.contexts = {
                key: context for key, context in self.contexts.items() if key in live_keys
            }
            self.contexts.update(resolved)
            snapshot = [self._snapshot_row(session) for session in sessions]
        else:
            self.analysis_session_count = len(snapshot)
            self.total_session_count = len(self.state.visible_sessions())
        analysis_signature = self._analysis_signature(snapshot)
        if not snapshot:
            self.view = AssistantView(
                state="ready", summary="Nothing needs your attention right now."
            )
            self._last_signature = analysis_signature
            self._save_checkpoint()
            self.state.bus.publish("assistant")
            return
        account = self._account()
        if account is None:
            self.view = AssistantView(
                state="error",
                summary="Assistant unavailable.",
                error="No Codex account is configured.",
            )
            self.state.bus.publish("assistant")
            return
        prior = self._suppress_unattributed_pr_insights(self.view)
        prior = self._suppress_terminal_pr_insights(prior)
        prior = self._suppress_non_actionable_insights(prior)
        prior = self._enrich_pr_headlines(prior)
        prior = self._deduplicate_insights(prior)
        self.view = AssistantView(
            state="analyzing",
            summary=prior.summary,
            insights=prior.insights,
            actions=prior.actions,
            analyzed_at=prior.analyzed_at,
        )
        self.state.bus.publish("assistant")
        try:
            raw = await self.runner(
                account, self.config, self._prompt(snapshot, self.total_session_count)
            )
            view = self._parse_result(raw, {row["session_key"] for row in snapshot})
            view = self._suppress_unattributed_pr_insights(view)
            view = self._suppress_terminal_pr_insights(view)
            view = self._suppress_non_actionable_insights(view)
            view = self._enrich_pr_headlines(view)
            view = self._deduplicate_insights(view)
            evidence = {row["session_key"]: self._evidence_signature(row) for row in snapshot}
            view = self._stabilize_insights(view, prior, evidence)
            view = self._deduplicate_insights(view)
            view = replace(view, summary=self._tracking_summary(len(view.insights)))
            actions = await self._auto_answer(view)
            self.view = AssistantView(
                state=view.state,
                summary=view.summary,
                insights=view.insights,
                actions=tuple((prior.actions + actions)[-6:]),
                analyzed_at=view.analyzed_at,
            )
            self._evidence_signatures.update(evidence)
            self._last_signature = analysis_signature
            self._save_checkpoint()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- background failure belongs in the panel
            log.warning("orchestration assistant failed: %s", exc)
            self.view = AssistantView(
                state="error",
                summary=prior.summary,
                insights=prior.insights,
                actions=prior.actions,
                analyzed_at=prior.analyzed_at,
                error=str(exc),
            )
        self.state.bus.publish("assistant")

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except TimeoutError:
                pass
            self._wake.clear()
            due = loop.time() - self._last_run >= self.config.refresh_interval_s
            visible_keys = {session.key for session in self.state.visible_sessions()}
            newly_visible = bool(visible_keys - self._known_visible_session_keys)
            self._known_visible_session_keys = visible_keys
            if not self._force and not due and not newly_visible:
                continue
            # Consume only the request that caused this check. A second request
            # arriving while context is resolved must remain armed for the next
            # loop iteration.
            self._force = False
            started_at = loop.time()
            sessions = self._analysis_sessions()
            collected_at = loop.time()
            resolved = await self.context_resolver.resolve(sessions)
            resolved_at = loop.time()
            session_keys = set(self.state.sessions)
            self.contexts = {
                key: context for key, context in self.contexts.items() if key in session_keys
            }
            self.contexts.update(resolved)
            snapshot = [self._snapshot_row(session) for session in sessions]
            signature = self._analysis_signature(snapshot)
            compared_at = loop.time()
            manual = self._manual_refresh_pending
            self._manual_refresh_pending = False
            if signature == self._last_signature:
                self._last_run = loop.time()
                if manual:
                    self.refresh_status = "No material changes · Luna not run"
                    self.state.bus.publish("assistant")
                log.debug(
                    "Deckhand refresh unchanged in %.3fs (collect %.3fs, context %.3fs, "
                    "compare %.3fs, manual=%s)",
                    self._last_run - started_at,
                    collected_at - started_at,
                    resolved_at - collected_at,
                    compared_at - resolved_at,
                    manual,
                )
                continue
            self._last_run = loop.time()
            if manual:
                self.refresh_status = "Material changes found · running Luna…"
            await self.refresh(snapshot)
            if manual:
                self.refresh_status = (
                    "Material changes found · analysis updated"
                    if self.view.state == "ready"
                    else "Material changes found · analysis failed"
                )
                self.state.bus.publish("assistant")
            log.debug(
                "Deckhand refresh analyzed in %.3fs (collect %.3fs, context %.3fs, "
                "compare %.3fs, manual=%s)",
                loop.time() - started_at,
                collected_at - started_at,
                resolved_at - collected_at,
                compared_at - resolved_at,
                manual,
            )
