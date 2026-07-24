from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from agentdeck.assistant import AssistantInsight, AssistantService, AssistantView, run_codex
from agentdeck.config import AccountConfig, AppConfig, AssistantConfig
from agentdeck.db import Db
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import Capability, PendingInteraction, Session, SessionStatus
from agentdeck.providers import PROVIDERS
from agentdeck.state import AppState

_ATTENTION = {"status": "blocked", "summary": "Stopped early", "reason": "Needs a decision"}
_FINISHED = {"status": "finished", "summary": "Opened a PR", "reason": "Review it"}
_FINISHED_NO_PR = {"status": "finished", "summary": "All tests pass", "reason": ""}


class _StubResolver:
    """Resolves fixed git/PR context (default: none), so tests stay hermetic."""

    def __init__(self, contexts=None):
        self._contexts = contexts or {}
        self.forces = []

    async def resolve(self, sessions, *, force=False):
        self.forces.append(force)
        return dict(self._contexts)


def _config(tmp_path, **assistant):
    return AppConfig(
        assistant=AssistantConfig(enabled=True, **assistant),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )


def _service(tmp_path, runner, *, state=None):
    return AssistantService(
        _config(tmp_path),
        state or AppState(),
        runner=runner,
        context_resolver=_StubResolver(),
    )


def _finished(tmp_path, **overrides):
    """A resting agent whose final prose must be classified."""
    base = dict(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        cwd=tmp_path,
        title="Build the prototype",
        initial_prompt="Build the local prototype",
        last_text="I finished the prototype.",
        last_role="agent",
        show_when_idle=True,
    )
    base.update(overrides)
    return Session(**base)


async def test_structured_question_card_skips_the_model(tmp_path):
    runner = AsyncMock()
    state = AppState()
    state.update_session(_finished(tmp_path, question="Which database?"))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    assert [i.headline for i in assistant.view.insights] == ["Asked you a question"]
    runner.assert_not_awaited()


async def test_manual_refresh_bypasses_github_metadata_ttl(tmp_path):
    resolver = _StubResolver()
    assistant = AssistantService(
        _config(tmp_path),
        AppState(),
        runner=AsyncMock(),
        context_resolver=resolver,
    )

    await assistant.refresh(manual=True)

    assert resolver.forces == [True]


