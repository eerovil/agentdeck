import asyncio
from datetime import UTC, datetime
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from playwright.async_api import async_playwright

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
    # The "hide closed" filter toggle is present.
    assert 'id="hide-closed"' in r.text


async def test_dashboard_collapsed_usage_renders_weekly_only_plan(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=None,
            five_hour_resets_at=None,
            seven_day_pct=12.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )
    )
    async with _client(app) as c:
        response = await c.get("/")
    assert '<b class="mini-pct ">12%</b>' in response.text
    assert '<span class="mini-7d">7d</span>' in response.text


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
            last_role="agent",
            question="Should I push both commits?",
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
    # Trailing agent question surfaced separately.
    assert 'class="card-question"' in r.text
    assert "Should I push both commits?" in r.text


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
    # Live-stream recovery hook remains present without a footer below the composer.
    assert "visibilitychange" in dash.text
    assert "streamStale" in dash.text
    assert 'class="build"' not in dash.text
    # Dashboard HTML must not be cached, so a deploy's inline JS actually lands.
    assert dash.headers["cache-control"] == "no-cache"


async def test_markdown_links_reject_unsafe_schemes_and_attribute_breakout(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.get("/")
    start = response.text.index("function mdEscape")
    end = response.text.index("// Connection hygiene", start)
    renderer = response.text[start:end]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main id=output></main>")
        await page.add_script_tag(content=renderer)
        rendered = await page.evaluate(
            """inputs => inputs.map(input => {
                const output = document.querySelector('#output');
                output.innerHTML = mdToHtml(input);
                const link = output.querySelector('a');
                return {
                    html: output.innerHTML,
                    href: link && link.getAttribute('href'),
                    attributes: link && Array.from(link.attributes, attr => attr.name),
                };
            })""",
            [
                "[x](javascript:alert(1))",
                "[x](data:text/html,&lt;script&gt;alert(1)&lt;/script&gt;)",
                "[x](vbscript:msgbox(1))",
                "[x](https://e.com/\"onmouseover=alert(1))",
                "[x](https://e.com)",
            ],
        )
        await browser.close()

    for result in rendered[:3]:
        assert result["href"] is None
        assert "<a " not in result["html"]
    quote_result = rendered[3]
    assert quote_result["href"] == 'https://e.com/"onmouseover=alert(1'
    assert quote_result["attributes"] == ["href", "target", "rel"]
    assert " onmouseover=" not in quote_result["html"]
    assert rendered[4]["href"] == "https://e.com"
    assert '<a href="https://e.com" target="_blank" rel="noopener">x</a>' in rendered[4]["html"]


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


async def test_clipboard_screenshot_attaches_to_chat_composer(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        response = await client.get("/")

    marker = "// Chat composers: paste clipboard screenshots"
    start = response.text.index(marker)
    end = response.text.index("// Enter submits", start)
    script = response.text[start:end]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(
            '<form><textarea name="message"></textarea>'
            '<input type="file" name="images" multiple></form>'
        )
        await page.add_script_tag(content=script)
        result = await page.evaluate(
            """async () => {
                const transfer = new DataTransfer();
                transfer.items.add(new File(
                    [new Uint8Array([137, 80, 78, 71])], 'screenshot.png',
                    {type: 'image/png'}
                ));
                const textarea = document.querySelector('textarea');
                const event = new ClipboardEvent('paste', {
                    clipboardData: transfer, bubbles: true, cancelable: true
                });
                textarea.dispatchEvent(event);
                const input = document.querySelector('input[name="images"]');
                return {
                    prevented: event.defaultPrevented,
                    files: input.files.length,
                    name: input.files[0].name,
                    previews: document.querySelectorAll('.paste-preview').length,
                    count: document.querySelector('.paste-count').textContent,
                };
            }"""
        )
        await browser.close()

    assert result == {
        "prevented": True,
        "files": 1,
        "name": "screenshot.png",
        "previews": 1,
        "count": "1 image attached",
    }


async def test_session_autoscroll_follows_successful_send_but_not_unrelated_swaps(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    marker = "// Open at the newest message."
    marker_at = response.text.index(marker)
    start = response.text.rfind("<script>", 0, marker_at) + len("<script>")
    end = response.text.index("</script>", start)
    script = response.text[start:end]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 800, "height": 500})
        await page.set_content(
            '<div class="transcript" style="height:2400px"></div>'
            '<div id="tool-activity"></div><div id="inject-result"></div>'
            '<form class="inject-form"></form>'
        )
        await page.add_script_tag(content=script)
        # Let the page-load scroll's animation frame finish before counting
        # scrolls caused by later HTMX events.
        await page.evaluate("() => new Promise(requestAnimationFrame)")
        await page.evaluate("window.scrollTo(0, 0)")
        result = await page.evaluate(
            """async () => {
                let calls = 0;
                const realScrollTo = window.scrollTo.bind(window);
                window.scrollTo = () => { calls += 1; };
                const swap = (target, mutate) => {
                    target.dispatchEvent(new CustomEvent('htmx:beforeSwap', {bubbles: true}));
                    if (mutate) mutate();
                    target.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true}));
                };
                swap(document.querySelector('#tool-activity'));
                const afterActivity = calls;
                swap(document.querySelector('.transcript'));
                const afterTranscript = calls;
                const form = document.querySelector('.inject-form');
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                await new Promise(requestAnimationFrame);
                const afterSend = calls;
                swap(document.querySelector('.transcript'));
                await new Promise(requestAnimationFrame);
                const afterSentTranscript = calls;
                await new Promise(resolve => setTimeout(resolve, 400));
                const afterViewportSettle = calls;
                const status = document.querySelector('#inject-result');
                swap(status, () => { status.style.height = '80px'; });
                await new Promise(requestAnimationFrame);
                const afterSendingStatus = calls;
                swap(status);
                await new Promise(requestAnimationFrame);
                const afterUnchangedStatus = calls;
                window.scrollTo = realScrollTo;
                return {
                    afterActivity,
                    afterTranscript,
                    afterSend,
                    afterSentTranscript,
                    afterViewportSettle,
                    afterSendingStatus,
                    afterUnchangedStatus,
                };
            }"""
        )
        await browser.close()

    assert result == {
        "afterActivity": 0,
        "afterTranscript": 0,
        "afterSend": 1,
        "afterSentTranscript": 2,
        "afterViewportSettle": 2,
        "afterSendingStatus": 3,
        "afterUnchangedStatus": 3,
    }


async def test_mobile_session_composer_is_compact():
    css = (
        Path(__file__).parents[1] / "src/agentdeck/web/static/app.css"
    ).read_text()
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section>
        <form class="inject-form">
          <label for="inject-message">Message</label>
          <textarea id="inject-message" rows="3"></textarea>
          <label class="image-picker">
            ＋ Attach image <span class="paste-hint">or paste screenshot</span>
          </label>
          <input class="image-input" type="file">
          <div class="composer-actions"><span>Enter to send</span><button>Send</button></div>
        </form>
      </section></main></body>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 412, "height": 800})
        await page.set_content(html)
        metrics = await page.evaluate(
            """() => {
                const form = document.querySelector('.inject-form');
                const textarea = form.querySelector('textarea');
                const picker = form.querySelector('.image-picker');
                const actions = form.querySelector('.composer-actions');
                const main = document.querySelector('main');
                return {
                    formHeight: form.getBoundingClientRect().height,
                    textareaHeight: textarea.getBoundingClientRect().height,
                    controlsAligned: Math.abs(
                        picker.getBoundingClientRect().top - actions.getBoundingClientRect().top
                    ) < 2,
                    hintHidden: getComputedStyle(actions.querySelector('span')).display,
                    pasteHintHidden: getComputedStyle(form.querySelector('.paste-hint')).display,
                    keyboardGap: main.getBoundingClientRect().bottom
                        - form.getBoundingClientRect().bottom,
                };
            }"""
        )
        await browser.close()

    assert metrics["formHeight"] < 145
    assert metrics["textareaHeight"] == 58
    assert metrics["controlsAligned"]
    assert metrics["hintHidden"] == "none"
    assert metrics["pasteHintHidden"] == "none"
    assert metrics["keyboardGap"] == 0


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


