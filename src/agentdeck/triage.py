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

# Card kinds map to CSS (insight-waiting / insight-stalled are warn-coloured,
# insight-finished is the calmer review-blue).
KIND_WAITING = "waiting"  # explicit, structured: the agent is blocked on you
KIND_STALLED = "stalled"  # inferred from the final message or a stall heuristic
KIND_FINISHED = "finished"  # lower-priority: the agent finished and left work for you

# Cards in these kinds are active attention; "finished" sinks below them.
_PRIORITY = {KIND_WAITING: 0, KIND_STALLED: 0, KIND_FINISHED: 1}
_ISSUE_REF_RE = re.compile(r"github\.com/[^/]+/([^/]+)/(?:issues|pull)/(\d+)", re.IGNORECASE)


def card_priority(insight: AssistantInsight) -> int:
    """Lower sorts first: active attention above finished/PR-review."""
    return _PRIORITY.get(insight.kind, 0)


def issue_ref(issue_url: str | None) -> str | None:
    """Short ``repo#number`` for an issue/PR URL, or None."""
    match = _ISSUE_REF_RE.search(issue_url or "")
    return f"{match.group(1)}#{match.group(2)}" if match else None


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
    """LLM judgement of one finished agent's final message.

    ``status`` is one of: ``blocked`` (did not finish, step in now) or ``finished``
    (finished and left its work for you — with or without a PR). Both are attention
    and both produce a card. The classifier never emits ``done`` or ``merged``:
    ``done`` is set only by a human dismissing a card, and ``merged`` is derived
    live from PR status — neither is a classifier output.
    """

    status: str
    summary: str
    reason: str


# Verdict status -> card kind. Every classifier verdict is attention, so both map.
_STATUS_KIND = {"blocked": KIND_STALLED, "finished": KIND_FINISHED}


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
            if pull.is_open and not pull.draft
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
    if session.is_waiting:
        return AssistantInsight(
            key, KIND_WAITING, "Asked you a question", _head(session.question, _MAX_TASK_CHARS)
        )

    # 3. A kanban worker parked its issue as blocked while GitHub still reports it open.
    if (
        session.worker_type == "kanban"
        and session.issue_status_kind == "open"
        and _BLOCKED_ISSUE_RE.search(session.last_text or "")
    ):
        ref = issue_ref(session.issue_url)
        headline = f"{ref} blocked for human action" if ref else "Blocked for human action"
        return AssistantInsight(
            key,
            KIND_WAITING,
            headline,
            "The kanban agent parked this issue with claude:blocked while it is still open. "
            "Review the diagnosis, then close or retrigger it.",
        )

    # 4. Finished-and-resting with an OPEN PR that no one has reviewed. This is
    #    deterministic and re-evaluated every refresh from live PR status, so a
    #    merged or closed PR simply stops producing a card (no stale attention).
    if not session.thinking:
        pull = _open_pull(context)
        if pull is not None:
            title = getattr(pull, "title", "") or ""
            detail = (
                f"{title} — ready for your review."
                if title
                else "The pull request is open and ready for your review."
            )
            return AssistantInsight(
                key, KIND_FINISHED, f"PR #{pull.number} ready for review", detail
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


def all_pulls_terminal(context: GitContext | None) -> bool:
    """True when the session has resolved PRs and every one is merged or closed.

    Such a session's work has shipped — there is nothing left to review, so it
    must not produce a card: not a structured open-PR card (that only fires for
    open PRs) and not an LLM "finished" resurrected from a transcript that still
    says "opened PR #123". A merged PR is done.
    """
    if context is None or not context.pull_requests:
        return False
    return all(pull.is_terminal for pull in context.pull_requests)


def has_merged_pr(context: GitContext | None) -> bool:
    """True when at least one of the session's PRs actually merged.

    Drives the non-attention ``merged`` pill. A PR *closed without merging* does
    not count — nothing shipped, so the session falls back to its finished/done
    status instead.
    """
    if context is None:
        return False
    return any(pull.is_merged for pull in context.pull_requests)


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
    return f"""You triage one coding agent for a human operator. Put it in ONE of two states:

- "blocked": the agent did NOT finish and needs the operator to step in now. It failed, is
  blocked, hit an error it could not resolve, needs a decision before it can proceed, or is
  directly asking a question it is waiting on an answer to. ("I could not do X"; "which should
  I pick?")

- "finished": the agent finished its work and left the result for you — whether or not there
  is a PR. This covers opening or updating a pull request, answering your question, and making
  a local change or completing the task with no follow-up. It may add caveats or ask you to
  sanity-check the result; that is still "finished", not "blocked". The work is done and wants
  your eyes, but the agent is not stuck.

If you cannot tell whether it finished, use "blocked". Do not use tools. Judge only the text.

Return:
- status: "blocked" | "finished"
- summary: one short line stating what the agent did (plain, specific, no preamble)
- reason: one short line on what the operator should do or how to confirm the result

Task the agent was working on:
{task or "(unknown)"}

The agent's final message:
{final_message or "(empty)"}
"""


def parse_verdict(raw: dict) -> Verdict:
    """Coerce the model's JSON into a Verdict, failing open on missing fields."""
    summary = raw.get("summary")
    reason = raw.get("reason")
    # Missing/invalid status fails open to "blocked": never silently drop a handoff.
    status = raw.get("status")
    if status not in {"blocked", "finished"}:
        status = "blocked"
    has_summary = isinstance(summary, str) and bool(summary.strip())
    return Verdict(
        status=status,
        summary=_first_line(summary) if has_summary else "Finished",
        reason=_first_line(reason) if isinstance(reason, str) else "",
    )


def verdict_card(session_key: str, verdict: Verdict) -> AssistantInsight | None:
    """Render a Verdict as an attention card. Both classifier statuses produce one;
    None is only a defensive fallback for an unexpected status."""
    kind = _STATUS_KIND.get(verdict.status)
    if kind is None:
        return None
    detail = verdict.reason or (
        "Finished — ready for your review."
        if verdict.status == "finished"
        else "The agent's final message suggests it needs you."
    )
    return AssistantInsight(session_key, kind, verdict.summary, detail)
