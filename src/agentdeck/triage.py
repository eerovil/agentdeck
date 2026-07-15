"""Per-session "does this agent need me?" triage for Deckhand.

Deckhand answers one question per chat: **does this agent need the operator's
attention right now?** Most of that is decided deterministically here
(structured_trigger / needs_llm); only a finished agent whose final *prose*
might hide an unresolved problem is handed to the LLM classifier, which returns
a Verdict. Everything in this module is pure and side-effect free so it can be
unit-tested without a running service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .git_context import GitContext
from .models import PendingInteraction, Session

# The agent's final message is the whole signal for the LLM path; keep enough to
# judge intent without shipping a whole transcript.
_MAX_MESSAGE_CHARS = 2_000
_MAX_TASK_CHARS = 400
_BLOCKED_ISSUE_RE = re.compile(r"(?<![\w-])claude:blocked(?![\w-])", re.IGNORECASE)

# Card kinds map to CSS (insight-waiting / insight-stalled are warn-coloured).
KIND_WAITING = "waiting"  # explicit, structured: the agent is blocked on you
KIND_STALLED = "stalled"  # inferred from the final message or a stall heuristic


@dataclass(frozen=True)
class AssistantInsight:
    session_key: str
    kind: str
    headline: str
    detail: str


@dataclass(frozen=True)
class AssistantView:
    state: str = "idle"
    summary: str = "Waiting for session activity."
    insights: tuple[AssistantInsight, ...] = ()
    analyzed_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True)
class Verdict:
    """LLM judgement of one finished agent's final message."""

    attention: bool
    summary: str
    reason: str


def tracking_summary(count: int) -> str:
    if count == 0:
        return "Nothing needs your attention right now."
    if count == 1:
        return "1 agent needs your attention."
    return f"{count} agents need your attention."


def _tail(value: str | None, limit: int) -> str:
    return (value or "").strip()[-limit:]


def _head(value: str | None, limit: int) -> str:
    return (value or "").strip()[:limit]


def _first_line(value: str, limit: int = 140) -> str:
    line = value.strip().splitlines()[0] if value.strip() else ""
    return line[:limit].rstrip()


def _open_pull(context: GitContext | None) -> object | None:
    if context is None:
        return None
    return next(
        (
            pull
            for pull in context.pull_requests
            if pull.status == "open" and not pull.draft
        ),
        None,
    )


def structured_trigger(
    session: Session,
    context: GitContext | None,
    interaction: PendingInteraction | None,
    now: datetime,
    *,
    hang_after_s: float,
) -> AssistantInsight | None:
    """Deterministic attention cards — no LLM needed, never a false negative.

    Returns the first matching card (most urgent first) or None when nothing
    structured demands the operator. A None result does not mean "no attention":
    a finished agent may still need the LLM classifier (see needs_llm).
    """
    key = session.key

    # 1. A pending approval/question/MCP prompt is the agent explicitly blocked on you.
    if interaction is not None:
        if interaction.kind in {"approval", "exec"}:
            headline = "Approval needed"
            detail = interaction.command or interaction.message or interaction.title or ""
        elif interaction.kind == "mcp_url":
            headline = "Action needed in your browser"
            detail = interaction.url or interaction.message or interaction.title or ""
        else:
            headline = interaction.title or "Waiting for your answer"
            detail = interaction.message or (
                interaction.questions[0].prompt if interaction.questions else ""
            )
        return AssistantInsight(key, KIND_WAITING, headline, _head(detail, _MAX_TASK_CHARS))

    # 2. The agent ended its last reply with a question directed at you.
    if session.question:
        return AssistantInsight(
            key, KIND_WAITING, "Asked you a question", _head(session.question, _MAX_TASK_CHARS)
        )

    # 3. A kanban worker parked its issue as blocked while GitHub still reports it open.
    if (
        session.worker_type == "kanban"
        and session.issue_status_kind == "open"
        and _BLOCKED_ISSUE_RE.search(session.last_text or "")
    ):
        return AssistantInsight(
            key,
            KIND_WAITING,
            "Blocked for human action",
            "The kanban agent parked this issue with claude:blocked while it is still open. "
            "Review the diagnosis, then close or retrigger it.",
        )

    # 4. Finished-and-resting with an open PR that no one has reviewed.
    if not session.thinking:
        pull = _open_pull(context)
        if pull is not None:
            title = getattr(pull, "title", "") or ""
            detail = (
                f"{title} is open while its chat is idle."
                if title
                else "The PR is open while its chat is idle."
            )
            return AssistantInsight(
                key, KIND_WAITING, f"PR #{pull.number} awaiting review", detail
            )

    # 5. Still "thinking" but the transcript has gone silent — likely hung.
    if session.thinking and session.last_activity is not None:
        idle_s = (now - session.last_activity).total_seconds()
        if idle_s >= hang_after_s:
            minutes = int(idle_s // 60)
            return AssistantInsight(
                key,
                KIND_STALLED,
                f"No progress for {minutes} min",
                "The agent is marked active but has not written to its transcript recently. "
                "It may be hung.",
            )

    return None


def needs_llm(session: Session) -> bool:
    """A finished agent whose final message must be read to judge attention.

    True only for a resting (not-thinking) session where the agent itself spoke
    last and left prose to interpret. Actively-working sessions and sessions
    already caught by a structured trigger never reach the model.
    """
    return (
        not session.thinking
        and session.last_role == "agent"
        and bool((session.last_text or "").strip())
    )


def classification_prompt(session: Session) -> str:
    """One tiny prompt: does THIS agent's final message need the operator?"""
    task = _head(session.initial_prompt or session.title, _MAX_TASK_CHARS)
    final_message = _tail(session.last_text, _MAX_MESSAGE_CHARS)
    return f"""You triage one coding agent for a human operator. Decide the single question:
does this agent need the operator's attention now?

Set attention=true when the agent's final message indicates it stopped without finishing,
failed, is blocked, is unsure, hit an error it could not resolve, is asking the operator to
decide or act, or otherwise handed work back to a human. Set attention=false when it reports
the task done and resolved with nothing left for the operator.

Bias toward attention=true when the message is ambiguous or you are unsure — a missed handoff
is worse than an extra card the operator dismisses. Do not use tools. Judge only the text below.

Return:
- attention: boolean
- summary: one short line stating what the agent did (plain, specific, no preamble)
- reason: one short line on why it needs the operator, or "" when attention is false

Task the agent was working on:
{task or "(unknown)"}

The agent's final message:
{final_message or "(empty)"}
"""


def parse_verdict(raw: dict) -> Verdict:
    """Coerce the model's JSON into a Verdict, failing open on missing fields."""
    summary = raw.get("summary")
    reason = raw.get("reason")
    # Missing/invalid attention fails open to True: never silently drop a handoff.
    attention_value = raw.get("attention")
    attention = True if not isinstance(attention_value, bool) else attention_value
    has_summary = isinstance(summary, str) and bool(summary.strip())
    return Verdict(
        attention=attention,
        summary=_first_line(summary) if has_summary else "Finished",
        reason=_first_line(reason) if isinstance(reason, str) else "",
    )


def verdict_card(session_key: str, verdict: Verdict) -> AssistantInsight:
    """Render an attention Verdict as a card. Only call when verdict.attention."""
    detail = verdict.reason or "The agent's final message suggests it needs you."
    return AssistantInsight(session_key, KIND_STALLED, verdict.summary, detail)
