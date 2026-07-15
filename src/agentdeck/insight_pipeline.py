"""Pure insight post-processing for Deckhand.

These functions take explicit git/session context instead of reaching into
``AssistantService``, so the ``suppress -> surface -> enrich -> deduplicate``
spine can be unit-tested in isolation. ``AssistantService._normalize_insights``
composes them in one authoritative order; nothing here touches persisted
handled-state, evidence signatures, or providers — that stays in the service.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from .git_context import GitContext
from .models import PendingInteraction, Session

log = logging.getLogger(__name__)

_INSIGHT_PR_SEQUENCE_RE = re.compile(
    r"\b(?:PR|PRs|pull request|pull requests)\s+"
    r"((?:#?\d+)(?:\s*(?:,|and|&)\s*#?\d+)*)",
    re.IGNORECASE,
)
_INSIGHT_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)
_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/[^/]+/([^/]+)/issues/(\d+)(?:[/?#]|$)",
    re.IGNORECASE,
)
_VALIDATED_COMPLETION_RE = re.compile(
    r"\b(?:\d+\s+(?:\*{0,2})?passed|tests?\s+(?:all\s+)?pass(?:ed|ing))\b",
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

InteractionLookup = Callable[[Session], PendingInteraction | None]


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
class AssistantView:
    state: str = "idle"
    summary: str = "Waiting for session activity."
    insights: tuple[AssistantInsight, ...] = ()
    actions: tuple[AssistantAction, ...] = ()
    analyzed_at: datetime | None = None
    error: str | None = None


def tracking_summary(count: int) -> str:
    if count == 0:
        return "Nothing needs your attention right now."
    if count == 1:
        return "Deckhand is tracking 1 item that still needs attention."
    return f"Deckhand is tracking {count} items that still need attention."


def pr_claims(insight: AssistantInsight) -> tuple[set[int], set[tuple[str, int]]]:
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


def suppress_terminal_pr_insights(
    view: AssistantView, contexts: Mapping[str, GitContext]
) -> AssistantView:
    """Suppress only advice whose claimed PR work is entirely terminal."""
    insights = []
    for insight in view.insights:
        numbers, repositories = pr_claims(insight)
        if not numbers and not repositories:
            insights.append(insight)
            continue
        context = contexts.get(insight.session_key)
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
    return replace(
        view,
        summary=(view.summary if insights else "Nothing needs your attention right now."),
        insights=insights,
    )


def suppress_unattributed_pr_insights(
    view: AssistantView, contexts: Mapping[str, GitContext]
) -> AssistantView:
    """Reject PR claims copied from another chat in the shared snapshot."""
    insights = []
    for insight in view.insights:
        numbers, repositories = pr_claims(insight)
        if not numbers and not repositories:
            insights.append(insight)
            continue
        context = contexts.get(insight.session_key)
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
    return replace(view, summary=tracking_summary(len(result)), insights=result)


def suppress_non_actionable_insights(
    view: AssistantView,
    sessions: Mapping[str, Session],
    interaction: InteractionLookup,
) -> AssistantView:
    """Active progress alone is not an item that needs the operator's attention."""
    insights = tuple(
        insight
        for insight in view.insights
        if not (
            insight.kind in {"info", "waiting", "stalled"}
            and (session := sessions.get(insight.session_key)) is not None
            and session.thinking
            and interaction(session) is None
            and not session.question
        )
    )
    if insights == view.insights:
        return view
    return replace(view, summary=tracking_summary(len(insights)), insights=insights)


