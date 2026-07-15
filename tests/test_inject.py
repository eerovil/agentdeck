import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.inject import InjectionService, InjectionStatus, QueuedMessage
from agentdeck.models import (
    Account,
    Capability,
    InjectResult,
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
    Session,
    SessionStatus,
    TranscriptEvent,
)
from agentdeck.providers.codex import WRITABLE_ROOTS_CONFIG_OVERRIDE
from agentdeck.providers.codex.inject import (
    inject_session,
    is_injectable_rollout,
    start_session,
)
from agentdeck.providers.codex.transcripts import last_turn_complete


def _line(type_, payload):
    return json.dumps({"timestamp": "2026-07-13T12:00:00Z", "type": type_, "payload": payload})


def _exec_rollout(tmp_path: Path, *, complete: bool = True) -> Path:
    sid = "019f5b5b-6281-7a00-a197-d020a1243d2d"
    path = tmp_path / f"rollout-{sid}.jsonl"
    lines = [
        _line(
            "session_meta",
            {"session_id": sid, "cwd": str(tmp_path), "source": "exec"},
        ),
        _line("event_msg", {"type": "task_started"}),
    ]
    if complete:
        lines.append(_line("event_msg", {"type": "task_complete"}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_completed_exec_turn_is_the_only_injectable_rollout(tmp_path):
    complete = _exec_rollout(tmp_path)
    assert last_turn_complete(complete)
    assert is_injectable_rollout(complete, "exec")
    assert not is_injectable_rollout(complete, "cli")

    active_dir = tmp_path / "active"
    active_dir.mkdir()
    active = _exec_rollout(active_dir, complete=False)
    assert not last_turn_complete(active)
    assert not is_injectable_rollout(active, "exec")


class _FakeProcess:
    def __init__(self, returncode=0):
        self.returncode = None
        self.final_returncode = returncode
        self.pid = os.getpid()
        self.input = None

    async def communicate(self, value):
        self.input = value
        self.returncode = self.final_returncode
        return (b"", b"")


async def test_inject_session_passes_prompt_on_stdin(tmp_path):
    path = _exec_rollout(tmp_path)
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "019f5b5b-6281-7a00-a197-d020a1243d2d",
        SessionStatus.IDLE,
        cwd=tmp_path,
        kind="exec",
        capabilities=frozenset({Capability.INJECT}),
    )
    process = _FakeProcess()
    spawned = {}
    images = [tmp_path / "one.png", tmp_path / "two.jpg"]

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return process

    result = await inject_session(
        account,
        session,
        path,
        "do the next thing",
        timeout_s=10,
        images=images,
        process_factory=factory,
    )
    assert result.accepted
    assert process.input == b"do the next thing\n"
    assert "do the next thing" not in spawned["args"]
    assert spawned["args"][:4] == ("codex", "exec", "resume", session.session_id)
    assert spawned["args"][4:10] == (
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
    )
    assert spawned["args"][10:14] == (
        "-i",
        str(images[0]),
        "-i",
        str(images[1]),
    )
    assert spawned["kwargs"]["env"]["CODEX_HOME"] == str(tmp_path)
    assert spawned["kwargs"]["start_new_session"] is True


async def test_inject_session_rechecks_turn_boundary_before_spawn(tmp_path):
    path = _exec_rollout(tmp_path, complete=False)
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        kind="exec",
    )
    called = False

    async def factory(*args, **kwargs):
        nonlocal called
        called = True

    result = await inject_session(
        account,
        session,
        path,
        "message",
        timeout_s=10,
        process_factory=factory,
    )
    assert not result.accepted
    assert "completed" in result.reason
    assert called is False


class _BlockingProvider:
    def __init__(self):
        self.release = asyncio.Event()
        self.messages = []

    async def inject(self, account, session, message, *, timeout_s):
        self.messages.append(message)
        await self.release.wait()
        return InjectResult(True)


async def test_injection_service_queues_one_session_fifo(tmp_path):
    changed = []
    service = InjectionService(InjectConfig(enabled=True), on_change=changed.append)
    provider = _BlockingProvider()
    account = Account("codex:test", "codex", "test", tmp_path)
    session = Session(
        "codex:test:sid",
        account.key,
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.INJECT}),
    )
    first = await service.start(account, session, provider, "first")
    second = await service.start(account, session, provider, "second")
    assert first.accepted
    assert second.accepted
    status = service.status(session.key)
    assert [item.text for item in status.items] == ["first", "second"]
    assert [item.state for item in status.items] == ["queued", "queued"]
    assert changed == [session.key, session.key]
    provider.release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not service.can_queue(session.key):
            break
    assert provider.messages == ["first", "second"]
    assert [item.state for item in service.status(session.key).items] == [
        "complete",
        "complete",
    ]
    await service.stop()


