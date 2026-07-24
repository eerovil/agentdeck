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
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AppConfig, AssistantConfig
from .deckhand import deckhand_account, most_recent_first
from .deckhand_runner import run_codex_json
from .dismissals import Dismissals
from .git_context import GitContext, GitContextResolver
from .models import Account, PendingInteraction, Session
from .providers import pending_interaction_for
from .push import PushService
from .state import AppState
from .triage import (
    AssistantInsight,
    AssistantView,
    DeckhandStatus,
    Verdict,
    card_priority,
    classification_prompt,
    needs_llm,
    parse_verdict,
    resolve_deckhand_status,
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


async def run_codex(account: Account, config: AssistantConfig, prompt: str) -> dict[str, Any]:
    """Run one read-only, ephemeral Codex classification and return its JSON result."""
    return await run_codex_json(
        account,
        config,
        prompt,
        schema_path=_SCHEMA_PATH,
        temp_prefix="agentdeck-assistant-",
        job_name="assistant",
    )


class AssistantService:
    """Debounce session changes into per-session attention triage."""

    HANG_AFTER_S = _HANG_AFTER_S
    # Cap concurrent LLM classifications: each spawns a codex subprocess, and the
    # triage window (``max_sessions``) can queue many on a cold cache. Bound the
    # fan-out so the window size doesn't equal the concurrent load on the account.
    CLASSIFY_CONCURRENCY = 8

    def __init__(
        self,
        config: AppConfig,
        state: AppState,
        *,
        runner: Runner = run_codex,
        context_resolver: GitContextResolver | None = None,
        push: PushService | None = None,
    ) -> None:
        self.config = config.assistant
        self.state = state
        self.accounts = config.build_accounts()
        self.push = push
        # Fire-and-forget push sends (issue #7/#13); kept referenced until done.
        self._push_tasks: set[asyncio.Task] = set()
        self._pending_pushes: dict[tuple[str, str], asyncio.Task] = {}
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
        self._known_working_session_keys: set[str] = set()
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
        # Operator dismissals (insight + question-waiting) and their persistence,
        # behind one store — see dismissals.py.
        self.dismissals = Dismissals.load(state.db)

    # --- persistence ---------------------------------------------------

    def _restore_checkpoint(self, payload: dict[str, Any] | None) -> None:
        # v6 retired the "review"/"done" verdict vocabulary (now blocked/finished);
        # an older checkpoint is simply dropped and reclassified on the next tick.
        if not payload or payload.get("version") != 6:
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
                    Verdict(str(entry[1]), str(entry[2]), str(entry[3])),
                )
                for k, entry in payload.get("verdicts", {}).items()
                if isinstance(entry, list) and len(entry) == 4
            }
            self._force = False
        except (KeyError, TypeError, ValueError):
            log.warning("Ignoring invalid Deckhand checkpoint")

    def _checkpoint_payload(self) -> dict[str, Any]:
        checkpoint_insights = tuple(
            insight
            for insight in self.view.insights
            if (insight.session_key, insight.headline) not in self._pending_pushes
        )
        return {
            "version": 6,
            "view": {
                "summary": (
                    self.view.summary
                    if len(checkpoint_insights) == len(self.view.insights)
                    else tracking_summary(len(checkpoint_insights))
                ),
                "insights": [
                    {
                        "session_key": insight.session_key,
                        "kind": insight.kind,
                        "headline": insight.headline,
                        "detail": insight.detail,
                    }
                    for insight in checkpoint_insights
                ],
                "analyzed_at": (
                    self.view.analyzed_at.isoformat() if self.view.analyzed_at else None
                ),
            },
            "signatures": self._signatures,
            "verdicts": {
                key: [sig, verdict.status, verdict.summary, verdict.reason]
                for key, (sig, verdict) in self._verdicts.items()
            },
        }

    def _save_checkpoint(self) -> None:
        if self.state.db and self.view.state == "ready":
            self.state.db.record_assistant_checkpoint(self._checkpoint_payload())

    # --- lifecycle -----------------------------------------------------

    def _account(self) -> Account | None:
        return deckhand_account(self.accounts, self.config.account_key)

    async def start(self) -> None:
        if self.config.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="attention-triage")
            self._session_watch_task = asyncio.create_task(
                self._watch_sessions(), name="attention-triage-sessions"
            )

    async def stop(self) -> None:
        # Include in-flight push sends so they don't outlive the event loop / DB.
        tasks = tuple(
            t
            for t in (self._task, self._session_watch_task, *self._push_tasks)
            if t is not None
        )
        self._task = None
        self._session_watch_task = None
        self._push_tasks.clear()
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
        return pending_interaction_for(session, self.accounts)

    def _triage_sessions(self) -> list[Session]:
        """Blocking chats first, then most-recently-active, capped to max_sessions."""
        visible = self._eligible_sessions()
        blocking = [s for s in visible if s.question or self._interaction(s) is not None]
        blocking_keys = {s.key for s in blocking}
        remaining = [s for s in visible if s.key not in blocking_keys]
        remaining.sort(key=most_recent_first)
        selected = blocking + remaining[: max(0, self.config.max_sessions - len(blocking))]
        self.analysis_session_count = len(selected)
        self.total_session_count = len(visible)
        return selected

    def _eligible_sessions(self) -> list[Session]:
        """Chats with their own operator-facing Deckhand handoff.

        A delegated child reports through its visible parent. A machine-started
        chat with no visible parent is itself top-level, though, so suppressing
        it would leave its completed handoff with nowhere to appear.
        """
        presentation = self.state.session_presentation()
        top_level_keys = {session.key for session in presentation.top_level}
        return [
            session
            for session in presentation.visible
            if not session.is_delegated or session.key in top_level_keys
        ]

    @staticmethod
    def _message_signature(session: Session) -> str:
        """Identity of the conversation's latest turn — question and last
        messages only. Changes exactly when a NEW message arrives, and is immune
        to the evidence-signature churn (git/PR context re-resolution, poll
        noise) that would otherwise revert a question-waiting dismissal every
        refresh. This is the revert key for ``_waiting_done``."""
        return json.dumps(
            [
                session.question,
                session.last_role,
                _trim(session.last_prompt),
                _trim(session.last_text),
            ],
            default=str,
            separators=(",", ":"),
        )

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
        *,
        previous_signature: str | None = None,
    ) -> str:
        """Stable identity of a session's material state (excludes transient liveness).

        Drives the classifier cache, handled-card validity, and change detection.
        Thinking/activity/subagent churn is deliberately excluded so cards neither
        flicker nor get reclassified on poll noise.
        """
        prs = (
            sorted((p.number, p.status, p.draft) for p in context.pull_requests)
            if context is not None
            else []
        )
        if context is not None and not context.pulls_complete and previous_signature:
            try:
                previous_prs = json.loads(previous_signature).get("prs")
                if isinstance(previous_prs, list):
                    prs = previous_prs
            except (AttributeError, TypeError, ValueError):
                pass
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
            "prs": prs,
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
            return Verdict("blocked", summary, reason), False

    async def refresh(self, *, manual: bool = False) -> None:
        sessions = self._triage_sessions()
        resolved = await self.context_resolver.resolve(sessions)
        eligible_keys = {session.key for session in self._eligible_sessions()}
        delegated_handled = set(self.dismissals.insight_keys()) - eligible_keys
        for session_key in delegated_handled & self.state.delegated_session_keys:
            self.dismissals.drop_insight(session_key)
        self.contexts = {k: v for k, v in self.contexts.items() if k in eligible_keys}
        for key, context in resolved.items():
            # A failed GitHub lookup is not evidence that a PR disappeared. Keep
            # the last authoritative context in a warm process; after a restart,
            # the restored signature below protects persisted dismissals until a
            # complete lookup succeeds.
            if context.pulls_complete or key not in self.contexts:
                self.contexts[key] = context
        self._verdicts = {k: v for k, v in self._verdicts.items() if k in eligible_keys}

        now = datetime.now(UTC)
        account = self._account()

        cards: list[AssistantInsight] = []
        signatures: dict[str, str] = {}
        pending: list[tuple[Session, str]] = []

        for session in sessions:
            context = self.contexts.get(session.key)
            interaction = self._interaction(session)
            signature = self._evidence_signature(
                session,
                context,
                interaction,
                previous_signature=(
                    self._signatures.get(session.key)
                    or self.dismissals.insight_signature(session.key)
                ),
            )
            signatures[session.key] = signature

            card = structured_trigger(
                session, context, interaction, now, hang_after_s=self.HANG_AFTER_S
            )
            if card is not None:
                cards.append(card)
                continue
            if context is not None and context.is_shipped:
                # The agent's PR(s) merged/closed — terminal, nothing to review.
                continue
            if not needs_llm(session):
                continue
            cached = self._verdicts.get(session.key)
            if cached is not None and cached[0] == signature:
                card = verdict_card(session.key, cached[1])
                if card is not None:
                    cards.append(card)
            elif account is not None:
                pending.append((session, signature))

        if not signatures and not self.view.insights:
            self._commit_view(AssistantView(state="ready"), signatures, manual=manual)
            return

        degraded = False
        if pending:
            self.view = replace(self.view, state="analyzing")
            self.state.assistant_changed()
            gate = asyncio.Semaphore(self.CLASSIFY_CONCURRENCY)

            async def _classify_bounded(session):
                async with gate:
                    return await self._classify(account, session)

            results = await asyncio.gather(
                *(_classify_bounded(session) for session, _ in pending)
            )
            for (session, signature), (verdict, ok) in zip(pending, results, strict=True):
                if ok:
                    self._verdicts[session.key] = (signature, verdict)
                else:
                    degraded = True
                    cached = self._verdicts.get(session.key)
                    verdict = cached[1] if cached is not None else verdict
                card = verdict_card(session.key, verdict)
                if card is not None:
                    cards.append(card)

        # Drop dismissals whose session identity moved (insight → evidence
        # signature; waiting → message signature / question gone).
        self.dismissals.prune_stale(
            signatures, self.state.sessions.get, self._message_signature
        )
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
            # A question-waiting dismissal hides the re-created waiting card too,
            # so the next refresh can't resurface it (stale entries were already
            # pruned above, so anything left here is still valid).
            if self.dismissals.is_waiting_dismissed(card.session_key):
                continue
            handled_sig = self.dismissals.insight_signature(card.session_key)
            current = signatures.get(card.session_key)
            if handled_sig is not None and handled_sig == current:
                self.dismissals.refresh_insight(card.session_key, card)
                continue
            if handled_sig is not None:
                self.dismissals.drop_insight(card.session_key)
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
        self._notify_new_insights(self.view, view)
        self.view = view
        self._save_checkpoint()
        if manual:
            self.refresh_status = (
                "Updated" if not view.error else "Some agents could not be read"
            )
        if changed or manual:
            self.state.assistant_changed()

    def _notify_new_insights(self, old: AssistantView, new: AssistantView) -> None:
        """Web-push each attention item that just appeared (issue #7/#13).

        Identity is (session_key, headline): a card that changes what it says
        (e.g. "ready for review" → "blocked") notifies again, while an unchanged
        card that survives a refresh does not. Handled cards never reach the view,
        so acknowledging one silences it. On restart the checkpoint restores the
        prior insights, so nothing re-notifies. No-op unless push is enabled."""
        if not (self.push and self.push.enabled):
            return
        seen = {(i.session_key, i.headline) for i in old.insights}
        current = {(i.session_key, i.headline) for i in new.insights}
        for identity, task in tuple(self._pending_pushes.items()):
            if identity not in current:
                del self._pending_pushes[identity]
                task.cancel()
        for insight in new.insights:
            if (insight.session_key, insight.headline) in seen:
                continue
            self._dispatch_push(insight)

    def _push_needs_settle(self, insight: AssistantInsight) -> bool:
        """Whether a resting handoff could still gain newly scanned child work."""
        session = self.state.sessions.get(insight.session_key)
        return bool(
            session is not None
            and not session.thinking
            and not session.stalled
            and insight.kind != "waiting"
        )

    def _push_is_current_and_resting(self, insight: AssistantInsight) -> bool:
        identity = (insight.session_key, insight.headline)
        if identity not in {
            (current.session_key, current.headline) for current in self.view.insights
        }:
            return False
        session = self.state.sessions.get(insight.session_key)
        if session is None:
            return False
        return not self.state.session_presentation().display(session).thinking

    async def _wait_for_session_scan(self, account_key: str, revision: int) -> None:
        """Wait for a successful full scan after ``revision`` without a race."""
        with self.state.bus.subscribe("session_scans") as subscription:
            while self.state.session_scan_revision(account_key) <= revision:
                await subscription.get()

    def _dispatch_push(self, insight: AssistantInsight) -> None:
        # send_to_all is blocking (HTTP to each push service), so run it off the
        # event loop and don't await — a slow push service can't stall triage.
        identity = (insight.session_key, insight.headline)
        settle = self._push_needs_settle(insight)
        if settle and identity in self._pending_pushes:
            return
        session = self.state.sessions.get(insight.session_key)
        if settle:
            assert session is not None
        scan_revision = (
            self.state.session_scan_revision(session.account_key)
            if settle
            else 0
        )

        async def send() -> bool:
            if settle:
                await self._wait_for_session_scan(session.account_key, scan_revision)
                if not self._push_is_current_and_resting(insight):
                    return False
            await asyncio.to_thread(
                self.push.send_to_all,
                insight.headline,
                insight.detail or "",
                f"/sessions/{insight.session_key}",
            )
            return True

        try:
            task = asyncio.create_task(
                send(),
                name=f"push:{insight.session_key}",
            )
        except RuntimeError:  # no running loop (e.g. a synchronous unit test)
            return
        self._push_tasks.add(task)
        if settle:
            self._pending_pushes[identity] = task

        def done(completed: asyncio.Task) -> None:
            self._push_tasks.discard(completed)
            if self._pending_pushes.get(identity) is completed:
                del self._pending_pushes[identity]
            if completed.cancelled():
                return
            try:
                sent = completed.result()
            except Exception as exc:  # noqa: BLE001 -- background push failure is non-fatal
                log.warning("Deckhand push failed for %s: %s", insight.session_key, exc)
                return
            if sent:
                # Pending cards are excluded from checkpoints so a web restart
                # retries the scan barrier instead of losing the notification.
                self._save_checkpoint()

        task.add_done_callback(done)

    # --- handled / undo ------------------------------------------------

    def handle(self, session_key: str) -> bool:
        session = self.state.sessions.get(session_key)
        # A chat waiting on your answer (a pending question) reverts on a NEW
        # message, so key its dismissal on the message signature — robust to the
        # evidence-signature churn (git/PR context re-resolution, poll noise) that
        # would otherwise revert it every refresh. This holds whether or not
        # Deckhand also raised a waiting card; drop that card so the panel clears
        # (and _apply_handled keeps the next refresh from resurfacing it).
        if session is not None and session.is_waiting:
            self.dismissals.dismiss_waiting(
                session_key, self._message_signature(session), session.question
            )
            insights = tuple(
                i for i in self.view.insights if i.session_key != session_key
            )
            if len(insights) != len(self.view.insights):
                self.view = replace(
                    self.view, summary=tracking_summary(len(insights)), insights=insights
                )
            self._save_checkpoint()
            self.state.assistant_changed()
            return True
        # A non-question Deckhand card (blocked/finished): dismiss on the evidence
        # signature, auto-restored when the session's evidence changes.
        insight = next(
            (i for i in self.view.insights if i.session_key == session_key), None
        )
        signature = self._signatures.get(session_key)
        if insight is None or signature is None:
            return False
        self.dismissals.dismiss_insight(session_key, signature, insight)
        insights = tuple(i for i in self.view.insights if i.session_key != session_key)
        self.view = replace(
            self.view, summary=tracking_summary(len(insights)), insights=insights
        )
        self._save_checkpoint()
        self.state.assistant_changed()
        return True

    def unhandle(self, session_key: str) -> bool:
        if not self.dismissals.is_dismissed(session_key):
            return False
        dismissal = self.dismissals.restore(session_key)
        if dismissal is not None:  # an insight dismissal (waiting carries none)
            insight = dismissal.insight
            # Restore the card immediately when its evidence is still current, so
            # the undo is visible without waiting for the next triage tick.
            current = self._signatures.get(session_key)
            already_shown = any(i.session_key == session_key for i in self.view.insights)
            if insight is not None and current == dismissal.signature and not already_shown:
                insights = self.view.insights + (insight,)
                self.view = replace(
                    self.view, summary=tracking_summary(len(insights)), insights=insights
                )
            self._save_checkpoint()
        self.request_refresh()
        self.state.assistant_changed()
        return True

    @property
    def handled_items(self) -> tuple[AssistantHandledItem, ...]:
        """Most recent handled card; older entries stay persisted as an undo stack."""
        latest = self.dismissals.latest_insight()
        if latest is None:
            return ()
        session_key, insight = latest
        session = self.state.sessions.get(session_key)
        headline = (
            insight.headline
            if insight is not None
            else (session.display_title if session else "Handled item")
        )
        return (AssistantHandledItem(session_key, headline),)

    def handled_insight(self, session_key: str) -> AssistantInsight | None:
        return self.dismissals.insight(session_key)

    def is_handled(self, session_key: str) -> bool:
        """Whether ``session_key`` currently reads as dismissed (signature still
        matches). A convenience for the done/undo control."""
        return session_key in self._handled_keys()

    def _handled_keys(self) -> frozenset[str]:
        """Sessions currently dismissed by the operator — they render a ``done``
        pill. Validity is re-checked here (before the periodic prune even runs)
        against current identities, so a stale dismissal auto-reverts."""
        return self.dismissals.active_keys(
            self._signatures, self.state.sessions.get, self._message_signature
        )

    def _session_verdicts(self) -> dict[str, Verdict]:
        """Durable per-session classifier verdict (blocked/finished), independent
        of the transient attention view. Unlike ``view.insights`` these survive a
        run that produces no cards, so a per-session status pill stays stable."""
        return {key: verdict for key, (_sig, verdict) in self._verdicts.items()}

    def deckhand_statuses(
        self, sessions: Iterable[Session]
    ) -> dict[str, DeckhandStatus]:
        """One resolved Deckhand Status per given session; absent key = no pill.

        Gathers the four status sources once — durable verdicts, operator
        dismissals, merged-PR state, and the live attention view — and resolves
        the final pill through ``triage.resolve_deckhand_status``, the single home
        for the precedence the web layer used to reconstruct from these internals.
        """
        verdicts = self._session_verdicts()
        handled = self._handled_keys()
        live_by_key = {insight.session_key: insight for insight in self.view.insights}
        eligible_keys = {session.key for session in self._eligible_sessions()}
        statuses: dict[str, DeckhandStatus] = {}
        for session in sessions:
            key = session.key
            handled_insight = self.handled_insight(key) if key in handled else None
            context = self.contexts.get(key)
            status = resolve_deckhand_status(
                session,
                eligible=key in eligible_keys,
                verdict=verdicts.get(key),
                dismissed=key in handled,
                dismissed_headline=(
                    handled_insight.headline if handled_insight is not None else None
                ),
                merged=context is not None and context.has_merged_pr,
                live_insight=live_by_key.get(key),
            )
            if status is not None:
                statuses[key] = status
        return statuses

    # --- loop ----------------------------------------------------------

    def _carded_session_resumed(self, eligible: list[Session]) -> bool:
        """A session currently backing a displayed card has resumed working, so
        its finished/attention card should be recomputed (dropped) now rather
        than lingering until the periodic refresh (issue #15). ``thinking`` is
        excluded from the evidence signature — to avoid reclassification churn —
        so this transition is otherwise invisible to change detection."""
        carded = {insight.session_key for insight in self.view.insights}
        return any(session.key in carded and session.thinking for session in eligible)

    def _working_session_finished(self, eligible: list[Session]) -> bool:
        """An active turn just became resting, so triage its handoff now.

        Session events wake the loop, but the regular refresh interval otherwise
        throttles them. Track only the stable working/not-working edge here so a
        completed turn can surface attention immediately without making ordinary
        activity updates trigger repeated classification.
        """
        working = {session.key for session in eligible if session.thinking}
        finished = bool(self._known_working_session_keys - working)
        self._known_working_session_keys = working
        return finished

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            except TimeoutError:
                pass
            self._wake.clear()
            due = loop.time() - self._last_run >= self.config.refresh_interval_s
            eligible = self._eligible_sessions()
            visible_keys = {session.key for session in eligible}
            newly_visible = bool(visible_keys - self._known_visible_session_keys)
            self._known_visible_session_keys = visible_keys
            resumed_working = self._carded_session_resumed(eligible)
            finished_working = self._working_session_finished(eligible)
            if (
                not self._force
                and not due
                and not newly_visible
                and not resumed_working
                and not finished_working
            ):
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