async def test_finished_agent_flagged_by_the_model_becomes_a_card(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    assert runner.await_count == 1
    (insight,) = assistant.view.insights
    assert insight.kind == "stalled"
    assert insight.headline == "Stopped early"
    assert insight.detail == "Needs a decision"
    assert assistant.view.summary == "1 agent needs your attention."


async def test_finished_agent_without_a_pr_still_shows_a_finished_card(tmp_path):
    # New model: a finished agent always surfaces as attention (there is no silent
    # model "done" anymore), even when it opened no PR — the operator clears it.
    runner = AsyncMock(return_value=_FINISHED_NO_PR)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    (insight,) = assistant.view.insights
    assert insight.kind == "finished"
    assert insight.headline == "All tests pass"
    assert assistant.view.summary == "1 agent needs your attention."


async def test_delegated_agents_are_not_triaged_or_counted(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    state = AppState()
    parent = _finished(tmp_path, key="codex:test:operator")
    state.update_session(parent)
    state.update_session(
        _finished(
            tmp_path,
            key="codex:test:subagent",
            session_id="subagent",
            is_delegated=True,
            last_text="Investigated the issue but made no changes.",
        )
    )
    state.recorded_delegation_parents["codex:test:subagent"] = parent.session_id
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    assert runner.await_count == 1
    assert [insight.session_key for insight in assistant.view.insights] == [
        "codex:test:operator"
    ]
    assert assistant.analysis_session_count == 1
    assert assistant.total_session_count == 1
    child = state.sessions["codex:test:subagent"]
    assert child.key not in assistant.deckhand_statuses([child])


async def test_finished_top_level_delegation_gets_attention_and_status(tmp_path):
    # Issue #134: a machine-started session with no visible parent is itself the
    # operator's top-level chat. Once resting, it must not disappear merely
    # because its durable provenance still says it was delegated.
    runner = AsyncMock(return_value=_FINISHED_NO_PR)
    state = AppState()
    session = _finished(tmp_path, is_delegated=True)
    state.update_session(session)
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    runner.assert_awaited_once()
    assert [insight.session_key for insight in assistant.view.insights] == [session.key]
    assert assistant.deckhand_statuses([session])[session.key].state == "finished"


async def test_delegated_agent_is_removed_from_old_handled_state(tmp_path):
    state = AppState()
    parent = _finished(
        tmp_path, key="codex:test:operator", session_id="operator", thinking=True
    )
    session = _finished(tmp_path, is_delegated=True)
    state.update_session(parent)
    state.update_session(session)
    state.recorded_delegation_parents[session.key] = parent.session_id
    assistant = _service(tmp_path, AsyncMock(), state=state)
    assistant.dismissals.dismiss_insight(
        session.key,
        "old-evidence",
        AssistantInsight(session.key, "stalled", "Old delegated card", "Old detail"),
    )

    await assistant.refresh()

    assert assistant.handled_items == ()
    assert not assistant.dismissals.is_dismissed(session.key)


async def test_finished_agent_with_pr_review_shows_a_finished_card(tmp_path):
    runner = AsyncMock(return_value=_FINISHED)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    (insight,) = assistant.view.insights
    assert insight.kind == "finished"  # finished work surfaces as a review card
    assert insight.headline == "Opened a PR"


def test_carded_session_resuming_work_triggers_a_refresh(tmp_path):
    # Issue #15: the loop's change detection must notice a carded session that
    # resumed working (thinking is excluded from the evidence signature, so it's
    # otherwise invisible) — otherwise the finished card lingers a full interval.
    assistant = _service(tmp_path, AsyncMock(), state=AppState())
    assistant.view = AssistantView(
        state="ready",
        insights=(AssistantInsight("codex:test:thread-1", "finished", "Opened a PR", "d"),),
    )
    resting = _finished(tmp_path)
    working = _finished(tmp_path, status=SessionStatus.LIVE, thinking=True)
    assert assistant._carded_session_resumed([resting]) is False
    assert assistant._carded_session_resumed([working]) is True
    # a working session with no card of its own → nothing to drop, no refresh
    assistant.view = AssistantView(state="ready", insights=())
    assert assistant._carded_session_resumed([working]) is False


def test_working_session_finishing_triggers_one_refresh(tmp_path):
    # Issue #134: session events wake the loop, but a completed active turn must
    # bypass its periodic throttle so Deckhand can surface the finished handoff.
    assistant = _service(tmp_path, AsyncMock(), state=AppState())
    working = _finished(tmp_path, status=SessionStatus.LIVE, thinking=True)
    resting = _finished(tmp_path)

    assert assistant._working_session_finished([working]) is False
    assert assistant._working_session_finished([resting]) is True
    assert assistant._working_session_finished([resting]) is False


async def test_subagent_discovery_cancels_provisional_parent_handoff(tmp_path):
    runner = AsyncMock(return_value=_FINISHED)
    push = _FakePush()
    state = AppState()
    parent = _finished(tmp_path, status=SessionStatus.LIVE, thinking=True)
    state.update_session(parent)
    assistant = AssistantService(
        _config(tmp_path),
        state,
        runner=runner,
        context_resolver=_StubResolver(),
        push=push,
    )

    assert assistant._working_session_finished(assistant._eligible_sessions()) is False
    state.update_session(replace(parent, status=SessionStatus.IDLE, thinking=False))
    assert assistant._working_session_finished(assistant._eligible_sessions()) is True

    # The card is reversible and can render immediately, but push waits for the
    # next collection cycle to rule out newly launched child work.
    await assistant.refresh()
    assert len(assistant.view.insights) == 1
    runner.assert_awaited_once()

    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key=parent.key,
        is_delegated=True,
    )
    state.update_session(child)
    state.sessions_scanned(parent.account_key)
    await _drain(assistant)

    assert push.sent == []


async def test_settled_parent_handoff_pushes_once(tmp_path):
    runner = AsyncMock(return_value=_FINISHED)
    push = _FakePush()
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = AssistantService(
        _config(tmp_path),
        state,
        runner=runner,
        context_resolver=_StubResolver(),
        push=push,
    )

    await assistant.refresh()
    await asyncio.sleep(0)
    assert push.sent == []
    assert assistant._checkpoint_payload()["view"]["insights"] == []

    state.sessions_scanned("codex:test")
    await _drain(assistant)

    assert push.sent == [("Opened a PR", "Review it", "/sessions/codex:test:thread-1")]
    assert len(assistant._checkpoint_payload()["view"]["insights"]) == 1


async def test_finished_card_drops_once_the_session_works_again(tmp_path):
    # Issue #15 end to end: the review card is shown while resting, then gone
    # once the same session is actively working.
    runner = AsyncMock(return_value=_FINISHED)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()
    assert [i.kind for i in assistant.view.insights] == ["finished"]

    state.update_session(_finished(tmp_path, status=SessionStatus.LIVE, thinking=True))
    await assistant.refresh()
    assert assistant.view.insights == ()


async def test_descendant_progress_prevents_false_parent_stall(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    now = datetime.now(UTC)
    state = AppState()
    parent = _finished(
        tmp_path,
        status=SessionStatus.LIVE,
        thinking=False,
        stalled=True,
        last_progress=now - timedelta(minutes=12),
    )
    child = _finished(
        tmp_path,
        key="codex:test:child",
        session_id="child",
        status=SessionStatus.LIVE,
        thinking=True,
        last_progress=now,
        parent_session_key=parent.key,
        is_delegated=True,
    )
    state.update_session(parent)
    state.update_session(child)
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    assert assistant.view.insights == ()
    runner.assert_not_awaited()


async def test_merged_pr_produces_no_card_and_skips_the_model(tmp_path):
    runner = AsyncMock(return_value=_FINISHED)
    state = AppState()
    session = _finished(tmp_path, last_text="Shipped the guardrail in PR #1628.")
    state.update_session(session)
    context = GitContext(
        "ScandinavianOutdoor/tilhi",
        "feature/guardrail",
        False,
        (PullRequestContext("ScandinavianOutdoor/tilhi", 1628, "Guardrail", "u", "merged"),),
    )
    assistant = AssistantService(
        _config(tmp_path),
        state,
        runner=runner,
        context_resolver=_StubResolver({session.key: context}),
    )

    await assistant.refresh()

    assert assistant.view.insights == ()  # merged work is done — no green card
    runner.assert_not_awaited()  # and we don't even ask the model


async def test_verdict_is_cached_until_the_final_message_changes(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()
    await assistant.refresh()
    assert runner.await_count == 1  # unchanged evidence -> no re-classification

    state.update_session(_finished(tmp_path, last_text="Actually I hit an error."))
    await assistant.refresh()
    assert runner.await_count == 2  # changed final message -> reclassified


async def test_classification_failure_fails_open_to_a_card(tmp_path):
    runner = AsyncMock(side_effect=RuntimeError("codex down"))
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    assert len(assistant.view.insights) == 1  # never silently drop a possible handoff
    assert assistant.view.error == "Some agents could not be read."


async def test_actively_working_agent_is_not_classified(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    state = AppState()
    state.update_session(
        _finished(tmp_path, status=SessionStatus.LIVE, thinking=True)
    )
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    runner.assert_not_awaited()
    assert assistant.view.insights == ()


async def test_handle_hides_card_until_evidence_changes(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)
    await assistant.refresh()

    assert assistant.handle("codex:test:thread-1") is True
    assert assistant.view.insights == ()

    await assistant.refresh()  # still acknowledged
    assert assistant.view.insights == ()

    state.update_session(_finished(tmp_path, last_text="New problem appeared."))
    await assistant.refresh()  # evidence changed -> resurfaces
    assert len(assistant.view.insights) == 1
    assert not assistant.dismissals.is_dismissed("codex:test:thread-1")


async def test_done_pr_stays_dismissed_when_github_is_unavailable_after_restart(tmp_path):
    path = tmp_path / "agentdeck.db"
    session = _finished(tmp_path, last_text="Opened PR #151 for review.")
    pull = PullRequestContext(
        repository="eerovil/agentdeck",
        number=151,
        title="Keep done sessions dismissed",
        url="https://github.com/eerovil/agentdeck/pull/151",
        status="open",
    )
    trusted = GitContext("eerovil/agentdeck", "fix/deckhand", False, (pull,))
    config = _config(tmp_path, max_sessions=1)

    state = AppState(db=Db(path))
    state.update_session(session)
    first = AssistantService(
        config,
        state,
        runner=AsyncMock(return_value=_FINISHED),
        context_resolver=_StubResolver({session.key: trusted}),
    )
    await first.refresh()
    assert [item.headline for item in first.view.insights] == [
        "PR #151 ready for review"
    ]
    assert first.handle(session.key) is True

    # A newer session can push this handled one outside the capped triage window.
    # Its dismissal remains durable even though the checkpoint no longer carries
    # its signature.
    state.update_session(
        _finished(
            tmp_path,
            key="codex:test:newer",
            session_id="newer",
            last_activity=datetime.now(UTC),
        )
    )
    await first.refresh()
    assert session.key not in first._signatures
    assert first.dismissals.is_dismissed(session.key)

    unavailable = GitContext(
        "eerovil/agentdeck", "fix/deckhand", False, (), pulls_complete=False
    )
    resolver = _StubResolver({session.key: unavailable})
    state2 = AppState(db=Db(path))
    state2.update_session(session)
    second = AssistantService(
        config,
        state2,
        runner=AsyncMock(return_value=_FINISHED),
        context_resolver=resolver,
    )

    await second.refresh()
    assert second.view.insights == ()
    assert second.is_handled(session.key)

    resolver._contexts[session.key] = trusted
    await second.refresh()
    assert second.view.insights == ()
    assert second.is_handled(session.key)


def test_incomplete_pr_context_only_reuses_previous_pr_evidence(tmp_path):
    assistant = _service(tmp_path, AsyncMock())
    session = _finished(tmp_path, last_text="Opened PR #151 for review.")
    pull = PullRequestContext(
        repository="eerovil/agentdeck",
        number=151,
        title="Keep done sessions dismissed",
        url="https://github.com/eerovil/agentdeck/pull/151",
        status="open",
    )
    trusted = GitContext("eerovil/agentdeck", "fix/deckhand", False, (pull,))
    unavailable = GitContext(
        "eerovil/agentdeck", "fix/deckhand", False, (), pulls_complete=False
    )
    previous = assistant._evidence_signature(session, trusted, None)

    assert (
        assistant._evidence_signature(
            session, unavailable, None, previous_signature=previous
        )
        == previous
    )
    changed = replace(session, last_text="A new problem appeared.")
    assert (
        assistant._evidence_signature(
            changed, unavailable, None, previous_signature=previous
        )
        != previous
    )


async def test_unhandle_restores_card_immediately(tmp_path):
    runner = AsyncMock(return_value=_ATTENTION)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)
    await assistant.refresh()
    assistant.handle("codex:test:thread-1")

    assert assistant.unhandle("codex:test:thread-1") is True
    assert len(assistant.view.insights) == 1
    assert assistant.handled_items == ()


async def test_interaction_card_prioritized_and_deterministic(tmp_path, monkeypatch):
    runner = AsyncMock()
    interaction = PendingInteraction(
        id="i-1",
        kind="approval",
        thread_id="thread-1",
        turn_id="turn-1",
        title="Approve command",
        command="rm -rf build",
    )
    monkeypatch.setattr(
        PROVIDERS["codex"], "pending_interaction", lambda account, session: interaction
    )
    state = AppState()
    state.update_session(
        _finished(tmp_path, capabilities=frozenset({Capability.INTERACT}))
    )
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    (insight,) = assistant.view.insights
    assert insight.headline == "Approval needed"
    runner.assert_not_awaited()


def test_interaction_is_hidden_without_interact_capability(tmp_path, monkeypatch):
    # pending_interaction owns the INTERACT gate now, so for a session lacking
    # INTERACT the provider's interaction *read* must never even be reached.
    calls = []
    monkeypatch.setattr(
        PROVIDERS["codex"],
        "_actionable_interaction",
        lambda account, session_id: calls.append(session_id),
    )
    assistant = _service(tmp_path, AsyncMock())
    session = _finished(tmp_path)

    assert assistant._interaction(session) is None
    assert calls == []


async def test_checkpoint_restores_view_without_reclassifying(tmp_path):
    path = tmp_path / "agentdeck.db"
    state = AppState(db=Db(path))
    state.update_session(_finished(tmp_path))
    first = _service(tmp_path, AsyncMock(return_value=_ATTENTION), state=state)
    await first.refresh()
    assert len(first.view.insights) == 1

    reloaded_runner = AsyncMock(return_value=_ATTENTION)
    state2 = AppState(db=Db(path))
    state2.update_session(_finished(tmp_path))
    second = _service(tmp_path, reloaded_runner, state=state2)

    assert len(second.view.insights) == 1  # restored from checkpoint
    await second.refresh()
    reloaded_runner.assert_not_awaited()  # cached verdict survived the restart


async def test_waiting_dismissal_updates_checkpoint_before_restart(tmp_path):
    path = tmp_path / "agentdeck.db"
    session = _finished(tmp_path, question="Ship it?")
    state = AppState(db=Db(path))
    state.update_session(session)
    first = _service(tmp_path, AsyncMock(), state=state)
    await first.refresh()
    assert len(first.view.insights) == 1

    assert first.handle(session.key) is True
    assert first.view.insights == ()

    state2 = AppState(db=Db(path))
    state2.update_session(session)
    second = _service(tmp_path, AsyncMock(), state=state2)

    assert second.view.insights == ()
    assert second.is_handled(session.key)


def test_dedupe_and_order_collapses_duplicates_and_sinks_finished():
    cards = [
        AssistantInsight("a", "finished", "PR #255 ready for review", "d"),
        AssistantInsight("b", "waiting", "Asked you a question", "d"),
        AssistantInsight("c", "finished", "PR #255 ready for review", "d"),  # duplicate PR
        AssistantInsight("d", "finished", "PR #42 ready for review", "d"),
    ]
    ordered = AssistantService._dedupe_and_order(cards)
    assert [c.headline for c in ordered] == [
        "Asked you a question",  # active attention first
        "PR #255 ready for review",  # deduped to one, finished sinks below
        "PR #42 ready for review",
    ]


def test_run_codex_is_exported():
    assert callable(run_codex)


def test_view_and_insight_are_importable_from_assistant():
    view = AssistantView(state="ready", insights=(AssistantInsight("k", "waiting", "h", "d"),))
    assert replace(view, summary="x").summary == "x"


class _FakePush:
    def __init__(self, *, enabled=True):
        self.enabled = enabled
        self.sent: list[tuple[str, str, str]] = []

    def send_to_all(self, title, body="", url="/"):
        self.sent.append((title, body, url))
        return 1


def _push_service(tmp_path, push):
    service = AssistantService(
        _config(tmp_path),
        AppState(),
        runner=AsyncMock(),
        context_resolver=_StubResolver(),
        push=push,
    )
    return service


async def _drain(svc):
    while svc._push_tasks:
        await asyncio.gather(*tuple(svc._push_tasks), return_exceptions=True)


def _view(*insights):
    return AssistantView(state="ready", insights=tuple(insights))


def _commit(svc, *insights):
    svc._commit_view(_view(*insights), {i.session_key: "s" for i in insights}, manual=False)


async def test_new_deckhand_insight_triggers_one_push_and_dedupes(tmp_path):
    # Issue #7 (#13): a newly-appeared attention item pushes once; an unchanged
    # one on the next refresh does not; a genuinely new one pushes again.
    push = _FakePush()
    svc = _push_service(tmp_path, push)
    a = AssistantInsight("codex:test:a", "waiting", "Asked you a question", "answer it")

    svc._commit_view(_view(a), {"codex:test:a": "s"}, manual=False)
    await _drain(svc)
    assert push.sent == [("Asked you a question", "answer it", "/sessions/codex:test:a")]

    # same insight again → no new push
    svc._commit_view(_view(a), {"codex:test:a": "s"}, manual=False)
    await _drain(svc)
    assert len(push.sent) == 1

    # a different session's insight → another push (the existing one stays quiet)
    b = AssistantInsight("codex:test:b", "finished", "PR #9 ready for review", "look")
    svc._commit_view(
        _view(a, b),
        {"codex:test:a": "s", "codex:test:b": "s"},
        manual=False,
    )
    await _drain(svc)
    assert push.sent[1] == ("PR #9 ready for review", "look", "/sessions/codex:test:b")
    assert len(push.sent) == 2


async def test_changed_headline_notifies_again(tmp_path):
    push = _FakePush()
    svc = _push_service(tmp_path, push)
    _commit(svc, AssistantInsight("s:1", "finished", "PR ready", "d"))
    await _drain(svc)
    _commit(svc, AssistantInsight("s:1", "blocked", "PR blocked", "d"))
    await _drain(svc)
    assert [t for t, _, _ in push.sent] == ["PR ready", "PR blocked"]


async def test_no_push_when_disabled(tmp_path):
    push = _FakePush(enabled=False)
    svc = _push_service(tmp_path, push)
    _commit(svc, AssistantInsight("s:1", "waiting", "h", "d"))
    await _drain(svc)
    assert push.sent == []


def test_deckhand_statuses_gathers_and_resolves(tmp_path):
    from unittest.mock import AsyncMock

    from agentdeck.models import Session, SessionStatus
    from agentdeck.triage import AssistantInsight, AssistantView, Verdict

    def sess(key, **kw):
        return Session(
            key=key, account_key="codex:test", session_id=key,
            status=SessionStatus.IDLE, show_when_idle=True, **kw,
        )

    sessions = [sess("v"), sess("i"), sess("none"), sess("q", question="Which?")]
    state = AppState()
    for session in sessions:
        state.update_session(session)
    svc = _service(tmp_path, AsyncMock(), state=state)

    svc._verdicts = {"v": ("sig", Verdict("finished", "All shipped", ""))}
    svc.view = AssistantView(
        state="ready", insights=(AssistantInsight("i", "waiting", "Asked", "d"),)
    )
    statuses = svc.deckhand_statuses(sessions)
    assert statuses["v"].state == "finished"  # durable verdict, resting
    assert statuses["i"].state == "waiting"  # live attention view
    assert statuses["none"].state == "unknown"  # resting, unclassified
    assert statuses["q"].state == "waiting"  # pending question off the session
    # A working, unclassified session yields no pill (absent key).
    working = sess("w", thinking=True)
    state.update_session(working)
    assert "w" not in svc.deckhand_statuses([working])