async def test_queued_message_remains_visible_after_navigation(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    app.state.app_state.sessions["codex:test:sid"].show_when_idle = True
    release = asyncio.Event()

    async def fake_inject(account, session, message, *, timeout_s):
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "keep this queued"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202

    # A new browser/page request sees the authoritative server-side queue on
    # both the list card and reopened detail page.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        dashboard = await client.get("/")
        detail = await client.get("/sessions/codex:test:sid")
    assert 'class="card-pending" data-pending-count="1"' in dashboard.text
    assert "keep this queued" in dashboard.text
    assert "keep this queued" in detail.text
    assert 'class="ev user pending-message"' in detail.text
    assert detail.text.index('class="ev user pending-message"') < detail.text.index(
        'id="tool-activity"'
    )

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if not app.state.injector.can_queue("codex:test:sid"):
            break
    await app.state.injector.stop()


def test_pending_message_dedup_only_matches_new_transcript_turns():
    from agentdeck.web.render import pending_injection_messages

    queued_at = datetime.now(UTC)
    item = QueuedMessage(1, "testing", created_at=queued_at)
    status = InjectionStatus("queued", items=(item,))
    old_same_text = TranscriptEvent(
        seq=1,
        role="user",
        text="testing",
        ts=queued_at - timedelta(minutes=1),
    )
    new_same_text = TranscriptEvent(
        seq=2,
        role="user",
        text="testing",
        ts=queued_at + timedelta(seconds=1),
    )

    assert pending_injection_messages(status, [old_same_text]) == [item]
    assert pending_injection_messages(status, [old_same_text, new_same_text]) == []


async def test_start_session_passes_first_prompt_on_stdin(tmp_path):
    account = Account("codex:test", "codex", "test", tmp_path)
    process = _FakeProcess()
    spawned = {}
    images = [tmp_path / "one.png", tmp_path / "two.webp"]

    async def factory(*args, **kwargs):
        spawned["args"] = args
        spawned["kwargs"] = kwargs
        return process

    result = await start_session(
        account,
        tmp_path,
        "start something",
        timeout_s=10,
        images=images,
        process_factory=factory,
    )
    assert result.accepted
    assert spawned["args"] == (
        "codex",
        "exec",
        "--config",
        'web_search="live"',
        "--config",
        "sandbox_workspace_write.network_access=true",
        "--config",
        WRITABLE_ROOTS_CONFIG_OVERRIDE,
        "-i",
        str(images[0]),
        "-i",
        str(images[1]),
        "--json",
        "--skip-git-repo-check",
        "-",
    )
    assert process.input == b"start something\n"


def _web_app(tmp_path, *, enabled=True):
    config = AppConfig(
        history=HistoryConfig(enabled=False),
        inject=InjectConfig(enabled=enabled, max_message_chars=50),
        accounts=[AccountConfig(provider="codex", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    session = Session(
        "codex:test:sid",
        "codex:test",
        "sid",
        SessionStatus.IDLE,
        cwd=tmp_path,
        capabilities=frozenset({Capability.TRANSCRIPT, Capability.INJECT}),
    )
    app.state.app_state.update_session(session)
    return app


async def test_inject_route_accepts_and_reports_status(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    provider = app.state.injector
    release = asyncio.Event()
    messages = []

    async def fake_inject(account, session, message, *, timeout_s):
        messages.append(message)
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "continue safely"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        assert 'aria-label="Message queued: continue safely"' in response.text
        assert 'hx-swap-oob="beforeend:.transcript"' in response.text
        assert 'class="ev user pending-message"' in response.text
        assert '<span class="ev-role">user</span>' in response.text
        assert 'class="ev-time"' in response.text
        assert "user · queued" not in response.text
        conflict = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "again"},
            headers={"origin": "http://test"},
        )
        assert conflict.status_code == 202
        assert "again" in conflict.text
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not provider.can_queue("codex:test:sid"):
                break
        assert messages == ["continue safely", "again"]
        status = await client.get("/partials/sessions/codex:test:sid/inject-status")
        assert "Queue completed" not in status.text
        assert "continue safely" not in status.text
    await provider.stop()


async def test_inject_route_kill_switch_validation_and_origin(tmp_path):
    disabled = _web_app(tmp_path, enabled=False)
    async with AsyncClient(
        transport=ASGITransport(app=disabled), base_url="http://test"
    ) as client:
        page = await client.get("/sessions/codex:test:sid")
        assert "Continue this Codex session" not in page.text
        response = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "hello"},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 403

    enabled = _web_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=enabled), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        assert '>Message</label>' in page.text
        assert 'maxlength="50"' in page.text
        assert page.text.index('class="inject-form"') < page.text.index(
            'id="inject-result"'
        )
        cross_site = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "hello"},
            headers={"origin": "https://evil.example"},
        )
        assert cross_site.status_code == 403
        empty = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "   "},
            headers={"origin": "http://test"},
        )
        assert empty.status_code == 422
        oversized = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "x" * 51},
            headers={"origin": "http://test"},
        )
        assert oversized.status_code == 422
        wrong_type = await client.post(
            "/sessions/codex:test:sid/inject",
            content="hello",
            headers={"origin": "http://test", "content-type": "text/plain"},
        )
        assert wrong_type.status_code == 415


