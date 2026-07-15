from __future__ import annotations

from unittest.mock import AsyncMock

from agentdeck.assistant import AssistantAnswer, AssistantInsight, AssistantService, AssistantView
from agentdeck.config import AccountConfig, AppConfig, AssistantConfig
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.models import (
    InjectResult,
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
    Session,
    SessionStatus,
)
from agentdeck.providers import PROVIDERS
from agentdeck.state import AppState


def _config(tmp_path, **assistant):
    return AppConfig(
        assistant=AssistantConfig(enabled=True, **assistant),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )


def _session(tmp_path):
    return Session(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        cwd=tmp_path,
        title="Choose the database",
        last_prompt="Build the local prototype",
        question="Which database?",
        show_when_idle=True,
    )


def _question(*, kind="question", allow_other=False, secret=False):
    return PendingInteraction(
        id="interaction-1",
        kind=kind,
        thread_id="thread-1",
        turn_id="turn-1",
        title="Codex needs your answer",
        questions=(
            InteractionQuestion(
                id="database",
                header="Database",
                prompt="Which database?",
                options=(
                    InteractionOption("SQLite", "Local and simple"),
                    InteractionOption("Postgres", "Shared service"),
                ),
                allow_other=allow_other,
                secret=secret,
            ),
        ),
    )


def test_auto_answer_gate_accepts_only_complete_explicit_question_choices():
    answer = (AssistantAnswer("database", ("SQLite",)),)

    assert AssistantService._answers_are_safe(_question(), answer)
    assert not AssistantService._answers_are_safe(_question(kind="command_approval"), answer)
    assert not AssistantService._answers_are_safe(_question(allow_other=True), answer)
    assert not AssistantService._answers_are_safe(_question(secret=True), answer)
    assert not AssistantService._answers_are_safe(
        _question(), (AssistantAnswer("database", ("Something else",)),)
    )


async def test_ensure_session_context_resolves_and_caches_chat_outside_analysis(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    context = GitContext(
        repository="eerovil/agentdeck",
        branch="feature/deckhand",
        dirty=False,
        pull_requests=(
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=91,
                title="Add PR context",
                url="https://github.com/eerovil/agentdeck/pull/91",
                status="merged",
            ),
        ),
    )
    resolver = AsyncMock(return_value={})
    resolver.resolve = AsyncMock(return_value={session.key: context})
    assistant = AssistantService(_config(tmp_path), state, context_resolver=resolver)

    assert await assistant.ensure_session_context(session) == context
    assert await assistant.ensure_session_context(session) == context
    resolver.resolve.assert_awaited_once_with([session])


async def test_ensure_session_context_augments_cached_context_from_transcript(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    cached = GitContext("eerovil/agentdeck", "feature/deckhand", False)
    expanded = GitContext(
        "eerovil/agentdeck",
        "feature/deckhand",
        False,
        pull_requests=(
            PullRequestContext(
                "eerovil/agentdeck",
                91,
                "First PR",
                "https://github.com/eerovil/agentdeck/pull/91",
                "merged",
            ),
            PullRequestContext(
                "eerovil/agentdeck",
                92,
                "Second PR",
                "https://github.com/eerovil/agentdeck/pull/92",
                "open",
            ),
        ),
    )
    resolver = AsyncMock(return_value={})
    resolver.resolve = AsyncMock(return_value={session.key: expanded})
    assistant = AssistantService(_config(tmp_path), state, context_resolver=resolver)
    assistant.contexts[session.key] = cached
    assistant.view = AssistantView(
        state="ready",
        summary="PR attribution needs attention.",
        insights=(
            AssistantInsight(
                session_key=session.key,
                kind="coordination",
                headline="Wrong PR association",
                detail="Stale context.",
            ),
        ),
    )

    result = await assistant.ensure_session_context(
        session,
        transcript_context=(
            "Earlier https://github.com/eerovil/agentdeck/pull/91 and later "
            "https://github.com/eerovil/agentdeck/pull/92"
        ),
    )

    assert result == expanded
    assert assistant.view.insights == ()
    assert assistant.view.summary == "Nothing needs your attention right now."
    assert assistant._force is True
    resolved_session = resolver.resolve.await_args.args[0][0]
    assert "/pull/91" in resolved_session.last_text
    assert "/pull/92" in resolved_session.last_text


async def test_refresh_renders_advice_and_auto_answers_safe_choice(tmp_path, monkeypatch):
    state = AppState()
    state.update_session(_session(tmp_path))
    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "pending_interaction", lambda account, session: _question())
    answer = AsyncMock(return_value=InjectResult(True))
    monkeypatch.setattr(provider, "answer_interaction", answer)

    async def runner(account, config, prompt):
        assert account.key == "codex:test"
        assert config.model == "gpt-5.6-luna"
        assert "Which database?" in prompt
        return {
            "summary": "One agent needs a routine choice.",
            "insights": [
                {
                    "session_key": "codex:test:thread-1",
                    "kind": "waiting",
                    "headline": "Use SQLite for the local prototype",
                    "detail": "The prompt explicitly says this is local.",
                    "answers": [{"question_id": "database", "values": ["SQLite"]}],
                    "safe_to_auto_answer": True,
                    "confidence": 0.98,
                }
            ],
        }

    assistant = AssistantService(
        _config(tmp_path, auto_answer=True), state, runner=runner
    )
    await assistant.refresh()

    assert assistant.view.state == "ready"
    assert assistant.view.summary == "One agent needs a routine choice."
    assert assistant.view.insights[0].kind == "waiting"
    assert assistant.view.actions[0].session_key == "codex:test:thread-1"
    answer.assert_awaited_once()
    assert answer.await_args.kwargs == {
        "answers": {"database": ["SQLite"]},
        "decision": None,
    }


