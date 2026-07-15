from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

from agentdeck.assistant import AssistantInsight, AssistantService, AssistantView, run_codex
from agentdeck.config import AccountConfig, AppConfig, AssistantConfig
from agentdeck.db import Db
from agentdeck.models import PendingInteraction, Session, SessionStatus
from agentdeck.providers import PROVIDERS
from agentdeck.state import AppState

_ATTENTION = {"attention": True, "summary": "Stopped early", "reason": "Needs a decision"}
_CLEAR = {"attention": False, "summary": "All tests pass", "reason": ""}


class _StubResolver:
    """Never resolves git/PR context, so tests stay hermetic and offline."""

    async def resolve(self, sessions):
        return {}


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


async def test_finished_agent_cleared_by_the_model_shows_no_card(tmp_path):
    runner = AsyncMock(return_value=_CLEAR)
    state = AppState()
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    assert assistant.view.insights == ()
    assert assistant.view.summary == "Nothing needs your attention right now."


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
    assert "codex:test:thread-1" not in assistant._handled


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
    state.update_session(_finished(tmp_path))
    assistant = _service(tmp_path, runner, state=state)

    await assistant.refresh()

    (insight,) = assistant.view.insights
    assert insight.headline == "Approval needed"
    runner.assert_not_awaited()


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
