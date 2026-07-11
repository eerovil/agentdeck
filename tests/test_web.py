import asyncio
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from agentdeck.app import create_app
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.models import Capability, Session, SessionStatus, UsageSnapshot
from agentdeck.providers.claude_code.provider import worker_type


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


def test_worker_type_classification():
    assert worker_type(True, False) == "kanban"  # kanban dispatch always wins
    assert worker_type(True, True) == "kanban"  # even when also RC-spawned
    assert worker_type(False, True) == "cloud"  # cloud/RC, non-kanban
    assert worker_type(False, False) == "you"  # your own interactive chat


async def test_card_colour_class_and_direct_claudeai_button(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:kb1",
            account_key="claude_code:test",
            session_id="kb1",
            status=SessionStatus.LIVE,
            title="store#2728 · Fix things",
            worker_type="kanban",
            deep_link="https://claude.ai/code/session_kb1",
            issue_url="https://github.com/ScandinavianOutdoor/store/issues/2728",
            issue_status="open",
            issue_status_kind="open",
        )
    )
    async with _client(app) as c:
        r = await c.get("/")
    # Worker-type colour class on the card, and the default session (no
    # worker_type) falls back to the "you" stripe.
    assert "wt-kanban" in r.text
    assert "wt-you" in r.text
    # A direct claude.ai button, wired to the deep link — not the transcript URL.
    assert 'class="cc-btn"' in r.text
    assert 'data-href="https://claude.ai/code/session_kb1"' in r.text
    # And a GitHub issue button wired to the issue URL.
    assert 'class="gh-btn"' in r.text
    assert 'data-href="https://github.com/ScandinavianOutdoor/store/issues/2728"' in r.text
    # GitHub state badge.
    assert 'class="st-badge st-open"' in r.text


async def test_pwa_routes(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw = await c.get("/sw.js")
        mf = await c.get("/manifest.webmanifest")
        dash = await c.get("/")
    # Service worker: served from root (so its scope covers the whole app),
    # with the correct JS type and the scope-broadening header.
    assert sw.status_code == 200
    assert sw.headers["content-type"].startswith("text/javascript")
    assert sw.headers["service-worker-allowed"] == "/"
    assert "addEventListener('fetch'" in sw.text
    # The cache name is stamped with a content hash (no unresolved placeholder),
    # so any asset change busts the SW cache without a manual version bump.
    assert "__CACHE_STAMP__" not in sw.text
    assert "agentdeck-static-" in sw.text
    # Manifest: correct content type + installability essentials.
    assert mf.status_code == 200
    assert mf.headers["content-type"].startswith("application/manifest+json")
    body = mf.json()
    assert body["display"] == "standalone"
    assert any(i.get("purpose") == "maskable" for i in body["icons"])
    # The page registers the worker and links the manifest.
    assert "/manifest.webmanifest" in dash.text
    assert "serviceWorker.register('/sw.js')" in dash.text
    # Live-stream recovery hook + a build stamp so the phone can prove freshness.
    assert "visibilitychange" in dash.text
    assert "streamStale" in dash.text
    assert 'class="build"' in dash.text
    assert "agentdeck v" in dash.text
    # Dashboard HTML must not be cached, so a deploy's inline JS actually lands.
    assert dash.headers["cache-control"] == "no-cache"


async def test_partial_sessions(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/partials/sessions")
    assert r.status_code == 200
    assert "Hello World Session" in r.text


async def test_dashboard_has_list_search(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/")
    # Filter input lives outside #sessions so live swaps don't wipe it, and the
    # re-apply hook fires on each swap.
    assert 'id="q"' in r.text
    assert r.text.index('id="q"') < r.text.index('id="sessions"')
    assert "htmx:afterSwap" in r.text


async def test_card_shows_agent_response(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title="Hello World Session",
            last_prompt="what is the answer",
            last_text="the answer is 42",
        )
    )
    async with _client(app) as c:
        r = await c.get("/partials/sessions")
    assert "what is the answer" in r.text  # user's prompt
    assert "the answer is 42" in r.text  # agent's reply is now in the list view


def test_tool_events_hidden_and_live_marker(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_tool_activity, render_transcript_events

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    events = [
        TranscriptEvent(seq=1, role="tool", text="huge noisy tool result"),
        TranscriptEvent(seq=2, role="assistant", tool_name="Bash", text=None),
        TranscriptEvent(seq=3, role="assistant", text="here is my answer"),
    ]
    html = render_transcript_events(templates, events)
    assert "huge noisy tool result" not in html  # past tool result dropped
    assert "here is my answer" in html  # real assistant text kept
    # the live marker appears only while actively working
    assert "Using tools" in render_tool_activity(templates, "Using tools")
    assert "tool-wait" not in render_tool_activity(templates, None)


def test_activity_label_fallback():
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import activity_label

    tool_call = TranscriptEvent(seq=1, role="assistant", tool_name="Bash", text=None)
    tool_result = TranscriptEvent(seq=1, role="tool", text="output")
    user_msg = TranscriptEvent(seq=1, role="user", text="do it")
    reply = TranscriptEvent(seq=1, role="assistant", text="done")

    # a long tool run: quiet (not streaming) but last line is a tool → still busy
    assert activity_label(True, False, tool_call) == "Using tools"
    assert activity_label(True, False, tool_result) == "Using tools"
    # unanswered prompt while quiet → working (slow first token doesn't read idle)
    assert activity_label(True, False, user_msg) == "Working"
    # finished reply, quiet → idle (no marker)
    assert activity_label(True, False, reply) is None
    # finished reply but actively writing → working
    assert activity_label(True, True, reply) == "Working"
    # dead process → never a marker
    assert activity_label(False, True, tool_call) is None
    # open turn but no write for ages → stalled worker, not "Using tools" forever
    assert activity_label(True, False, tool_call, age_s=10_000) is None
    assert activity_label(True, False, user_msg, age_s=10_000) is None


def test_working_sessions_sort_first(tmp_path):
    from datetime import UTC, datetime, timedelta

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    now = datetime.now(UTC)
    # a quiet LIVE session, more recently active than the busy one
    state.update_session(
        Session(
            key="claude_code:test:quiet",
            account_key="claude_code:test",
            session_id="quiet",
            status=SessionStatus.LIVE,
            thinking=False,
            last_activity=now,
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:busy",
            account_key="claude_code:test",
            session_id="busy",
            status=SessionStatus.LIVE,
            thinking=True,
            activity="Using tools",
            last_activity=now - timedelta(minutes=3),
        )
    )
    keys = [s.session_id for s in state.visible_sessions()]
    assert keys.index("busy") < keys.index("quiet")  # busy floats above, despite older mtime


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


def test_display_state_alive_vs_thinking():
    from agentdeck.models import Session, SessionStatus

    def s(**kw):
        return Session(key="k", account_key="a", session_id="s", **kw)

    assert s(status=SessionStatus.LIVE, thinking=True).display_state == "thinking"
    assert s(status=SessionStatus.LIVE, thinking=False).display_state == "idle"  # alive, resting
    assert s(status=SessionStatus.IDLE).display_state == "idle"
    assert s(status=SessionStatus.REMOTE).display_state == "remote"


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
    assert direct.status_code == 200  # but reachable by direct URL (read-only view)


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
