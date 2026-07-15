"""Per-session attention triage for the dashboard.

Deckhand answers one question for the operator: **which agents need me now?**
Structured signals (a pending prompt, a question, a blocked issue, an open PR,
a stall) are decided deterministically in ``triage``; only a finished agent
whose final prose might hide an unresolved problem is sent to a small, cached,
per-session Codex classifier. The service here orchestrates that loop and owns
all mutable/persisted state; ``triage`` stays pure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, AssistantConfig
from .git_context import GitContext, GitContextResolver
from .models import Account, PendingInteraction, Session
from .providers import PROVIDERS
from .state import AppState
from .triage import (
    AssistantInsight,
    AssistantView,
    Verdict,
    card_priority,
    classification_prompt,
    needs_llm,
    parse_verdict,
    structured_trigger,
    tracking_summary,
    verdict_card,
)

log = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("assistant_output.schema.json")
_MAX_SIGNATURE_CHARS = 600
# A session marked active but silent this long is treated as possibly hung.
_HANG_AFTER_S = 600.0


@dataclass(frozen=True)
class AssistantHandledItem:
    session_key: str
    headline: str


Runner = Callable[[Account, AssistantConfig, str], Awaitable[dict[str, Any]]]


def _trim(value: str | None) -> str | None:
    if not value:
        return None
    return value[-_MAX_SIGNATURE_CHARS:]


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
    """Run one read-only, ephemeral Codex classification and return its JSON result."""
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
            # Codex stderr can echo the prompt (including transcript excerpts); keep the
            # operator-visible error generic so chat data never lands in logs.
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
    """Debounce session changes into per-session attention triage."""

    HANG_AFTER_S = _HANG_AFTER_S

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
            summary=("Starting triage…" if self.config.enabled else "Disabled")
        )
        self._task: asyncio.Task | None = None
        self._session_watch_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._known_visible_session_keys: set[str] = set()
        self._last_run = 0.0
        self.refresh_status: str | None = None
        self._manual_refresh_pending = False
        # session_key -> (evidence signature, LLM verdict) — re-run only on change.
        self._verdicts: dict[str, tuple[str, Verdict]] = {}
        # session_key -> evidence signature backing the currently displayed cards.
        self._signatures: dict[str, str] = {}
        self.analysis_session_count = 0
        self.total_session_count = 0
        self._force = True
        checkpoint = state.db.load_assistant_checkpoint() if state.db else None
        self._restore_checkpoint(checkpoint)
        handled = state.db.load_assistant_handled() if state.db else {}
        self._handled = {session_key: record[0] for session_key, record in handled.items()}
        self._handled_insights = {
            session_key: AssistantInsight(session_key, kind, headline, detail or "")
            for session_key, (_, kind, headline, detail) in handled.items()
            if kind is not None and headline is not None
        }

    # --- persistence ---------------------------------------------------

    def _restore_checkpoint(self, payload: dict[str, Any] | None) -> None:
        if not payload or payload.get("version") != 4:
            return
        try:
            raw_view = payload["view"]
            insights = tuple(
                AssistantInsight(
                    str(item["session_key"]),
                    str(item["kind"]),
                    str(item["headline"]),
                    str(item["detail"]),
                )
                for item in raw_view.get("insights", [])
            )
            analyzed_at = raw_view.get("analyzed_at")
            self.view = AssistantView(
                state="ready",
                summary=str(raw_view["summary"]),
                insights=insights,
                analyzed_at=datetime.fromisoformat(analyzed_at) if analyzed_at else None,
            )
            self._signatures = {
                str(k): str(v) for k, v in payload.get("signatures", {}).items()
            }
            self._verdicts = {
                str(k): (
                    str(entry[0]),
                    Verdict(bool(entry[1]), str(entry[2]), str(entry[3])),
                )
                for k, entry in payload.get("verdicts", {}).items()
                if isinstance(entry, list) and len(entry) == 4
            }
            self._force = False
        except (KeyError, TypeError, ValueError):
            log.warning("Ignoring invalid Deckhand checkpoint")

    def _checkpoint_payload(self) -> dict[str, Any]:
        return {
            "version": 4,
            "view": {
                "summary": self.view.summary,
                "insights": [
                    {
                        "session_key": insight.session_key,
                        "kind": insight.kind,
                        "headline": insight.headline,
                        "detail": insight.detail,
                    }
                    for insight in self.view.insights
                ],
                "analyzed_at": (
                    self.view.analyzed_at.isoformat() if self.view.analyzed_at else None
                ),
            },
            "signatures": self._signatures,
            "verdicts": {
                key: [sig, verdict.attention, verdict.summary, verdict.reason]
                for key, (sig, verdict) in self._verdicts.items()
            },
        }

    def _save_checkpoint(self) -> None:
        if self.state.db and self.view.state == "ready":
            self.state.db.record_assistant_checkpoint(self._checkpoint_payload())

    # --- lifecycle -----------------------------------------------------

    def _account(self) -> Account | None:
        codex = [account for account in self.accounts if account.provider_id == "codex"]
        if self.config.account_key:
            return next(
                (account for account in codex if account.key == self.config.account_key), None
            )
        return codex[0] if codex else None

    async def start(self) -> None:
        if self.config.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="attention-triage")
            self._session_watch_task = asyncio.create_task(
                self._watch_sessions(), name="attention-triage-sessions"
            )

    async def stop(self) -> None:
        tasks = tuple(t for t in (self._task, self._session_watch_task) if t is not None)
        self._task = None
        self._session_watch_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _watch_sessions(self) -> None:
        """Wake the cheap eligibility check as soon as collection changes."""
        with self.state.bus.subscribe("sessions") as subscription:
            while True:
                await subscription.get()
                self._wake.set()

    def request_refresh(self, *, manual: bool = False) -> bool:
        if not self.config.enabled:
            return False
        if manual:
            self._manual_refresh_pending = True
            self.refresh_status = "Checking current evidence…"
        self._force = True
        self._wake.set()
        return True

    # --- session selection & evidence ---------------------------------

    def _interaction(self, session: Session) -> PendingInteraction | None:
        account = next((a for a in self.accounts if a.key == session.account_key), None)
        if account is None:
            return None
        return PROVIDERS[account.provider_id].pending_interaction(account, session)

    def _triage_sessions(self) -> list[Session]:
        """Blocking chats first, then most-recently-active, capped to max_sessions."""
        visible = self.state.visible_sessions()
        blocking = [s for s in visible if s.question or self._interaction(s) is not None]
        blocking_keys = {s.key for s in blocking}
        remaining = [s for s in visible if s.key not in blocking_keys]
        remaining.sort(
            key=lambda s: -(
                (s.last_activity or s.started_at).timestamp()
                if (s.last_activity or s.started_at)
                else 0.0
            )
        )
        selected = blocking + remaining[: max(0, self.config.max_sessions - len(blocking))]
        self.analysis_session_count = len(selected)
        self.total_session_count = len(visible)
        return selected

    @staticmethod
    def _interaction_signature(interaction: PendingInteraction | None) -> Any:
        if interaction is None:
            return None
        return [
            interaction.id,
            interaction.kind,
            interaction.message,
            [question.prompt for question in interaction.questions],
        ]

    def _evidence_signature(
        self,
        session: Session,
        context: GitContext | None,
        interaction: PendingInteraction | None,
    ) -> str:
        """Stable identity of a session's material state (excludes transient liveness).

        Drives the classifier cache, handled-card validity, and change detection.
        Thinking/activity/subagent churn is deliberately excluded so cards neither
        flicker nor get reclassified on poll noise.
        """
        stable = {
            "title": session.title,
            "cwd": str(session.cwd) if session.cwd else None,
            "question": session.question,
            "last_role": session.last_role,
            "last_prompt": _trim(session.last_prompt),
            "last_text": _trim(session.last_text),
            "worker_type": session.worker_type,
            "issue_status_kind": session.issue_status_kind,
            "interaction": self._interaction_signature(interaction),
            "prs": (
                sorted((p.number, p.status, p.draft) for p in context.pull_requests)
                if context is not None
                else []
            ),
        }
        return json.dumps(stable, sort_keys=True, default=str, separators=(",", ":"))

    async def ensure_session_context(
        self, session: Session, *, transcript_context: str | None = None
    ) -> GitContext | None:
        """Resolve git/PR metadata when a chat outside the triage window opens."""
        existing = self.contexts.get(session.key)
        if existing is not None and not transcript_context:
            return existing
        target = session
        if transcript_context:
            target = replace(
                session,
                last_text="\n".join(
                    v for v in (session.last_text, transcript_context) if v
                ),
            )
        try:
            context = (await self.context_resolver.resolve([target])).get(session.key)
        except Exception as exc:  # noqa: BLE001 -- metadata must not break chat pages
            log.debug("Deckhand context resolve failed for %s: %s", session.key, exc)
            return None
        if context is not None and context != existing:
            self.contexts[session.key] = context
            self.request_refresh()
        return context

    # --- classification ------------------------------------------------

    async def _classify(self, account: Account, session: Session) -> tuple[Verdict, bool]:
        """Classify one finished agent's final message. Returns (verdict, ok).

        Fails open: on any error the verdict is attention=True so a handoff is
        never silently dropped, and ok=False lets the caller prefer a cached
        verdict and flag the run as degraded.
        """
        try:
            raw = await self.runner(account, self.config, classification_prompt(session))
            return parse_verdict(raw), True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- background failure belongs in the panel
            log.warning("attention triage failed for %s: %s", session.key, exc)
            first_line = (session.last_text or "").strip().splitlines()
            summary = first_line[0][:140].rstrip() if first_line else "Finished"
            reason = "Deckhand could not read this agent's final message."
            return Verdict(True, summary, reason), False

    async def refresh(self, *, manual: bool = False) -> None:
        sessions = self._triage_sessions()
        resolved = await self.context_resolver.resolve(sessions)
        live_keys = set(self.state.sessions)
        self.contexts = {k: v for k, v in self.contexts.items() if k in live_keys}
        self.contexts.update(resolved)
        self._verdicts = {k: v for k, v in self._verdicts.items() if k in live_keys}

        now = datetime.now(UTC)
        account = self._account()

        cards: list[AssistantInsight] = []
        signatures: dict[str, str] = {}
        pending: list[tuple[Session, str]] = []

        for session in sessions:
            context = self.contexts.get(session.key)
            interaction = self._interaction(session)
            signature = self._evidence_signature(session, context, interaction)
            signatures[session.key] = signature

            card = structured_trigger(
                session, context, interaction, now, hang_after_s=self.HANG_AFTER_S
            )
            if card is not None:
                cards.append(card)
                continue
            if not needs_llm(session):
                continue
            cached = self._verdicts.get(session.key)
            if cached is not None and cached[0] == signature:
                if cached[1].attention:
                    cards.append(verdict_card(session.key, cached[1]))
            elif account is not None:
                pending.append((session, signature))

        if not signatures and not self.view.insights:
            self._commit_view(AssistantView(state="ready"), signatures, manual=manual)
            return

        degraded = False
        if pending:
            self.view = replace(self.view, state="analyzing")
            self.state.bus.publish("assistant")
            results = await asyncio.gather(
                *(self._classify(account, session) for session, _ in pending)
            )
            for (session, signature), (verdict, ok) in zip(pending, results, strict=True):
                if ok:
                    self._verdicts[session.key] = (signature, verdict)
                else:
                    degraded = True
                    cached = self._verdicts.get(session.key)
                    verdict = cached[1] if cached is not None else verdict
                if verdict.attention:
                    cards.append(verdict_card(session.key, verdict))

        cards = self._apply_handled(cards, signatures)
        cards = self._dedupe_and_order(cards)
        view = AssistantView(
            state="ready",
            summary=tracking_summary(len(cards)),
            insights=tuple(cards),
            analyzed_at=now,
            error="Some agents could not be read." if degraded else None,
        )
        self._commit_view(view, signatures, manual=manual)

    def _apply_handled(
        self, cards: list[AssistantInsight], signatures: dict[str, str]
    ) -> list[AssistantInsight]:
        """Hide a card while its session stays acknowledged; auto-restore on change."""
        visible: list[AssistantInsight] = []
        for card in cards:
            handled_sig = self._handled.get(card.session_key)
            current = signatures.get(card.session_key)
            if handled_sig is not None and handled_sig == current:
                self._handled_insights[card.session_key] = card
                if self.state.db:
                    self.state.db.record_assistant_handled(
                        card.session_key, handled_sig, card.kind, card.headline, card.detail
                    )
                continue
            if handled_sig is not None:
                self._handled.pop(card.session_key, None)
                self._handled_insights.pop(card.session_key, None)
                if self.state.db:
                    self.state.db.delete_assistant_handled(card.session_key)
            visible.append(card)
        return visible

    @staticmethod
    def _dedupe_and_order(cards: list[AssistantInsight]) -> list[AssistantInsight]:
        """Collapse identical cards (same PR/issue on two chats) and sink finished ones.

        Headlines carry their identity (``PR #255 ready for review``, ``store#12
        blocked …``), so equal headlines are genuine duplicates. Active-attention
        cards keep their incoming (blocking-first, recency) order; ``finished``
        PR-review cards stably sort to the bottom.
        """
        seen: set[str] = set()
        unique: list[AssistantInsight] = []
        for card in cards:
            if card.headline in seen:
                continue
            seen.add(card.headline)
            unique.append(card)
        unique.sort(key=card_priority)
        return unique

    def _commit_view(
        self, view: AssistantView, signatures: dict[str, str], *, manual: bool
    ) -> None:
        self._signatures = signatures
        changed = view != self.view
        self.view = view
        self._save_checkpoint()
        if manual:
            self.refresh_status = (
                "Updated" if not view.error else "Some agents could not be read"
            )
        if changed or manual:
            self.state.bus.publish("assistant")

    # --- handled / undo ------------------------------------------------

    def handle(self, session_key: str) -> bool:
        insight = next(
            (i for i in self.view.insights if i.session_key == session_key), None
        )
        signature = self._signatures.get(session_key)
        if insight is None or signature is None:
            return False
        self._handled[session_key] = signature
        self._handled_insights[session_key] = insight
        if self.state.db:
            self.state.db.record_assistant_handled(
                session_key, signature, insight.kind, insight.headline, insight.detail
            )
        insights = tuple(i for i in self.view.insights if i.session_key != session_key)
        self.view = replace(
            self.view, summary=tracking_summary(len(insights)), insights=insights
        )
        self._save_checkpoint()
        self.state.bus.publish("assistant")
        return True

    def unhandle(self, session_key: str) -> bool:
        if session_key not in self._handled:
            return False
        handled_sig = self._handled.pop(session_key)
        insight = self._handled_insights.pop(session_key, None)
        if self.state.db:
            self.state.db.delete_assistant_handled(session_key)
        # Restore the card immediately when its evidence is still current, so the
        # undo is visible without waiting for the next triage tick.
        current = self._signatures.get(session_key)
        already_shown = any(i.session_key == session_key for i in self.view.insights)
        if insight is not None and current == handled_sig and not already_shown:
            insights = self.view.insights + (insight,)
            self.view = replace(
                self.view, summary=tracking_summary(len(insights)), insights=insights
            )
        self._save_checkpoint()
        self.request_refresh()
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
                else (session.title if session and session.title else "Handled item")
            )
            return (AssistantHandledItem(session_key, headline),)
        return ()

    def handled_insight(self, session_key: str) -> AssistantInsight | None:
        if session_key not in self._handled:
            return None
        return self._handled_insights.get(session_key)

    # --- loop ----------------------------------------------------------

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except TimeoutError:
                pass
            self._wake.clear()
            due = loop.time() - self._last_run >= self.config.refresh_interval_s
            visible_keys = {s.key for s in self.state.visible_sessions()}
            newly_visible = bool(visible_keys - self._known_visible_session_keys)
            self._known_visible_session_keys = visible_keys
            if not self._force and not due and not newly_visible:
                continue
            self._force = False
            manual = self._manual_refresh_pending
            self._manual_refresh_pending = False
            self._last_run = loop.time()
            try:
                await self.refresh(manual=manual)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- keep the loop alive
                log.warning("attention triage loop error: %s", exc)