async def test_inject_route_validates_images_and_cleans_up(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    app.state.config.inject.max_image_bytes = 16
    app.state.config.inject.max_image_total_bytes = 24
    release = asyncio.Event()
    started = asyncio.Event()
    received = []

    async def fake_inject(account, session, message, *, timeout_s, images=None):
        received.extend(images or [])
        started.set()
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "inject", fake_inject)
    png = b"\x89PNG\r\n\x1a\nsmall"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        invalid = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "inspect"},
            files={"images": ("fake.png", b"not an image", "image/png")},
            headers={"origin": "http://test"},
        )
        assert invalid.status_code == 422

        oversized = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "inspect"},
            files={"images": ("large.png", png + b"xxxx", "image/png")},
            headers={"origin": "http://test"},
        )
        assert oversized.status_code == 422

        accepted = await client.post(
            "/sessions/codex:test:sid/inject",
            data={"message": "inspect"},
            files={"images": ("attacker-name.png", png, "image/png")},
            headers={"origin": "http://test"},
        )
        assert accepted.status_code == 202
        await asyncio.wait_for(started.wait(), timeout=1)
        assert len(received) == 1
        saved = received[0]
        assert saved.is_file()
        assert saved.name != "attacker-name.png"
        assert saved.suffix == ".png"
        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not saved.exists():
                break
        assert not saved.exists()
        assert not saved.parent.exists()
    await app.state.injector.stop()


async def test_steer_image_cleanup_waits_for_turn(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({*session.capabilities, Capability.STEER})
    turn_done = asyncio.Event()
    cleanup_started = asyncio.Event()
    received = []

    async def fake_steer(account, session, message, *, images=None):
        received.extend(images or [])
        return InjectResult(True)

    async def fake_wait(account, session_id, *, timeout_s):
        cleanup_started.set()
        await turn_done.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "steer", fake_steer)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    png = b"\x89PNG\r\n\x1a\nsmall"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sessions/codex:test:sid/steer",
            data={"message": "look now"},
            files={"images": ("screen.png", png, "image/png")},
            headers={"origin": "http://test"},
        )
        assert response.status_code == 200
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        saved = received[0]
        assert saved.exists()
        turn_done.set()
        for _ in range(20):
            await asyncio.sleep(0)
            if not saved.exists():
                break
        assert not saved.exists()
    await app.state.injector.stop()


