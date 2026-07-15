from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from agentdeck.assistant import (
    AssistantAnswer,
    AssistantInsight,
    AssistantService,
    AssistantView,
    run_codex,
)
from agentdeck.config import AccountConfig, AppConfig, AssistantConfig
from agentdeck.db import Db
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
    destructive = PendingInteraction(
        **{
            **vars(_question()),
            "questions": (
                InteractionQuestion(
                    id="database",
                    header="Database",
                    prompt="What next?",
                    options=(InteractionOption("Delete database"), InteractionOption("Cancel")),
                ),
            ),
        }
    )
    assert not AssistantService._answers_are_safe(
        destructive, (AssistantAnswer("database", ("Delete database",)),)
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
                    "interaction_id": "interaction-1",
                    "kind": "waiting",
                    "headline": "Use SQLite for the local prototype",
                    "detail": "The prompt explicitly says this is local.",
                    "answers": [{"question_id": "database", "values": ["SQLite"]}],
                    "safe_to_auto_answer": True,
                    "confidence": 0.98,
                }
            ],
        }

    assistant = AssistantService(_config(tmp_path, auto_answer=True), state, runner=runner)
    await assistant.refresh()

    assert assistant.view.state == "ready"
    assert assistant.view.summary == "Deckhand is tracking 1 item that still needs attention."
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

    assistant = AssistantService(_config(tmp_path, auto_answer=True), state, runner=runner)
    await assistant.refresh()

    answer.assert_not_awaited()
    assert not assistant.view.actions


