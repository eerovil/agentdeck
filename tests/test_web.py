import asyncio
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig, InjectConfig
from agentdeck.models import Capability, Session, SessionStatus, UsageSnapshot


def _app_with_state(tmp_path, *, with_transcript=False):
    import json

    config = AppConfig(
        history=HistoryConfig(enabled=False),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    if with_transcript:
        proj = tmp_path / "projects" / "-tmp"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"role": "user", "content": "first question"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "an answer here"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            },
        ]
        (proj / "sid1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    app = create_app(config)
    state = app.state.app_state
    state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=42.0,
            five_hour_resets_at=None,
            seven_day_pct=7.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )
    )
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def test_healthz(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_dashboard_renders_usage_and_session(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "Hello World Session" in r.text
    assert "42%" in r.text


async def test_partial_sessions(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/partials/sessions")
    assert r.status_code == 200
    assert "Hello World Session" in r.text


async def test_thinking_badge_renders(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:think1",
            account_key="claude_code:test",
            session_id="think1",
            status=SessionStatus.LIVE,
            thinking=True,
            title="Busy session",
        )
    )
    async with _client(app) as c:
        r = await c.get("/partials/sessions")
    assert "thinking-badge" in r.text
    assert "dot live thinking" in r.text


async def test_idle_sessions_hidden_but_reachable(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:idle1",
            account_key="claude_code:test",
            session_id="idle1",
            status=SessionStatus.IDLE,
            title="An Old Finished Session",
        )
    )
    async with _client(app) as c:
        listing = await c.get("/partials/sessions")
        direct = await c.get("/sessions/claude_code:test:idle1")
    assert "An Old Finished Session" not in listing.text  # hidden from the list
    assert "Hello World Session" in listing.text  # the live one still shows
    assert direct.status_code == 200  # but reachable by direct URL (inject path)


async def test_partial_limit_bars(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/partials/limit-bars")
    assert r.status_code == 200
    assert "42%" in r.text
    assert "7%" in r.text


async def test_session_detail_renders_transcript(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:sid1")
    assert r.status_code == 200
    assert "first question" in r.text
    assert "an answer here" in r.text
    assert "claude-opus-4-8" in r.text
    assert "15 tok" in r.text  # 10 input + 5 output summed from usage
    # usage bars paint server-side in the topbar (no separate /events socket)
    assert "42%" in r.text
    # the page binds its single SSE connection to the per-session stream
    assert 'sse-connect="/events/sessions/claude_code:test:sid1"' in r.text


async def test_session_detail_unknown_404(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:doesnotexist")
    assert r.status_code == 404


async def test_transcript_load_earlier_partial(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as c:
        r = await c.get("/partials/sessions/claude_code:test:sid1/transcript?before=0")
    assert r.status_code == 200
    assert "an answer here" in r.text


async def test_inject_disabled_returns_403(tmp_path):
    config = AppConfig(
        history=HistoryConfig(enabled=False),
        inject=InjectConfig(enabled=False),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.IDLE,
        )
    )
    async with _client(app) as c:
        r = await c.post("/sessions/claude_code:test:sid1/inject", data={"message": "hi"})
    assert r.status_code == 403


async def test_chat_disabled_returns_403(tmp_path):
    config = AppConfig(
        history=HistoryConfig(enabled=False),
        inject=InjectConfig(enabled=False),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.IDLE,
        )
    )
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:sid1/chat")
    assert r.status_code == 403


async def test_sse_initial_events(tmp_path):
    """The stream primes the client with both fragments on connect.

    Driven at the generator level: httpx's ASGITransport buffers the whole
    response body, which never completes for an unbounded SSE stream.
    """
    from agentdeck.web.routes_sse import _stream

    app = _app_with_state(tmp_path)

    class FakeRequest:
        def __init__(self, application):
            self.app = application

        async def is_disconnected(self):
            return True

    gen = _stream(FakeRequest(app))
    try:
        first = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        second = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    finally:
        await gen.aclose()

    both = first + second
    assert "event: usage" in both
    assert "event: sessions" in both
    assert "42%" in both  # rendered usage fragment rode the stream
    assert "Hello World Session" in both
