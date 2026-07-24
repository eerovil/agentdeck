from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from agentdeck.config import AccountConfig, AppConfig, AssistantConfig
from agentdeck.models import Session, SessionStatus, TranscriptEvent
from agentdeck.providers import PROVIDERS
from agentdeck.state import AppState
from agentdeck.titles import TitleService, normalize_generated_title


class _TranscriptProvider:
    def __init__(self, events):
        self.events = events

    async def recent_conversation(self, account, session, limit=4):
        return self.events[-limit:]


def _config(tmp_path):
    return AppConfig(
        assistant=AssistantConfig(enabled=True, account_key="codex:test"),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )


def _session(tmp_path, **overrides):
    values = dict(
        key="codex:test:thread-1",
        account_key="codex:test",
        session_id="thread-1",
        status=SessionStatus.IDLE,
        cwd=tmp_path,
        title="store#42 · Native refund issue (review)",
        initial_prompt="Investigate the broken refund flow",
        last_prompt="Implement the validated fix",
        last_text="The fix is ready.",
        last_role="assistant",
        last_activity=datetime.now(UTC),
        issue_url="https://github.com/example/store/issues/42",
        show_when_idle=True,
    )
    values.update(overrides)
    return Session(**values)


async def test_title_generation_uses_conversation_and_persists_semantic_title(
    tmp_path, monkeypatch
):
    events = [
        TranscriptEvent(1, "system", "secret system instructions"),
        TranscriptEvent(2, "user", "Investigate the broken refund flow"),
        TranscriptEvent(3, "tool", "private command output", tool_name="exec"),
        TranscriptEvent(4, "assistant", "The API returns the wrong error."),
        TranscriptEvent(5, "user", "Implement the validated fix"),
        TranscriptEvent(6, "assistant", "The fix is ready."),
        TranscriptEvent(7, "assistant", "subagent result", subagent="child"),
    ]
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider(events))
    runner = AsyncMock(
        return_value={"titles": [{"id": 0, "title": "store#42 · Fix refund errors (review)"}]}
    )
    state = AppState()
    session = _session(tmp_path)
    state.update_session(session)
    service = TitleService(_config(tmp_path), state, runner=runner)

    assert await service.refresh() == 0

    record = state.generated_titles[session.key]
    assert record.title == "Fix refund errors"
    assert state.sessions[session.key].title == "store#42 · Native refund issue (review)"
    assert state.sessions[session.key].display_title == "store#42 · Fix refund errors (review)"
    assert state.sessions[session.key].project_name == "store"
    assert (
        state.sessions[session.key].generated_title_subject
        == "#42 · Fix refund errors (review)"
    )
    prompt = runner.await_args.args[2]
    assert "Investigate the broken refund flow" in prompt
    assert "The fix is ready." in prompt
    assert "private command output" not in prompt
    assert "secret system instructions" not in prompt
    assert "subagent result" not in prompt

    # Provider rescans reapply the persisted generated title over native metadata.
    state.replace_account_sessions("codex:test", [_session(tmp_path)])
    assert state.sessions[session.key].title == "store#42 · Native refund issue (review)"
    assert state.sessions[session.key].display_title == "store#42 · Fix refund errors (review)"


async def test_title_refresh_waits_for_changed_chat_to_be_idle(tmp_path, monkeypatch):
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider([]))
    runner = AsyncMock(
        side_effect=[
            {"titles": [{"id": 0, "title": "Investigate refund failures"}]},
            {"titles": [{"id": 0, "title": "Deploy the refund fix"}]},
        ]
    )
    state = AppState()
    project = tmp_path / "agentdeck"
    (project / ".git").mkdir(parents=True)
    session = _session(project, issue_url=None, title="Raw prompt")
    state.update_session(session)
    service = TitleService(_config(tmp_path), state, runner=runner)

    await service.refresh()
    await service.refresh()
    assert runner.await_count == 1

    state.update_session(
        _session(
            project,
            issue_url=None,
            title="Raw prompt",
            last_prompt="Deploy it now",
            thinking=True,
        )
    )
    await service.refresh()
    assert runner.await_count == 1

    state.update_session(
        _session(
            project,
            issue_url=None,
            title="Raw prompt",
            last_prompt="Deploy it now",
            thinking=False,
        )
    )
    await service.refresh()
    assert runner.await_count == 2
    assert state.sessions[session.key].title == "Raw prompt"
    assert state.sessions[session.key].display_title == "agentdeck · Deploy the refund fix"