async def test_new_session_route_and_enter_to_send_ui(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    release = asyncio.Event()

    async def fake_start(account, cwd, message, *, timeout_s):
        assert cwd == tmp_path
        assert message == "build a new thing"
        await release.wait()
        return InjectResult(True)

    from agentdeck.providers import PROVIDERS

    monkeypatch.setattr(PROVIDERS["codex"], "start_session", fake_start)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dashboard = await client.get("/")
        assert "New Codex chat" in dashboard.text
        assert 'class="send-on-enter"' in dashboard.text
        detail = await client.get("/sessions/codex:test:sid")
        assert "Enter to send" in detail.text
        assert "requestSubmit" in detail.text
        response = await client.post(
            "/sessions/new",
            data={
                "account_key": "codex:test",
                "cwd": str(tmp_path),
                "message": "build a new thing",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 202
        assert "Starting the Codex chat" in response.text
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            status = app.state.injector.new_status("codex:test")
            if status and status.state == "complete":
                break
        status_response = await client.get(
            "/partials/new-session-status?account_key=codex:test"
        )
        assert "New Codex chat completed" in status_response.text
    await app.state.injector.stop()


async def test_machine_delegation_api_returns_final_message(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    release = asyncio.Event()

    async def fake_start(
        account,
        cwd,
        message,
        *,
        timeout_s,
        sandbox,
        model,
        approval_policy,
    ):
        assert account.key == "codex:test"
        assert cwd == tmp_path
        assert message == "review this change"
        assert sandbox == "workspace-write"
        assert model is None
        assert approval_policy == "on-request"
        return InjectResult(True, session_id="delegated-thread")

    async def fake_wait(account, session_id, *, timeout_s):
        assert session_id == "delegated-thread"
        await release.wait()
        return InjectResult(True)

    async def fake_result(account, session_id):
        assert session_id == "delegated-thread"
        return "The delegated review is complete."

    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "start_session", fake_start)
    monkeypatch.setattr(provider, "wait_for_session", fake_wait)
    monkeypatch.setattr(provider, "session_result", fake_result)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "review this change"},
        )
        assert response.status_code == 202
        delegation_id = response.json()["id"]
        for _ in range(10):
            await asyncio.sleep(0)
            status = await client.get(f"/api/delegations/{delegation_id}")
            if status.json()["state"] == "running":
                break
        assert status.json() == {
            "id": delegation_id,
            "state": "running",
            "account_key": "codex:test",
            "session_key": "codex:test:delegated-thread",
            "session_url": "/sessions/codex:test:delegated-thread",
            "reason": None,
            "final_message": None,
            "interaction": None,
        }

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            status = await client.get(f"/api/delegations/{delegation_id}")
            if status.json()["state"] == "complete":
                break
        assert status.json()["final_message"] == "The delegated review is complete."

    await app.state.injector.stop()


async def test_machine_delegation_api_validates_requests(tmp_path):
    disabled = _web_app(tmp_path, enabled=False)
    async with AsyncClient(
        transport=ASGITransport(app=disabled), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/delegations",
            json={"cwd": str(tmp_path), "message": "hello"},
        )
        assert response.status_code == 422
        assert response.json()["detail"] == "message injection is disabled"
        unsafe = await client.post(
            "/api/delegations",
            json={
                "cwd": str(tmp_path),
                "message": "hello",
                "sandbox": "danger-full-access",
            },
        )
        assert unsafe.status_code == 422
        missing = await client.get("/api/delegations/not-a-real-id")
        assert missing.status_code == 404


async def test_owned_session_question_shows_stop_beside_send(tmp_path, monkeypatch):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset(
        {
            Capability.TRANSCRIPT,
            Capability.INJECT,
            Capability.INTERACT,
            Capability.STEER,
            Capability.INTERRUPT,
        }
    )
    interaction = PendingInteraction(
        id="opaque-token",
        kind="question",
        thread_id="sid",
        turn_id="turn-1",
        title="Codex needs your answer",
        questions=(
            InteractionQuestion(
                id="database",
                header="Database",
                prompt="Which database should we use?",
                options=(
                    InteractionOption("Postgres", "Relational"),
                    InteractionOption("SQLite", "Embedded"),
                ),
                allow_other=True,
            ),
        ),
    )
    from agentdeck.providers import PROVIDERS

    provider = PROVIDERS["codex"]
    monkeypatch.setattr(provider, "owns_session", lambda account, session: True)
    monkeypatch.setattr(
        provider, "pending_interaction", lambda account, session: interaction
    )
    answered = {}

    async def answer(account, session, interaction_id, *, answers, decision):
        answered.update(
            interaction_id=interaction_id,
            answers=answers,
            decision=decision,
        )
        return InjectResult(True)

    async def accepted(*args, **kwargs):
        return InjectResult(True)

    monkeypatch.setattr(provider, "answer_interaction", answer)
    monkeypatch.setattr(provider, "steer", accepted)
    monkeypatch.setattr(provider, "interrupt", accepted)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")
        assert "Which database should we use?" in page.text
        assert "Postgres" in page.text
        assert "Send now" not in page.text
        assert 'hx-post="/sessions/codex:test:sid/interrupt"' in page.text
        assert 'form="interrupt-form"' in page.text
        assert 'aria-label="Stop active turn">Stop</button>' in page.text
        controls = page.text[page.text.index('id="composer-controls"') :]
        controls = controls[: controls.index("</div>")]
        assert controls.index(">Stop</button>") < controls.index(">Send</button>")
        assert "owned-controls" not in page.text
        response = await client.post(
            "/sessions/codex:test:sid/interaction",
            data={
                "interaction_id": "opaque-token",
                "answer__database": "Postgres",
                "decision": "accept",
            },
            headers={"origin": "http://test"},
        )
        assert response.status_code == 200
        assert answered == {
            "interaction_id": "opaque-token",
            "answers": {"database": ["Postgres"]},
            "decision": "accept",
        }
        steer = await client.post(
            "/sessions/codex:test:sid/steer",
            data={"message": "Use SQLite instead"},
            headers={"origin": "http://test"},
        )
        assert steer.status_code == 200
        stop = await client.post(
            "/sessions/codex:test:sid/interrupt",
            headers={"origin": "http://test"},
        )
        assert stop.status_code == 200
        assert 'id="inject-result" class="inject-result running"' in stop.text
        assert 'aria-label="Stopping active turn"' in stop.text


async def test_idle_composer_hides_stop_button(tmp_path):
    app = _web_app(tmp_path)
    session = app.state.app_state.sessions["codex:test:sid"]
    session.capabilities = frozenset({Capability.TRANSCRIPT, Capability.INJECT})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/sessions/codex:test:sid")

    assert 'id="composer-controls"' in page.text
    assert ">Send</button>" in page.text
    assert 'aria-label="Stop active turn"' not in page.text