async def test_refresh_never_auto_answers_approval(tmp_path, monkeypatch):
    state = AppState()
    state.update_session(_session(tmp_path))
    provider = PROVIDERS["codex"]
    monkeypatch.setattr(
        provider,
        "pending_interaction",
        lambda account, session: _question(kind="command_approval"),
    )
    answer = AsyncMock(return_value=InjectResult(True))
    monkeypatch.setattr(provider, "answer_interaction", answer)

    async def runner(account, config, prompt):
        return {
            "summary": "Approval is waiting.",
            "insights": [
                {
                    "session_key": "codex:test:thread-1",
                    "kind": "waiting",
                    "headline": "Approval requested",
                    "detail": "Review it yourself.",
                    "answers": [{"question_id": "database", "values": ["SQLite"]}],
                    "safe_to_auto_answer": True,
                    "confidence": 1.0,
                }
            ],
        }

    assistant = AssistantService(
        _config(tmp_path, auto_answer=True), state, runner=runner
    )
    await assistant.refresh()

    answer.assert_not_awaited()
    assert not assistant.view.actions


async def test_refresh_omits_sessions_when_all_related_prs_are_terminal(tmp_path):
    state = AppState()
    state.update_session(_session(tmp_path))
    context = GitContext(
        repository="eerovil/agentdeck",
        branch="feature/deckhand",
        dirty=False,
        pull_requests=(
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=91,
                title="Add PR context",
                url="https://github.com/eerovil/agentdeck/pull/91",
                status="merged",
            ),
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=92,
                title="Closed replacement",
                url="https://github.com/eerovil/agentdeck/pull/92",
                status="closed",
            ),
        ),
    )
    resolver = AsyncMock(return_value={})
    resolver.resolve = AsyncMock(return_value={"codex:test:thread-1": context})

    runner = AsyncMock()

    assistant = AssistantService(
        _config(tmp_path), state, runner=runner, context_resolver=resolver
    )
    await assistant.refresh()

    assert assistant.contexts["codex:test:thread-1"] == context
    assert assistant.view.summary == "Nothing needs your attention right now."
    runner.assert_not_awaited()


async def test_refresh_suppresses_attention_card_when_all_related_prs_are_merged(tmp_path):
    state = AppState()
    state.update_session(_session(tmp_path))
    context = GitContext(
        repository="eerovil/agentdeck",
        branch="feature/deckhand",
        dirty=True,
        pull_requests=(
            PullRequestContext(
                repository="eerovil/agentdeck",
                number=91,
                title="Completed work",
                url="https://github.com/eerovil/agentdeck/pull/91",
                status="merged",
            ),
        ),
    )
    resolver = AsyncMock(return_value={})
    resolver.resolve = AsyncMock(return_value={"codex:test:thread-1": context})

    runner = AsyncMock()

    assistant = AssistantService(
        _config(tmp_path), state, runner=runner, context_resolver=resolver
    )
    await assistant.refresh()

    assert assistant.view.insights == ()
    assert assistant.view.summary == "Nothing needs your attention right now."
    runner.assert_not_awaited()


def test_result_drops_hallucinated_session_keys():
    view = AssistantService._parse_result(
        {
            "summary": "Done",
            "insights": [
                {
                    "session_key": "not-real",
                    "kind": "info",
                    "headline": "Nope",
                    "detail": "Nope",
                    "answers": [],
                    "safe_to_auto_answer": False,
                    "confidence": 0,
                }
            ],
        },
        {"codex:test:thread-1"},
    )

    assert not view.insights
