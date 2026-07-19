from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import PendingInteraction, Session, SessionStatus
from agentdeck.triage import (
    classification_prompt,
    needs_llm,
    parse_verdict,
    structured_trigger,
    tracking_summary,
    verdict_card,
)

_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _session(**overrides) -> Session:
    base = dict(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        show_when_idle=True,
    )
    base.update(overrides)
    return Session(**base)


def _interaction(kind="question", **overrides) -> PendingInteraction:
    base = dict(
        id="i-1",
        kind=kind,
        thread_id="thread-1",
        turn_id="turn-1",
        title="Codex needs your answer",
    )
    base.update(overrides)
    return PendingInteraction(**base)


def _trigger(session, context=None, interaction=None, now=_NOW, hang_after_s=600.0):
    return structured_trigger(session, context, interaction, now, hang_after_s=hang_after_s)


def test_pending_interaction_is_a_waiting_card():
    card = _trigger(_session(), interaction=_interaction(message="Pick one"))
    assert card is not None
    assert card.kind == "waiting"
    assert card.headline == "Codex needs your answer"
    assert "Pick one" in card.detail


def test_approval_interaction_shows_the_command():
    card = _trigger(
        _session(), interaction=_interaction(kind="approval", command="rm -rf build")
    )
    assert card.kind == "waiting"
    assert card.headline == "Approval needed"
    assert "rm -rf build" in card.detail


def test_trailing_question_is_a_waiting_card():
    card = _trigger(_session(question="Which database?"))
    assert card is not None
    assert card.headline == "Asked you a question"
    assert "Which database?" in card.detail


def test_blocked_kanban_issue_names_the_issue():
    session = _session(
        worker_type="kanban",
        issue_status_kind="open",
        issue_url="https://github.com/ScandinavianOutdoor/store/issues/123",
        last_text="Diagnosis done. claude:blocked pending a human decision.",
    )
    card = _trigger(session)
    assert card is not None
    assert card.kind == "waiting"
    assert card.headline == "store#123 blocked for human action"


def test_open_pull_request_on_idle_session_is_a_finished_card():
    context = GitContext(
        repository="eerovil/agentdeck",
        branch="feature/x",
        dirty=False,
        pull_requests=(
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=42,
                title="Add the thing",
                url="https://github.com/eerovil/agentdeck/pull/42",
                status="open",
            ),
        ),
    )
    card = _trigger(_session(), context=context)
    assert card is not None
    assert card.kind == "finished"
    assert card.headline == "PR #42 ready for review"
    assert "Add the thing" in card.detail


def test_all_pulls_terminal_detects_shipped_work():
    from agentdeck.triage import all_pulls_terminal

    merged = GitContext(
        "r", "b", False,
        (PullRequestContext("r", 1, "t", "u", "merged"),
         PullRequestContext("r", 2, "t", "u", "closed")),
    )
    open_too = GitContext(
        "r", "b", False,
        (PullRequestContext("r", 1, "t", "u", "merged"),
         PullRequestContext("r", 2, "t", "u", "open")),
    )
    assert all_pulls_terminal(merged) is True
    assert all_pulls_terminal(open_too) is False  # an open PR still needs review
    assert all_pulls_terminal(GitContext("r", "b", False, ())) is False
    assert all_pulls_terminal(None) is False


def test_merged_or_draft_pull_request_does_not_trigger():
    context = GitContext(
        repository="eerovil/agentdeck",
        branch="feature/x",
        dirty=False,
        pull_requests=(
            PullRequestContext("eerovil/agentdeck", 1, "Merged", "u", "merged"),
            PullRequestContext("eerovil/agentdeck", 2, "Draft", "u", "open", draft=True),
        ),
    )
    assert _trigger(_session(), context=context) is None


def test_silent_thinking_session_is_a_stalled_card():
    session = _session(
        status=SessionStatus.LIVE,
        thinking=True,
        last_activity=_NOW - timedelta(minutes=20),
    )
    card = _trigger(session)
    assert card is not None
    assert card.kind == "stalled"
    assert "No progress" in card.headline