async def test_generated_title_uses_worktree_project_and_strips_model_prefix(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider([]))
    runner = AsyncMock(
        return_value={"titles": [{"id": 0, "title": "agentdeck · Fix session titles"}]}
    )
    state = AppState()
    session = _session(
        tmp_path / "agentdeck" / ".worktrees" / "issue-159" / "src" / "agentdeck",
        issue_url=None,
        title="Requested changes",
    )
    state.update_session(session)

    await TitleService(_config(tmp_path), state, runner=runner).refresh()

    assert state.generated_titles[session.key].title == "Fix session titles"
    assert state.sessions[session.key].display_title == "agentdeck · Fix session titles"
    assert state.sessions[session.key].generated_title_subject == "Fix session titles"


async def test_generated_title_uses_repository_root_for_nested_cwd(tmp_path, monkeypatch):
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider([]))
    project = tmp_path / "agentdeck"
    (project / ".git").mkdir(parents=True)
    state = AppState()
    session = _session(project / "src" / "agentdeck", issue_url=None)
    state.update_session(session)

    await TitleService(
        _config(tmp_path),
        state,
        runner=AsyncMock(
            return_value={"titles": [{"id": 0, "title": "Fix session titles"}]}
        ),
    ).refresh()

    assert state.sessions[session.key].display_title == "agentdeck · Fix session titles"


async def test_title_generation_waits_until_project_identity_is_available(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider([]))
    runner = AsyncMock()
    state = AppState()
    session = _session(tmp_path, issue_url=None, cwd=None)
    state.update_session(session)

    assert await TitleService(_config(tmp_path), state, runner=runner).refresh() == 0
    runner.assert_not_awaited()
    assert session.key not in state.generated_titles


async def test_stale_title_result_is_discarded_when_a_new_prompt_arrives(
    tmp_path, monkeypatch
):
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider([]))
    state = AppState()
    session = _session(tmp_path, issue_url=None, title="Raw prompt")
    state.update_session(session)

    async def stale_runner(account, config, prompt):
        state.update_session(
            _session(
                tmp_path,
                issue_url=None,
                title="Raw prompt",
                last_prompt="A newer objective",
            )
        )
        return {"titles": [{"id": 0, "title": "Old objective"}]}

    service = TitleService(_config(tmp_path), state, runner=stale_runner)
    assert await service.refresh() == 0
    assert session.key not in state.generated_titles
    assert state.sessions[session.key].display_title == "Raw prompt"


async def test_backfill_batches_newest_visible_chats_first(tmp_path, monkeypatch):
    monkeypatch.setitem(PROVIDERS, "codex", _TranscriptProvider([]))
    runner = AsyncMock(
        return_value={
            "titles": [{"id": index, "title": f"Generated {index}"} for index in range(4)]
        }
    )
    state = AppState()
    now = datetime.now(UTC)
    for index in range(5):
        state.update_session(
            _session(
                tmp_path,
                key=f"codex:test:thread-{index}",
                session_id=f"thread-{index}",
                issue_url=None,
                title=f"Native {index}",
                initial_prompt=f"Task {index}",
                last_prompt=f"Task {index}",
                last_activity=now + timedelta(seconds=index),
            )
        )
    service = TitleService(_config(tmp_path), state, runner=runner)

    assert await service.refresh() == 1
    assert set(state.generated_titles) == {
        "codex:test:thread-1",
        "codex:test:thread-2",
        "codex:test:thread-3",
        "codex:test:thread-4",
    }


def test_generated_title_is_hard_limited():
    session = Session(
        key="k", account_key="a", session_id="s", status=SessionStatus.IDLE
    )
    title = normalize_generated_title(session, "x" * 100)
    assert title is not None
    assert len(title) == 80
    assert title.endswith("…")