async def test_refresh_clears_waiting_insight_when_agent_resumes(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    session.question = None
    state.update_session(session)
    runner = AsyncMock(
        side_effect=[
            {
                "summary": "One item needs attention.",
                "insights": [
                    {
                        "session_key": "codex:test:thread-1",
                        "kind": "stalled",
                        "headline": "Agent appears stalled",
                        "detail": "No progress is visible.",
                        "answers": [],
                        "safe_to_auto_answer": False,
                        "confidence": 0.9,
                    }
                ],
            },
            {"summary": "Nothing to report.", "insights": []},
        ]
    )
    assistant = AssistantService(_config(tmp_path), state, runner=runner)

    await assistant.refresh(snapshot=assistant.snapshot())
    state.update_session(
        Session(
            **{
                **vars(state.sessions["codex:test:thread-1"]),
                "thinking": True,
                "activity": "Working",
                "subagent_count": 2,
            }
        )
    )
    await assistant.refresh(snapshot=assistant.snapshot())

    assert assistant.view.insights == ()
    assert assistant.view.summary == "Nothing needs your attention right now."


async def test_refresh_keeps_wording_when_model_rephrases_unchanged_chat(tmp_path):
    state = AppState()
    state.update_session(_session(tmp_path))

    def result(headline):
        return {
            "summary": "One item needs attention.",
            "insights": [
                {
                    "session_key": "codex:test:thread-1",
                    "kind": "coordination",
                    "headline": headline,
                    "detail": "Keep one owner.",
                    "answers": [],
                    "safe_to_auto_answer": False,
                    "confidence": 0.9,
                }
            ],
        }

    assistant = AssistantService(
        _config(tmp_path),
        state,
        runner=AsyncMock(side_effect=[result("Stable title"), result("Rephrased title")]),
    )

    await assistant.refresh(snapshot=assistant.snapshot())
    await assistant.refresh(snapshot=assistant.snapshot())

    assert [item.headline for item in assistant.view.insights] == ["Stable title"]


async def test_handled_insight_stays_hidden_until_chat_evidence_changes(tmp_path):
    state = AppState()
    state.update_session(_session(tmp_path))
    result = {
        "summary": "One item needs attention.",
        "insights": [
            {
                "session_key": "codex:test:thread-1",
                "kind": "waiting",
                "headline": "Choose the database",
                "detail": "A choice is required.",
                "answers": [],
                "safe_to_auto_answer": False,
                "confidence": 0.9,
            }
        ],
    }
    assistant = AssistantService(_config(tmp_path), state, runner=AsyncMock(return_value=result))

    await assistant.refresh(snapshot=assistant.snapshot())
    assert assistant.handle("codex:test:thread-1")
    await assistant.refresh(snapshot=assistant.snapshot())
    assert assistant.view.insights == ()
    assert assistant.handled_items[0].headline == "Choose the database"

    session = state.sessions["codex:test:thread-1"]
    state.update_session(Session(**{**vars(session), "last_text": "The choice changed."}))
    await assistant.refresh(snapshot=assistant.snapshot())

    assert [item.headline for item in assistant.view.insights] == ["Choose the database"]
    assert "codex:test:thread-1" not in assistant._handled


async def test_unhandle_restores_cached_insight_immediately(tmp_path):
    state = AppState()
    state.update_session(_session(tmp_path))
    assistant = AssistantService(_config(tmp_path), state)
    insight = AssistantInsight(
        session_key="codex:test:thread-1",
        kind="waiting",
        headline="Choose the database",
        detail="A choice is required.",
    )
    assistant.view = AssistantView(
        state="ready", summary="One item needs attention.", insights=(insight,)
    )
    assistant._evidence_signatures[insight.session_key] = assistant._evidence_signature(
        assistant._snapshot_row(state.sessions[insight.session_key])
    )

    assert assistant.handle(insight.session_key)
    assert assistant.view.insights == ()
    assert assistant.unhandle(insight.session_key)

    assert assistant.view.insights == (insight,)
    assert assistant.handled_items == ()
    assert insight.session_key not in assistant._handled


def test_handled_panel_exposes_only_latest_item_as_undo_stack(tmp_path):
    state = AppState()
    first_session = _session(tmp_path)
    second_session = Session(
        **{
            **vars(first_session),
            "key": "codex:test:thread-2",
            "session_id": "thread-2",
            "title": "Second decision",
        }
    )
    state.update_session(first_session)
    state.update_session(second_session)
    first = AssistantInsight(first_session.key, "waiting", "First decision", "Resolve first.")
    second = AssistantInsight(
        second_session.key, "coordination", "Second decision", "Resolve second."
    )
    assistant = AssistantService(_config(tmp_path), state)
    assistant.view = AssistantView(state="ready", insights=(first, second))
    for session in (first_session, second_session):
        assistant._evidence_signatures[session.key] = assistant._evidence_signature(
            assistant._snapshot_row(session)
        )

    assert assistant.handle(first.session_key)
    assert assistant.handle(second.session_key)

    assert len(assistant._handled) == 2
    assert [(item.session_key, item.headline) for item in assistant.handled_items] == [
        (second.session_key, "Second decision")
    ]

    assert assistant.unhandle(second.session_key)
    assert [(item.session_key, item.headline) for item in assistant.handled_items] == [
        (first.session_key, "First decision")
    ]


async def test_refresh_retains_insight_when_chat_leaves_analysis_window(tmp_path):
    state = AppState()
    first = _session(tmp_path)
    second = Session(
        key="codex:test:thread-2",
        account_key="codex:test",
        session_id="thread-2",
        status=SessionStatus.IDLE,
        title="Another chat",
        show_when_idle=True,
    )
    state.update_session(first)
    state.update_session(second)
    runner = AsyncMock(
        side_effect=[
            {
                "summary": "One item needs attention.",
                "insights": [
                    {
                        "session_key": first.key,
                        "kind": "coordination",
                        "headline": "Keep this finding",
                        "detail": "It remains relevant.",
                        "answers": [],
                        "safe_to_auto_answer": False,
                        "confidence": 0.9,
                    }
                ],
            },
            {"summary": "Nothing to report.", "insights": []},
        ]
    )
    assistant = AssistantService(_config(tmp_path, max_sessions=1), state, runner=runner)

    await assistant.refresh(snapshot=[assistant._snapshot_row(first)])
    await assistant.refresh(snapshot=[assistant._snapshot_row(second)])

    assert [item.headline for item in assistant.view.insights] == ["Keep this finding"]


async def test_refresh_keeps_unrelated_question_when_all_related_prs_are_terminal(tmp_path):
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

    runner = AsyncMock(
        return_value={
            "summary": "A new decision is waiting.",
            "insights": [
                {
                    "session_key": "codex:test:thread-1",
                    "interaction_id": None,
                    "kind": "waiting",
                    "headline": "Choose the database",
                    "detail": "This question is unrelated to the historical PRs.",
                    "answers": [],
                    "safe_to_auto_answer": False,
                    "confidence": 0.9,
                }
            ],
        }
    )

    assistant = AssistantService(_config(tmp_path), state, runner=runner, context_resolver=resolver)
    await assistant.refresh()

    assert assistant.contexts["codex:test:thread-1"] == context
    assert [item.headline for item in assistant.view.insights] == ["Choose the database"]
    runner.assert_awaited_once()


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

    runner = AsyncMock(
        return_value={
            "summary": "Review is waiting.",
            "insights": [
                {
                    "session_key": "codex:test:thread-1",
                    "interaction_id": None,
                    "kind": "waiting",
                    "headline": "PR #91 needs review",
                    "detail": "Merge PR #91 after review.",
                    "answers": [],
                    "safe_to_auto_answer": False,
                    "confidence": 0.9,
                }
            ],
        }
    )

    assistant = AssistantService(_config(tmp_path), state, runner=runner, context_resolver=resolver)
    await assistant.refresh()

    assert assistant.view.insights == ()
    assert assistant.view.summary == "Nothing needs your attention right now."
    runner.assert_awaited_once()


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


def test_pr_insight_must_match_target_chats_authoritative_context(tmp_path):
    state = AppState()
    assistant = AssistantService(_config(tmp_path), state)
    assistant.contexts = {
        "codex:test:agentdeck": GitContext("eerovil/agentdeck", "master", False),
        "codex:test:storm": GitContext(
            "protecomp/storm",
            "claude/issue-238",
            False,
            pull_requests=(
                PullRequestContext(
                    "protecomp/storm",
                    239,
                    "Custobar coupon activation",
                    "https://github.com/protecomp/storm/pull/239",
                    "open",
                ),
            ),
        ),
    }
    wrong = AssistantInsight(
        "codex:test:agentdeck",
        "waiting",
        "PR #239 is idle and open",
        "The completed fixes likely need review.",
    )
    right = AssistantInsight(
        "codex:test:storm",
        "waiting",
        "PR #239 is awaiting review",
        "See https://github.com/protecomp/storm/pull/239.",
    )

    result = assistant._suppress_unattributed_pr_insights(
        AssistantView(state="ready", summary="Two PRs need review.", insights=(wrong, right))
    )

    assert result.insights == (right,)
    assert result.summary == "Deckhand is tracking 1 item that still needs attention."


def test_pr_headline_includes_project_and_feature_from_authoritative_title(tmp_path):
    state = AppState()
    assistant = AssistantService(_config(tmp_path), state)
    session_key = "codex:test:storm"
    assistant.contexts[session_key] = GitContext(
        "protecomp/storm",
        "feature/search",
        False,
        (
            PullRequestContext(
                "protecomp/storm",
                255,
                "Improve Elasticsearch free-text search",
                "https://github.com/protecomp/storm/pull/255",
                "open",
            ),
        ),
    )
    insight = AssistantInsight(
        session_key,
        "waiting",
        "PR #255 is open and awaiting review",
        "Implementation and tests are complete.",
    )

    result = assistant._enrich_pr_headlines(AssistantView(state="ready", insights=(insight,)))

    assert result.insights[0].headline == (
        "Storm · Elasticsearch · PR #255 is open and awaiting review"
    )
    assert assistant._enrich_pr_headlines(result) == result

    number_in_detail = replace(
        insight,
        headline="Open PR needs review",
        detail="Implementation is complete in PR #255.",
    )
    detail_result = assistant._enrich_pr_headlines(
        AssistantView(state="ready", insights=(number_in_detail,))
    )
    assert detail_result.insights[0].headline == ("Storm · Elasticsearch · PR #255 needs review")


async def test_refresh_does_not_retain_old_cross_chat_pr_insight(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    state.update_session(session)
    assistant = AssistantService(
        _config(tmp_path),
        state,
        runner=AsyncMock(return_value={"summary": "Nothing to report.", "insights": []}),
    )
    assistant.contexts[session.key] = GitContext("eerovil/agentdeck", "master", False)
    row = assistant._snapshot_row(session)
    assistant._evidence_signatures[session.key] = assistant._evidence_signature(row)
    assistant.view = AssistantView(
        state="ready",
        summary="PR #239 needs review.",
        insights=(
            AssistantInsight(
                session.key,
                "waiting",
                "PR #239 is idle and open",
                "The completed fixes likely need review.",
            ),
        ),
    )

    await assistant.refresh(snapshot=[row])

    assert assistant.view.insights == ()
    assert assistant.view.summary == "Nothing needs your attention right now."


def test_analysis_window_always_includes_blocking_chat(tmp_path):
    state = AppState()
    ordinary = _session(tmp_path)
    ordinary.question = None
    blocked = Session(
        **{
            **vars(ordinary),
            "key": "codex:test:thread-2",
            "session_id": "thread-2",
            "title": "Older blocked chat",
            "question": "Choose a deployment target?",
        }
    )
    state.update_session(ordinary)
    state.update_session(blocked)
    assistant = AssistantService(_config(tmp_path, max_sessions=1), state)

    assert [row["session_key"] for row in assistant.snapshot()] == [blocked.key]
    assert assistant.analysis_session_count == 1
    assert assistant.total_session_count == 2


def test_interaction_snapshot_contains_approval_context(tmp_path):
    interaction = PendingInteraction(
        id="approval-1",
        kind="command_approval",
        thread_id="thread-1",
        turn_id="turn-1",
        title="Approve command?",
        message="Needed for validation",
        command="git push origin feature",
        cwd=str(tmp_path),
        url="https://example.test/approval",
        decisions=("accept", "decline"),
    )

    value = AssistantService._interaction_json(interaction)

    assert value is not None
    assert value["command"] == "git push origin feature"
    assert value["cwd"] == str(tmp_path)
    assert value["url"] == "https://example.test/approval"
    assert value["decisions"] == ["accept", "decline"]


async def test_auto_answer_is_bound_to_analyzed_interaction(tmp_path, monkeypatch):
    state = AppState()
    session = _session(tmp_path)
    state.update_session(session)
    first = _question()
    second = PendingInteraction(
        **{**vars(first), "id": "interaction-2", "title": "A newer question"}
    )
    current = [first]
    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "pending_interaction", lambda account, item: current[0])
    answer = AsyncMock(return_value=InjectResult(True))
    monkeypatch.setattr(provider, "answer_interaction", answer)
    assistant = AssistantService(_config(tmp_path, auto_answer=True), state, runner=AsyncMock())
    row = assistant._snapshot_row(session)

    async def runner(account, config, prompt):
        current[0] = second
        return {
            "summary": "One choice.",
            "insights": [
                {
                    "session_key": session.key,
                    "interaction_id": first.id,
                    "kind": "waiting",
                    "headline": "Choose SQLite",
                    "detail": "Explicit local choice.",
                    "answers": [{"question_id": "database", "values": ["SQLite"]}],
                    "safe_to_auto_answer": True,
                    "confidence": 1.0,
                }
            ],
        }

    assistant.runner = runner
    await assistant.refresh(snapshot=[row])

    answer.assert_not_awaited()
    assert assistant.view.actions == ()


def test_dirty_worktree_does_not_change_handled_evidence(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    assistant = AssistantService(_config(tmp_path), state)
    assistant.contexts[session.key] = GitContext("eerovil/agentdeck", "feature", False)
    clean = assistant._snapshot_row(session)
    assistant.contexts[session.key] = GitContext("eerovil/agentdeck", "feature", True)
    dirty = assistant._snapshot_row(session)

    assert assistant._evidence_signature(clean) == assistant._evidence_signature(dirty)


def test_analysis_signature_ignores_poll_noise_but_tracks_pr_status():
    first = {
        "session_key": "codex:test:thread-1",
        "title": "Implement search",
        "cwd": "/code/project",
        "state": "idle",
        "activity": "",
        "question": "",
        "last_prompt": "Implement search",
        "last_response": "The PR is ready.",
        "subagents": 0,
        "git": {
            "repository": "eerovil/agentdeck",
            "branch": "feature/search",
            "dirty": False,
            "pull_requests": [
                {
                    "number": 255,
                    "state": "open",
                    "url": "https://github.com/eerovil/agentdeck/pull/255",
                }
            ],
        },
        "interaction": None,
    }
    second = {
        **first,
        "session_key": "codex:test:thread-2",
        "title": "Review search",
    }
    noisy_first = {
        **first,
        "state": "thinking",
        "activity": "Running command",
        "subagents": 2,
        "git": {**first["git"], "dirty": True},
    }
    noisy_second = {**second, "activity": "Waiting for command output"}

    baseline = AssistantService._analysis_signature([first, second])
    assert AssistantService._analysis_signature([noisy_second, noisy_first]) == baseline

    merged = {
        **noisy_first,
        "git": {
            **noisy_first["git"],
            "pull_requests": [
                {**noisy_first["git"]["pull_requests"][0], "state": "merged"}
            ],
        },
    }
    assert AssistantService._analysis_signature([noisy_second, merged]) != baseline


@pytest.mark.asyncio
async def test_background_poll_skips_luna_until_material_evidence_changes(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    state.update_session(session)
    runner_calls = 0

    async def runner(account, config, prompt):
        nonlocal runner_calls
        runner_calls += 1
        return {"summary": "Nothing needs attention.", "insights": []}

    class Resolver:
        calls = 0
        pr_status = "open"

        async def resolve(self, sessions):
            self.calls += 1
            pull = PullRequestContext(
                "eerovil/agentdeck",
                255,
                "Implement search",
                "https://github.com/eerovil/agentdeck/pull/255",
                self.pr_status,
                head_branch="feature/search",
                base_branch="master",
            )
            return {
                item.key: GitContext(
                    "eerovil/agentdeck", "feature/search", False, (pull,)
                )
                for item in sessions
            }

    resolver = Resolver()
    assistant = AssistantService(
        _config(tmp_path, refresh_interval_s=0.01),
        state,
        runner=runner,
        context_resolver=resolver,
    )

    async def wait_until(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.01)
        pytest.fail("Deckhand background poll did not complete")

    await assistant.start()
    try:
        assistant._wake.set()
        await wait_until(lambda: runner_calls == 1)

        state.update_session(
            replace(
                session,
                status=SessionStatus.LIVE,
                thinking=True,
                activity="Running command",
                subagent_count=2,
            )
        )
        assistant._last_run = 0
        assistant._wake.set()
        unchanged_started_at = time.monotonic()
        await wait_until(lambda: resolver.calls >= 2)
        await asyncio.sleep(0.02)
        assert time.monotonic() - unchanged_started_at < 0.5
        assert runner_calls == 1

        resolver.pr_status = "merged"
        assistant._last_run = 0
        assistant._wake.set()
        await wait_until(lambda: runner_calls == 2)
    finally:
        await assistant.stop()


@pytest.mark.asyncio
async def test_manual_refresh_skips_unchanged_luna_quickly_and_runs_for_new_evidence(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    state.update_session(session)
    runner = AsyncMock(return_value={"summary": "Nothing needs attention.", "insights": []})
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value={})
    assistant = AssistantService(
        _config(tmp_path, refresh_interval_s=3600),
        state,
        runner=runner,
        context_resolver=resolver,
    )
    await assistant.refresh(snapshot=assistant.snapshot())
    runner.reset_mock()

    async def wait_until(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.01)
        pytest.fail("manual Deckhand refresh did not complete")

    await assistant.start()
    try:
        started_at = time.monotonic()
        assert assistant.request_refresh(manual=True)
        assert assistant.refresh_status == "Checking current evidence…"
        await wait_until(
            lambda: assistant.refresh_status == "No material changes · Luna not run"
        )
        assert time.monotonic() - started_at < 0.5
        runner.assert_not_awaited()
        assert resolver.resolve.await_count == 1

        state.update_session(replace(session, last_text="The transcript changed materially."))
        assert assistant.request_refresh(manual=True)
        await wait_until(lambda: runner.await_count == 1)
        await wait_until(
            lambda: assistant.refresh_status == "Material changes found · analysis updated"
        )
    finally:
        await assistant.stop()


@pytest.mark.asyncio
async def test_deckhand_checkpoint_survives_restart_without_reanalysis(tmp_path):
    path = tmp_path / "agentdeck.db"
    session = _session(tmp_path)
    result = {
        "summary": "One item needs attention.",
        "insights": [
            {
                "session_key": session.key,
                "kind": "waiting",
                "headline": "Choose the database",
                "detail": "The agent needs a database choice.",
                "answers": [],
                "safe_to_auto_answer": False,
                "confidence": 0.9,
            }
        ],
    }

    first_db = Db(path)
    first_state = AppState(db=first_db)
    first_state.update_session(session)
    first = AssistantService(
        _config(tmp_path), first_state, runner=AsyncMock(return_value=result)
    )
    await first.refresh(snapshot=first.snapshot())
    original_view = first.view
    original_signature = first._last_signature
    first_db.close()

    second_db = Db(path)
    second_state = AppState(db=second_db)
    runner = AsyncMock(return_value=result)
    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value={})
    second = AssistantService(
        _config(tmp_path, refresh_interval_s=0.01),
        second_state,
        runner=runner,
        context_resolver=resolver,
    )
    try:
        assert second.view == original_view
        assert second._last_signature == original_signature
        assert second._force is False

        second_state.update_session(session)
        await second.start()
        second._wake.set()
        for _ in range(100):
            if resolver.resolve.await_count:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("restored Deckhand did not check current evidence")
        await asyncio.sleep(0.02)

        runner.assert_not_awaited()
        assert second.view == original_view
    finally:
        await second.stop()
        second_db.close()


def test_unhandle_does_not_restore_card_after_evidence_changed(tmp_path):
    state = AppState()
    session = _session(tmp_path)
    state.update_session(session)
    assistant = AssistantService(_config(tmp_path), state)
    insight = AssistantInsight(session.key, "waiting", "Choose", "A choice is waiting.")
    assistant.view = AssistantView(state="ready", insights=(insight,))
    assistant._evidence_signatures[session.key] = assistant._evidence_signature(
        assistant._snapshot_row(session)
    )
    assert assistant.handle(session.key)
    state.update_session(Session(**{**vars(session), "last_text": "Already resolved."}))

    assert assistant.unhandle(session.key)
    assert assistant.view.insights == ()
    assert assistant._force is True


def test_terminal_filter_is_per_claim_and_parses_pr_lists(tmp_path):
    state = AppState()
    assistant = AssistantService(_config(tmp_path), state)
    session_key = "codex:test:thread-1"
    assistant.contexts[session_key] = GitContext(
        "eerovil/agentdeck",
        "feature",
        False,
        (
            PullRequestContext(
                "eerovil/agentdeck", 91, "Merged", "https://example.test/91", "merged"
            ),
            PullRequestContext("eerovil/agentdeck", 92, "Open", "https://example.test/92", "open"),
        ),
    )
    merged = AssistantInsight(session_key, "waiting", "PR #91 needs review", "Review it.")
    open_pull = AssistantInsight(session_key, "waiting", "PR #92 needs review", "Review it.")
    both = AssistantInsight(
        session_key, "coordination", "PRs #91 and #92 overlap", "Keep one owner."
    )

    result = assistant._suppress_terminal_pr_insights(
        AssistantView(state="ready", insights=(merged, open_pull, both))
    )

    assert result.insights == (open_pull, both)
    assert AssistantService._pr_claims(both)[0] == {91, 92}


def test_duplicate_underlying_work_is_shown_once(tmp_path):
    state = AppState()
    first_session = _session(tmp_path)
    second_session = Session(
        **{
            **vars(first_session),
            "key": "codex:test:thread-2",
            "session_id": "thread-2",
        }
    )
    state.update_session(first_session)
    state.update_session(second_session)
    assistant = AssistantService(_config(tmp_path), state)
    first = AssistantInsight(
        first_session.key,
        "coordination",
        "Compact deploy needs confirmation",
        "Choose one owner.",
        coordination_key="compact-deploy",
    )
    second = AssistantInsight(
        second_session.key,
        "stalled",
        "Compact deployment is unresolved",
        "The same work is waiting.",
        coordination_key="compact-deploy",
    )

    result = assistant._deduplicate_insights(AssistantView(state="ready", insights=(first, second)))

    assert result.insights == (first,)


async def test_codex_failure_does_not_expose_prompt_or_stderr(tmp_path, monkeypatch):
    class FailedProcess:
        returncode = 7

        async def communicate(self, value):
            assert b"private dashboard" in value
            return (b"", b"request failed: private transcript secret")

    async def create_process(*args, **kwargs):
        return FailedProcess()

    monkeypatch.setattr("agentdeck.assistant.asyncio.create_subprocess_exec", create_process)
    service = AssistantService(_config(tmp_path), AppState())
    account = service._account()
    assert account is not None

    with pytest.raises(RuntimeError) as error:
        await run_codex(account, service.config, "private dashboard")

    assert str(error.value) == "Codex assistant exited without an answer (status 7)"
    assert "secret" not in str(error.value)