def test_recently_active_thinking_session_does_not_trigger():
    session = _session(
        status=SessionStatus.LIVE,
        thinking=True,
        last_activity=_NOW - timedelta(seconds=30),
    )
    assert _trigger(session) is None


def test_finished_agent_without_structured_signal_has_no_card():
    session = _session(last_role="agent", last_text="All done, tests pass.")
    assert _trigger(session) is None


def test_needs_llm_only_for_resting_agent_with_final_prose():
    assert needs_llm(_session(last_role="agent", last_text="done")) is True
    # actively working -> not classified
    assert needs_llm(_session(thinking=True, last_role="agent", last_text="done")) is False
    # operator spoke last -> nothing to judge yet
    assert needs_llm(_session(last_role="user", last_text="do it")) is False
    # no final message
    assert needs_llm(_session(last_role="agent", last_text="   ")) is False


def test_parse_verdict_fails_open_on_missing_status():
    verdict = parse_verdict({"summary": "Ran the migration", "reason": ""})
    assert verdict.status == "blocked"
    assert verdict.summary == "Ran the migration"


def test_parse_verdict_keeps_first_line_of_summary():
    verdict = parse_verdict(
        {"status": "finished", "summary": "Merged the PR\nextra", "reason": "nope"}
    )
    assert verdict.status == "finished"
    assert verdict.summary == "Merged the PR"  # first line only


def test_parse_verdict_defaults_summary_when_blank():
    verdict = parse_verdict({"status": "blocked", "summary": "", "reason": "stuck"})
    assert verdict.summary == "Finished"


def test_classification_prompt_includes_task_and_final_message():
    session = _session(
        initial_prompt="Port the translator",
        last_text="I could not finish; the API key is missing.",
        last_role="agent",
    )
    prompt = classification_prompt(session)
    assert "Port the translator" in prompt
    assert "the API key is missing" in prompt
    assert "status" in prompt


def test_classification_prompt_offers_two_states():
    prompt = classification_prompt(_session(last_role="agent", last_text="x"))
    # The classifier collapses to blocked/finished; done and merged are not model
    # outputs (done is manual, merged is derived from PR status).
    assert '"blocked"' in prompt
    assert '"finished"' in prompt
    assert '"review"' not in prompt
    assert '"done"' not in prompt
    assert "pull request" in prompt.lower()


def test_card_priority_sinks_finished_below_active():
    from agentdeck.triage import AssistantInsight, card_priority

    assert card_priority(AssistantInsight("k", "waiting", "h", "d")) < card_priority(
        AssistantInsight("k", "finished", "h", "d")
    )
    assert card_priority(AssistantInsight("k", "stalled", "h", "d")) < card_priority(
        AssistantInsight("k", "finished", "h", "d")
    )


def test_issue_ref_parses_issue_and_pull_urls():
    from agentdeck.triage import issue_ref

    assert issue_ref("https://github.com/ScandinavianOutdoor/store/issues/12") == "store#12"
    assert issue_ref("https://github.com/x/tilhi/pull/99") == "tilhi#99"
    assert issue_ref(None) is None
    assert issue_ref("not a url") is None


def test_verdict_card_maps_status_to_kind():
    from agentdeck.triage import Verdict

    blocked = verdict_card("k", Verdict("blocked", "Did the thing", "But failed"))
    assert blocked.kind == "stalled"
    assert blocked.headline == "Did the thing"
    assert blocked.detail == "But failed"

    finished = verdict_card("k", Verdict("finished", "Opened a PR", ""))
    assert finished.kind == "finished"  # finished work still wants your eyes
    assert finished.detail  # a default review nudge is filled in

    # An unexpected status defensively yields no card (classifier only emits the two).
    assert verdict_card("k", Verdict("done", "All good", "")) is None


def test_tracking_summary_phrasing():
    assert tracking_summary(0) == "Nothing needs your attention right now."
    assert tracking_summary(1) == "1 agent needs your attention."
    assert tracking_summary(3) == "3 agents need your attention."