def test_ask_user_question_renders_in_transcript(tmp_path):
    """An AskUserQuestion line is a tool_use with no text, so the generic
    tool-drop rule would hide it — but it carries the prompt in `question` and
    must stay visible so the choice the agent is waiting on shows in the view."""
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    events = [
        TranscriptEvent(
            seq=1,
            role="assistant",
            tool_name="AskUserQuestion",
            question="Which database should we use?",
            text=None,
        ),
    ]
    html = render_transcript_events(templates, events)
    assert "Which database should we use?" in html
    assert "ev-question" in html


def test_activity_label_fallback():
    from agentdeck.models import TranscriptEvent, detailed_activity_label
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
    # an unanswered AskUserQuestion is waiting on the user, not busy — even though
    # it is a tool_use and even right after it was written (streaming).
    ask = TranscriptEvent(seq=1, role="assistant", tool_name="AskUserQuestion", question="Pick?")
    assert activity_label(True, False, ask) is None
    assert activity_label(True, True, ask) is None

    command = TranscriptEvent(
        seq=2,
        role="assistant",
        tool_name="exec_command",
        tool_summary="cmd: uv run pytest tests/test_web.py",
    )
    path = TranscriptEvent(
        seq=3,
        role="assistant",
        tool_name="view_image",
        tool_summary="path: /tmp/screenshot.png",
    )
    assert detailed_activity_label("Using tools", command) == (
        "Running: uv run pytest tests/test_web.py"
    )
    assert detailed_activity_label("Using tools", path) == "Accessing: /tmp/screenshot.png"
    assert detailed_activity_label("Working", command) == "Working"


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