def surface_open_blocked_issues(
    view: AssistantView, snapshot: list[dict[str, Any]]
) -> AssistantView:
    """Guarantee one understandable action card per open terminal issue.

    This state is supplied authoritatively by the kanban collector. Model
    advice can add judgment elsewhere, but cannot silently omit an issue
    whose terminal worker explicitly parked it for human intervention.
    """
    blocked = {
        str(row["session_key"]): row
        for row in snapshot
        if row.get("operator_action_required") == "open_issue_blocked"
    }
    if not blocked:
        return view

    insights = [
        insight for insight in view.insights if insight.session_key not in blocked
    ]
    for session_key, row in blocked.items():
        match = _ISSUE_URL_RE.match(str(row.get("issue_url") or ""))
        if match:
            repository, number = match.groups()
            project = _PROJECT_DISPLAY_NAMES.get(
                repository.casefold(), repository.replace("-", " ").title()
            )
            subject = f"{project} issue #{number}"
        else:
            subject = str(row.get("title") or "Open issue")
        insights.append(
            AssistantInsight(
                session_key=session_key,
                kind="waiting",
                headline=f"{subject} is blocked for human action",
                detail=(
                    "The terminal kanban agent parked this issue with "
                    "claude:blocked while GitHub still reports it open. "
                    "Review the diagnosis, then close or retrigger the issue."
                ),
                confidence=1.0,
            )
        )
    return replace(view, insights=tuple(insights))


def surface_unreported_open_pull_requests(
    view: AssistantView, snapshot: list[dict[str, Any]]
) -> AssistantView:
    """Keep a validated but still-open PR visible as the chat's next action."""
    represented_sessions = {insight.session_key for insight in view.insights}
    insights = list(view.insights)
    for row in snapshot:
        session_key = str(row.get("session_key") or "")
        if (
            not session_key
            or session_key in represented_sessions
            or row.get("state") != "idle"
            or row.get("question")
            or row.get("interaction")
            or not _VALIDATED_COMPLETION_RE.search(str(row.get("last_response") or ""))
        ):
            continue
        git = row.get("git")
        if not isinstance(git, dict):
            continue
        pull = next(
            (
                item
                for item in git.get("pull_requests") or ()
                if isinstance(item, dict)
                and item.get("status") == "open"
                and not item.get("draft")
                and isinstance(item.get("number"), int)
            ),
            None,
        )
        if pull is None:
            continue
        title = pull.get("title")
        detail = (
            f"{title} is still open while its owning chat is idle. Review or "
            "otherwise resolve the pull request."
            if isinstance(title, str) and title
            else "The pull request is still open while its owning chat is idle. "
            "Review or otherwise resolve it."
        )
        insights.append(
            AssistantInsight(
                session_key=session_key,
                kind="waiting",
                headline=f"PR #{pull['number']} is open and awaiting review",
                detail=detail,
                confidence=1.0,
            )
        )
        represented_sessions.add(session_key)
    return replace(view, insights=tuple(insights))


def pr_feature_word(title: str, project: str) -> str:
    for word in _PR_TITLE_WORD_RE.findall(title):
        if (
            not word.isdigit()
            and word.casefold() not in _GENERIC_PR_TITLE_WORDS
            and word.casefold() != project.casefold()
        ):
            return word
    return "Change"


def enrich_pr_headlines(
    view: AssistantView, contexts: Mapping[str, GitContext]
) -> AssistantView:
    """Prefix PR advice with its project and a compact title-derived feature word."""
    insights = []
    for insight in view.insights:
        context = contexts.get(insight.session_key)
        numbers, repositories = pr_claims(insight)
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
        feature = pr_feature_word(pull.title, project)
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


def deduplicate_insights(
    view: AssistantView,
    contexts: Mapping[str, GitContext],
    sessions: Mapping[str, Session],
) -> AssistantView:
    """Keep one operator action per underlying PR, issue, branch, or model group."""
    seen: set[str] = set()
    insights = []
    for insight in view.insights:
        context = contexts.get(insight.session_key)
        numbers, repositories = pr_claims(insight)
        pulls = context.pull_requests if context is not None else ()
        claimed = sorted(
            f"{pull.repository.casefold()}#{pull.number}"
            for pull in pulls
            if pull.number in numbers
            or (pull.repository.casefold(), pull.number) in repositories
        )
        session = sessions.get(insight.session_key)
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
    return replace(view, summary=tracking_summary(len(result)), insights=result)