def test_active_sessions_keep_stable_relative_order(tmp_path):
    from datetime import UTC, datetime, timedelta

    from agentdeck.state import AppState

    state = AppState()
    now = datetime.now(UTC)
    first = Session(
        key="codex:test:first",
        account_key="codex:test",
        session_id="first",
        status=SessionStatus.LIVE,
        thinking=True,
        last_activity=now,
    )
    second = Session(
        key="codex:test:second",
        account_key="codex:test",
        session_id="second",
        status=SessionStatus.LIVE,
        thinking=True,
        last_activity=now - timedelta(seconds=1),
    )
    state.update_session(first)
    state.update_session(second)
    assert [session.session_id for session in state.visible_sessions()] == [
        "first",
        "second",
    ]

    # A new event makes the second chat most recent, but active cards do not
    # fight for the top spot on every transcript write.
    state.update_session(
        Session(**{**second.__dict__, "last_activity": now + timedelta(seconds=1)})
    )
    assert [session.session_id for session in state.visible_sessions()] == [
        "first",
        "second",
    ]


def test_visible_idle_sessions_are_not_penalized_in_sort(tmp_path):
    from datetime import UTC, datetime, timedelta

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    now = datetime.now(UTC)
    state.update_session(
        Session(
            key="codex:test:live",
            account_key="codex:test",
            session_id="live",
            status=SessionStatus.LIVE,
            last_activity=now - timedelta(minutes=3),
            show_when_idle=True,
        )
    )
    state.update_session(
        Session(
            key="codex:test:idle",
            account_key="codex:test",
            session_id="idle",
            status=SessionStatus.IDLE,
            last_activity=now,
            show_when_idle=True,
        )
    )

    keys = [s.session_id for s in state.visible_sessions()]
    assert keys.index("idle") < keys.index("live")


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
    assert 'data-session-key="claude_code:test:think1"' in r.text
    assert 'data-working="1"' in r.text


async def test_subagent_count_renders_on_card_and_detail_header(tmp_path):
    app = _app_with_state(tmp_path)
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.subagent_count = 2

    async with _client(app) as client:
        dashboard = await client.get("/")
        detail = await client.get("/sessions/claude_code:test:sid1")

    assert "2 sub-agents" in dashboard.text
    assert 'title="This chat is running spawned agents"' in dashboard.text
    assert "2 sub-agents" in detail.text


async def test_dashboard_marks_chats_that_recently_stopped_working(tmp_path):
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
    async with _client(app) as client:
        response = await client.get("/")

    marker = "// Keep the tab useful as a passive monitor."
    start = response.text.index(marker)
    end = response.text.index("</script>", start)
    script = response.text[start:end]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(
            '<title>agentdeck</title><div id="sessions"><div class="session-list">'
            '<a class="session" data-session-key="quiet" data-working="0">'
            '<div class="session-main">quiet</div></a>'
            '<a class="session" data-session-key="busy" data-working="1">'
            '<div class="session-main">busy<span class="thinking-badge">working</span></div>'
            '<span class="dot thinking"></span></a>'
            "</div></div>"
        )
        await page.add_script_tag(content=script)
        assert await page.title() == "(1) agentdeck"

        await page.locator('[data-session-key="busy"]').evaluate(
            "card => card.setAttribute('data-working', '0')"
        )
        await page.evaluate("window.agentdeckRefreshWorkState()")
        result = await page.evaluate(
            """() => ({
                title: document.title,
                first: document.querySelector('.session-list').firstElementChild.dataset.sessionKey,
                marked: document.querySelector('[data-session-key="busy"]')
                    .classList.contains('recently-stopped'),
                badge: document.querySelector(
                    '[data-session-key="busy"] .recently-stopped-badge'
                ).textContent,
                thinkingBadge: !!document.querySelector(
                    '[data-session-key="busy"] .thinking-badge'
                ),
            })"""
        )
        await browser.close()

    assert result == {
        "title": "★ (0) agentdeck",
        "first": "busy",
        "marked": True,
        "badge": "★ just finished",
        "thinkingBadge": False,
    }


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


async def test_provider_can_keep_idle_sessions_visible(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="codex:test:idle1",
            account_key="codex:test",
            session_id="idle1",
            status=SessionStatus.IDLE,
            title="A Quiet Codex Chat",
            show_when_idle=True,
        )
    )
    async with _client(app) as c:
        listing = await c.get("/partials/sessions")
    assert "A Quiet Codex Chat" in listing.text


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


async def test_session_detail_uses_responsive_split_view(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    assert 'class="session-page"' in response.text
    assert 'class="session-layout"' in response.text
    assert 'class="session-sidebar" aria-label="All sessions"' in response.text
    assert 'class="session-detail" aria-label="Selected chat"' in response.text
    assert 'aria-current="page"' in response.text

    css = (
        Path(__file__).parents[1] / "src/agentdeck/web/static/app.css"
    ).read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        await page.set_content(html)
        desktop = await page.evaluate(
            """() => ({
                sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
                columns: getComputedStyle(document.querySelector('.session-layout'))
                    .gridTemplateColumns,
                detailOverflow: getComputedStyle(document.querySelector('.session-detail'))
                    .overflowY,
                back: getComputedStyle(document.querySelector('.back')).display,
            })"""
        )
        await page.set_viewport_size({"width": 800, "height": 800})
        mobile = await page.evaluate(
            """() => ({
                sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
                layout: getComputedStyle(document.querySelector('.session-layout')).display,
                detailOverflow: getComputedStyle(document.querySelector('.session-detail'))
                    .overflowY,
                back: getComputedStyle(document.querySelector('.back')).display,
            })"""
        )
        await browser.close()

    assert desktop["sidebar"] == "block"
    assert len(desktop["columns"].split()) == 2
    assert desktop["detailOverflow"] == "auto"
    assert desktop["back"] == "none"
    assert mobile == {
        "sidebar": "none",
        "layout": "block",
        "detailOverflow": "visible",
        "back": "flex",
    }


async def test_session_detail_renders_ask_user_question(tmp_path):
    """End-to-end: an AskUserQuestion line (a tool_use with no text block) is
    written to the transcript file and must survive the JSONL → parse → HTTP
    render path — the choice the agent is waiting on shows on the detail page.
    Regression for the viewer silently dropping it as generic tool noise."""
    import json

    proj = tmp_path / "projects" / "-tmp"
    proj.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"role": "user", "content": "set up the db"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "AskUserQuestion",
                        "input": {
                            "questions": [
                                {
                                    "question": "Which database should we use?",
                                    "header": "DB",
                                    "options": [{"label": "Postgres", "description": "..."}],
                                }
                            ]
                        },
                    }
                ],
            },
        },
    ]
    (proj / "ask1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig

    config = AppConfig(
        history=HistoryConfig(enabled=False),
        accounts=[AccountConfig(provider="claude_code", label="test", config_dir=str(tmp_path))],
    )
    app = create_app(config)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:ask1",
            account_key="claude_code:test",
            session_id="ask1",
            status=SessionStatus.LIVE,
            title="DB setup",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:ask1")
    assert r.status_code == 200
    assert "Which database should we use?" in r.text  # the choice question is visible
    assert "ev-question" in r.text  # rendered via the dedicated question block


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


async def test_session_sse_primes_desktop_list(tmp_path):
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)

    class FakeRequest:
        def __init__(self, application):
            self.app = application

        async def is_disconnected(self):
            return False

    key = "claude_code:test:sid1"
    gen = _session_stream(FakeRequest(app), key)
    try:
        usage = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        sessions = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    finally:
        await gen.aclose()

    assert "event: usage" in usage
    assert "event: sessions" in sessions
    assert "Hello World Session" in sessions
    assert 'aria-current="page"' in sessions
