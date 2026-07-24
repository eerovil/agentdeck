import asyncio
import base64
import json
import os
import re
import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient
from playwright.async_api import async_playwright

from agentdeck.app import create_app
from agentdeck.assistant import AssistantInsight, AssistantView
from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
from agentdeck.git_context import GitContext, PullRequestContext
from agentdeck.host_stats import HostStats
from agentdeck.models import (
    Account,
    Capability,
    Session,
    SessionStatus,
    SubagentProgress,
    TranscriptEvent,
    UsageSnapshot,
)
from agentdeck.providers.claude_code.provider import worker_type
from agentdeck.triage import Verdict
from agentdeck.web.routes_pages import _pr_reference_text


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
            {
                "type": "user",
                "timestamp": "2026-07-15T07:41:00Z",
                "message": {"role": "user", "content": "first question"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-07-15T07:42:00Z",
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


def _session_card(html: str, session_key: str) -> str:
    return html.split(f'data-session-key="{session_key}"', 1)[1].split("</a>", 1)[0]


def test_session_list_context_owns_full_page_and_fragment_keys(tmp_path):
    from agentdeck.web.render import session_list_context

    app = _app_with_state(tmp_path)
    state = app.state.app_state
    presentation = state.session_presentation()
    context = session_list_context(
        app.state.accounts,
        presentation,
        injector=app.state.injector,
        assistant=app.state.assistant,
        selected_session_key="claude_code:test:sid1",
    )

    assert set(context) == {
        "sessions",
        "children_of",
        "labels",
        "selected_session_key",
        "deckhand_status",
        "queue_summaries",
        "assistant",
        "assistant_sessions",
        "working_count",
    }
    assert context["sessions"] == presentation.top_level
    assert context["selected_session_key"] == "claude_code:test:sid1"
    assert context["assistant_sessions"] is state.sessions


async def test_healthz(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_dashboard_renders_usage_and_session(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=23.0,
            memory_pct=61.0,
            memory_used_bytes=10,
            memory_total_bytes=20,
            sampled_at=datetime.now(UTC),
        )
    )
    async with _client(app) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "Hello World Session" in r.text
    assert "42%" in r.text
    assert "CPU" in r.text
    assert "23%" in r.text
    assert "MEM" in r.text
    assert "61%" in r.text
    assert 'class="acct host-acct"' in r.text
    # The "hide closed" / "hide done" filter toggles are present.
    assert 'id="hide-closed"' in r.text
    assert 'id="hide-done"' in r.text
    assert 'id="assistant-panel"' in r.text
    assert "Attention triage" in r.text


async def test_new_issue_link_is_visible_in_shared_topbar(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        dashboard = await client.get("/")
        session = await client.get("/sessions/claude_code:test:sid1")

    expected = 'href="https://github.com/eerovil/agentdeck/issues/new"'
    for response in (dashboard, session):
        assert response.status_code == 200
        assert expected in response.text
        assert 'aria-label="Create AgentDeck GitHub issue"' in response.text
        assert 'class="github-mark"' in response.text

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = dashboard.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.locator(".new-issue-link").evaluate(
            """link => {
              const rect = link.getBoundingClientRect();
              return {
                visible: rect.width > 0 && rect.height > 0,
                iconVisible: !!link.querySelector('.github-mark')
                  && link.querySelector('.github-mark').getBoundingClientRect().width > 0,
                left: rect.left,
                right: rect.right,
                viewport: document.documentElement.clientWidth,
                overflow: document.documentElement.scrollWidth
                  - document.documentElement.clientWidth,
                topbarPosition: getComputedStyle(link.closest('.topbar')).position,
              };
            }"""
        )
        await browser.close()

    assert metrics["visible"]
    assert metrics["iconVisible"]
    assert metrics["left"] >= 0
    assert metrics["right"] <= metrics["viewport"]
    assert metrics["overflow"] <= 1
    assert metrics["topbarPosition"] == "sticky"


async def test_deckhand_shows_active_working_count(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    state = app.state.app_state
    session = state.sessions["claude_code:test:sid1"]
    state.update_session(replace(session, thinking=True))

    async with _client(app) as client:
        response = await client.get("/")

    assert 'class="assistant-working"' in response.text
    assert "1 working" in response.text


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


async def test_orchestration_assistant_fits_mobile_and_desktop(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(
        state="ready",
        summary="Two agents may need coordination before the next deploy.",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="waiting",
                headline="Storm · Elasticsearch · PR #255 is open and awaiting review",
                detail="This agent is waiting for an explicit architecture decision.",
            ),
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="coordination",
                headline="Avoid overlapping deploys",
                detail="Another active session is changing the same service.",
            ),
        ),
    )
    async with _client(app) as client:
        response = await client.get("/")

    assert "This agent is waiting for an explicit architecture decision." not in response.text
    assert "Another active session is changing the same service." not in response.text
    assert response.text.count('data-agentdeck-action="open_deckhand_chat"') == 2

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    action_script = (static_dir / "action_timing.js").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        await page.add_script_tag(content=action_script)
        await page.evaluate(
            """() => {
              // set_content has an opaque about:blank origin; make this synthetic
              // click same-origin so it exercises the production navigation gate.
              document.querySelector('.assistant-insight-link').href = location.href;
              document.addEventListener('click', event => {
                if (event.target.closest('.assistant-insight-link')) event.preventDefault();
              }, {once: true});
            }"""
        )
        await page.locator(".assistant-insight-link").first.click()
        opening = await page.locator(".assistant-insight-link").first.evaluate(
            """link => ({
              opening: link.classList.contains('opening'),
              busy: link.getAttribute('aria-busy'),
              label: getComputedStyle(link, '::after').content,
              timing: JSON.parse(JSON.stringify(
                Array.from(AgentDeckActionTiming.records.values())[0]
              )),
            })"""
        )
        sizes = []
        for width in (320, 760):
            await page.set_viewport_size({"width": width, "height": 800})
            sizes.append(
                await page.evaluate(
                    """() => {
                        const panel = document.querySelector('#assistant-panel');
                        const rect = panel.getBoundingClientRect();
                        return {
                            left: rect.left,
                            right: rect.right,
                            viewport: document.documentElement.clientWidth,
                            scroll: document.documentElement.scrollWidth,
                            insights: panel.querySelectorAll('.assistant-insight').length,
                        };
                    }"""
                )
            )
        await browser.close()

    assert opening["opening"] is True
    assert opening["busy"] == "true"
    assert opening["label"] == '"Opening chat…"'
    assert opening["timing"]["action"] == "open_deckhand_chat"
    assert (
        opening["timing"]["epochMarks"]["acknowledged"]
        - opening["timing"]["epochMarks"]["interaction"]
        < 16
    )
    for size in sizes:
        assert size["left"] >= 0
        assert size["right"] <= size["viewport"]
        assert size["scroll"] <= size["viewport"]
        assert size["insights"] == 2


async def test_orchestration_assistant_item_can_be_marked_handled(tmp_path):
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    assistant.view = AssistantView(
        state="ready",
        summary="One item needs attention.",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="coordination",
                headline="Choose one owner",
                detail="Two chats overlap.",
            ),
        ),
    )
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    evidence = assistant._evidence_signature(session, None, None)
    assistant._signatures["claude_code:test:sid1"] = evidence

    async with _client(app) as client:
        response = await client.post(
            "/assistant/handle", data={"session_key": "claude_code:test:sid1"}
        )

    assert response.status_code == 200
    assert 'class="assistant-insight-link"' not in response.text
    assert 'aria-label="Most recently completed Deckhand item"' in response.text
    assert "Choose one owner" in response.text
    assert "Undo" in response.text
    assert "Nothing needs your attention right now." in response.text
    assert assistant.dismissals.insight_signature("claude_code:test:sid1") == evidence

    async with _client(app) as client:
        handled_page = await client.get("/sessions/claude_code:test:sid1")
    assert "Two chats overlap." not in handled_page.text
    assert "Undo marking Choose one owner done" in handled_page.text

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        handled = await page.locator(".assistant-handled-item").bounding_box()
        panel = await page.locator("#assistant-panel").bounding_box()
        await browser.close()
    assert handled is not None and panel is not None
    assert handled["x"] >= panel["x"]
    assert handled["x"] + handled["width"] <= panel["x"] + panel["width"]

    async with _client(app) as client:
        restored = await client.post(
            "/assistant/unhandle", data={"session_key": "claude_code:test:sid1"}
        )

    assert restored.status_code == 200
    assert 'class="assistant-insight-link"' in restored.text
    assert 'aria-label="Most recently completed Deckhand item"' not in restored.text
    assert assistant.dismissals.insight_keys() == []

    async with _client(app) as client:
        restored_page = await client.get("/sessions/claude_code:test:sid1")
    assert "Two chats overlap." in restored_page.text
    assert "Mark Choose one owner done" in restored_page.text


async def test_transcript_bottom_deckhand_done_button_toggles_with_insight(tmp_path):
    # Feature: a Deckhand done/undo control at the bottom of the transcript,
    # reusing /assistant/handle + /assistant/unhandle, shown only when there is a
    # live insight to dismiss (or a standing dismissal to undo), and toggling in
    # lockstep with the sidebar card via its own SSE topic.
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    key = "claude_code:test:sid1"

    # No insight yet → the bottom bar renders as an empty (hidden) container, no
    # button, and it is wired to a real change signal, not a blind poll.
    assistant.view = AssistantView(state="ready", summary="", insights=())
    async with _client(app) as client:
        empty = await client.get(f"/sessions/{key}")
    assert 'id="deckhand-done-bar"' in empty.text
    assert 'sse-swap="assistant-session-done"' in empty.text
    assert "Deckhand done" not in empty.text
    assert 'hx-trigger="every' not in empty.text  # no self-polling on the control

    # A live insight → the bottom bar offers "Deckhand done" wired to the existing
    # handle endpoint for this session.
    assistant.view = AssistantView(
        state="ready",
        summary="One item needs attention.",
        insights=(
            AssistantInsight(
                session_key=key,
                kind="coordination",
                headline="Choose one owner",
                detail="Two chats overlap.",
            ),
        ),
    )
    session = app.state.app_state.sessions[key]
    assistant._signatures[key] = assistant._evidence_signature(session, None, None)

    async with _client(app) as client:
        live = await client.get(f"/sessions/{key}")
    bar = live.text.split('id="deckhand-done-bar"', 1)[1].split("</div>", 1)[0]
    assert 'hx-post="/assistant/handle"' in bar
    assert f'name="session_key" value="{key}"' in bar
    assert "✓ Deckhand done" in bar
    assert "Undo Deckhand done" not in bar

    # Mark it done → the SSE-driven control flips to Undo (unhandle), matching the
    # sidebar card, and the page's own bottom bar renders the same on reload.
    async with _client(app) as client:
        await client.post("/assistant/handle", data={"session_key": key})
        done_page = await client.get(f"/sessions/{key}")
    done_bar = done_page.text.split('id="deckhand-done-bar"', 1)[1].split("</div>", 1)[0]
    assert 'hx-post="/assistant/unhandle"' in done_bar
    assert "↩ Undo Deckhand done" in done_bar
    assert "✓ Deckhand done" not in done_bar

    # Undo restores the dismissable state.
    async with _client(app) as client:
        await client.post("/assistant/unhandle", data={"session_key": key})
        back_page = await client.get(f"/sessions/{key}")
    back_bar = back_page.text.split('id="deckhand-done-bar"', 1)[1].split("</div>", 1)[0]
    assert 'hx-post="/assistant/handle"' in back_bar
    assert "✓ Deckhand done" in back_bar


async def test_manual_deckhand_check_explains_that_unchanged_evidence_skips_luna(tmp_path):
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True

    async with _client(app) as client:
        response = await client.post("/assistant/refresh")

    assert response.status_code == 202
    assert assistant._manual_refresh_pending is True
    assert assistant._force is True
    assert "Checking current evidence…" in response.text
    assert "Check now" in response.text
    assert "Luna runs only when something changed" in response.text


async def test_host_usage_fits_collapsed_mobile_header(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.accounts.extend(
        [
            Account("claude_code:alt", "claude_code", "alt", tmp_path),
            Account("codex:codex", "codex", "codex", tmp_path),
        ]
    )
    app.state.app_state.set_host_stats(
        HostStats(
            cpu_pct=23.0,
            memory_pct=61.0,
            memory_used_bytes=20 * 1024**3,
            memory_total_bytes=32 * 1024**3,
            sampled_at=datetime.now(UTC),
        )
    )
    async with _client(app) as client:
        response = await client.get("/")

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 412, "height": 800})
        await page.set_content(html)
        metrics = []
        for width in (320, 360, 412):
            await page.set_viewport_size({"width": width, "height": 800})
            metrics.append(
                await page.evaluate(
                    """() => {
                        const usage = document.querySelector('.usage-mini');
                        const host = usage.querySelector('.mini-host');
                        return {
                            height: usage.getBoundingClientRect().height,
                            usageRight: usage.getBoundingClientRect().right,
                            hostRight: host.getBoundingClientRect().right,
                            hostDisplay: getComputedStyle(host).display,
                            caretDisplay: getComputedStyle(
                              usage.querySelector('.mini-caret')
                            ).display,
                            clientWidth: usage.clientWidth,
                            scrollWidth: usage.scrollWidth,
                        };
                    }"""
                )
            )
        await browser.close()

    for size in metrics:
        assert size["hostRight"] < size["usageRight"]
        assert size["scrollWidth"] <= size["clientWidth"]
        assert size["height"] < 40
        assert size["hostDisplay"] == "flex"
        assert size["caretDisplay"] == "none"


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


async def test_session_card_shows_deckhand_status_pill(tmp_path):
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    # One row per resolution branch:
    #   live      — a live attention insight (shows even while thinking)
    #   finished  — only a durable finished verdict, resting (verdict pill)
    #   done      — manually dismissed (handled) resting chat (done pill)
    #   merged    — its PR merged, derived from PR status (merged pill)
    #   working   — a durable verdict but mid-turn (verdict suppressed, no pill)
    #   idle      — resting, never classified ("?")
    #   asking    — a pending question (waiting, straight off the session)
    #   subagent  — delegated/background chat (never gets a pill)
    sessions = {
        "live": dict(last_role="agent", thinking=False),
        "finished": dict(last_role="agent", thinking=False),
        "done": dict(last_role="agent", thinking=False),
        "merged": dict(last_role="agent", thinking=False),
        "working": dict(last_role="agent", thinking=True),
        "idle": dict(last_role="agent", thinking=False),
        "asking": dict(last_role="agent", thinking=False, question="Which option?"),
        "subagent": dict(last_role="agent", thinking=False, is_delegated=True),
    }
    for sid, extra in sessions.items():
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:{sid}",
                account_key="claude_code:test",
                session_id=sid,
                status=SessionStatus.LIVE,
                title=f"Session {sid}",
                **extra,
            )
        )
    assistant._verdicts = {
        "claude_code:test:finished": ("sig", Verdict("finished", "Opened a PR", "review it")),
        "claude_code:test:working": ("sig", Verdict("finished", "Mid-turn work", "review it")),
        # Even with a verdict, a delegated chat must never show a pill.
        "claude_code:test:subagent": ("sig", Verdict("blocked", "Subagent internal step", "")),
    }
    # A manually dismissed chat resolves to a non-attention "done" pill.
    assistant.dismissals.dismiss_insight(
        "claude_code:test:done",
        "sig",
        AssistantInsight("claude_code:test:done", "finished", "All shipped and verified", "d"),
    )
    # A merged PR resolves to a non-attention "merged" pill, derived from PR status.
    assistant.contexts = {
        "claude_code:test:merged": GitContext(
            "acme/app", "feat", False,
            (PullRequestContext("acme/app", 42, "Ship it", "u", "merged"),),
        ),
    }
    assistant.view = AssistantView(
        state="ready",
        summary="One agent needs your attention.",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:live",
                kind="stalled",
                headline="No progress for 12 min",
                detail="It may be hung.",
            ),
        ),
    )
    async with _client(app) as c:
        r = await c.get("/")
    # Live insight -> mapped to a blocked pill, headline on hover.
    assert 'class="dh-pill dh-blocked"' in r.text
    assert 'title="Deckhand: No progress for 12 min"' in r.text
    # Durable finished verdict on a resting chat, with its summary on hover.
    assert 'class="dh-pill dh-finished"' in r.text
    assert 'title="Deckhand: Opened a PR"' in r.text
    # Manually dismissed chat -> non-attention done pill.
    assert 'class="dh-pill dh-done"' in r.text
    assert 'title="Deckhand: All shipped and verified"' in r.text
    # Merged PR -> non-attention merged pill, derived from live PR status.
    assert 'class="dh-pill dh-merged"' in r.text
    assert 'title="Deckhand: Its PR was merged."' in r.text
    # Never-classified resting chat -> question-mark pill (not a fabricated verdict).
    assert 'class="dh-pill dh-unknown"' in r.text
    assert 'title="Deckhand has not classified this chat yet"' in r.text
    # A pending question always resolves to waiting, regardless of insight state.
    assert 'class="dh-pill dh-waiting"' in r.text
    assert 'title="Deckhand: the agent asked you a question"' in r.text
    # The working chat's stored verdict is suppressed mid-turn: its headline never appears.
    assert 'title="Deckhand: Mid-turn work"' not in r.text
    # The retired "review" pill never renders.
    assert "dh-review" not in r.text
    # A delegated/subagent chat never shows a pill, even with a verdict.
    assert "Session subagent" in r.text  # the card itself renders
    assert 'title="Deckhand: Subagent internal step"' not in r.text


async def test_hide_done_toggle_hides_done_rows_and_persists(tmp_path):
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    # Two resting rows: one manually dismissed (done pill), one never-classified
    # (unknown "?" pill). Only the done row should hide when the toggle is on.
    for sid in ("done", "keep"):
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:{sid}",
                account_key="claude_code:test",
                session_id=sid,
                status=SessionStatus.LIVE,
                title=f"Session {sid}",
                last_role="agent",
                thinking=False,
            )
        )
    assistant.dismissals.dismiss_insight(
        "claude_code:test:done",
        "sig",
        AssistantInsight("claude_code:test:done", "finished", "All shipped", "d"),
    )
    async with _client(app) as c:
        r = await c.get("/")
    assert 'class="dh-pill dh-done"' in r.text

    done_sel = '#sessions a.session[data-session-key="claude_code:test:done"]'
    keep_sel = '#sessions a.session[data-session-key="claude_code:test:keep"]'
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 390, "height": 800})

        # Serve the rendered page from a real http origin so the localStorage the
        # filter writes is readable (set_content's about:blank origin denies it).
        async def handle(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=r.text)
            else:
                await route.abort()

        await page.route("**/*", handle)
        await page.goto("http://localhost/")
        # Default: the toggle is off and both rows are visible.
        assert await page.locator("#hide-done").get_attribute("aria-pressed") == "false"
        assert await page.locator(done_sel).is_visible()
        assert await page.locator(keep_sel).is_visible()
        # Toggle on -> only the done row hides.
        await page.locator("#hide-done").click()
        assert await page.locator(done_sel).is_hidden()
        assert await page.locator(keep_sel).is_visible()
        # Choice persists per-device and is reflected on the button.
        assert await page.evaluate(
            "() => localStorage.getItem('agentdeck.hideDone')"
        ) == "1"
        assert await page.locator("#hide-done").get_attribute("aria-pressed") == "true"
        # A live list swap must not un-hide it: the filter re-applies on afterSwap.
        await page.evaluate(
            "() => document.getElementById('sessions')"
            ".dispatchEvent(new CustomEvent('htmx:afterSwap'))"
        )
        assert await page.locator(done_sel).is_hidden()
        # Toggle back off -> the done row returns.
        await page.locator("#hide-done").click()
        assert await page.locator(done_sel).is_visible()
        await browser.close()


async def test_session_detail_sidebar_shows_deckhand_pills(tmp_path):
    # Regression: the chat detail page built its context by hand and omitted
    # deckhand_status, so the sidebar flashed "?" for real verdicts on initial
    # HTTP load until SSE corrected it. The pill must be right on first render.
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1", account_key="claude_code:test",
            session_id="sid1", status=SessionStatus.LIVE, title="Sid1",
            last_role="agent", thinking=False,
        )
    )
    app.state.assistant._verdicts = {
        "claude_code:test:sid1": ("sig", Verdict("finished", "All shipped", "")),
    }
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:sid1")
    assert 'class="dh-pill dh-finished"' in r.text
    assert 'title="Deckhand: All shipped"' in r.text


async def test_deckhand_pill_fits_narrow_mobile(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:m1", account_key="claude_code:test",
            session_id="m1", status=SessionStatus.LIVE,
            title="A reasonably long session title that might wrap on a phone",
            last_role="agent", thinking=False,
            issue_status="open", issue_status_kind="open",
            cwd=Path("/home/eero/some/deep/project/dir"),
            kind="chat", context_tokens=123456,
        )
    )
    app.state.assistant._verdicts = {
        "claude_code:test:m1": ("sig", Verdict("blocked", "Stuck on something", "")),
    }
    async with _client(app) as client:
        response = await client.get("/")
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        size = await page.evaluate(
            """() => {
              const pill = document.querySelector('.dh-pill');
              const card = pill && pill.closest('.session');
              return {
                pillVisible: !!pill && pill.getBoundingClientRect().width > 0,
                bodyOverflow: document.body.scrollWidth - document.body.clientWidth,
                cardOverflow: card ? card.scrollWidth - card.clientWidth : 0,
              };
            }"""
        )
        await browser.close()
    # The pill renders, and neither the page nor its card overflows at 320px.
    assert size["pillVisible"]
    assert size["bodyOverflow"] <= 1
    assert size["cardOverflow"] <= 1


async def test_deckhand_insight_pointer_focus_and_double_tap_lifecycle(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    for number in (1, 2):
        app.state.app_state.update_session(
            Session(
                key=f"claude_code:test:focus{number}",
                account_key="claude_code:test",
                session_id=f"focus{number}",
                status=SessionStatus.LIVE,
                title=f"Focus target {number}",
                last_role="agent",
                question="Ready to ship?",
            )
        )
    app.state.assistant.view = AssistantView(
        state="ready",
        summary="Two agents need your attention.",
        insights=tuple(
            AssistantInsight(
                session_key=f"claude_code:test:focus{number}",
                kind="waiting",
                headline=f"Agent {number} asked you a question",
                detail="Ready to ship?",
            )
            for number in (1, 2)
        ),
    )
    async with _client(app) as client:
        response = await client.get("/")
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    timing_js = (static_dir / "action_timing.js").read_text()
    focus_js = (static_dir / "deckhand_focus.js").read_text()
    # A <base> gives links a real origin while the rendered dashboard and shipped
    # scripts remain in-process.
    html = response.text.replace(
        "<head>", '<head><base href="http://agentdeck.test/">', 1
    )
    html = html.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 900, "height": 600})

        async def serve_chat(route):
            await route.fulfill(status=200, content_type="text/html", body="opened")

        await page.route("http://agentdeck.test/sessions/**", serve_chat)
        await page.set_content(html)
        await page.add_script_tag(content=timing_js)
        await page.add_script_tag(content=focus_js)
        links = page.locator(".assistant-insight-link")
        first = '.session[data-session-key="claude_code:test:focus1"]'
        second = '.session[data-session-key="claude_code:test:focus2"]'

        await links.nth(0).click()
        assert await page.locator(first).evaluate(
            "card => card.classList.contains('dh-focused')"
        )

        # The ring is state, not a 1.4-second animation timeout.
        await page.wait_for_timeout(1500)
        assert await page.locator(first).evaluate(
            "card => card.classList.contains('dh-focused')"
        )

        # Once the focused row has been visible, scrolling it out clears focus.
        await page.evaluate(
            """() => {
              var spacer = document.createElement('div');
              spacer.style.height = '2000px';
              document.body.appendChild(spacer);
              scrollTo(0, document.body.scrollHeight);
            }"""
        )
        await page.wait_for_function(
            "sel => !document.querySelector(sel).classList.contains('dh-focused')",
            arg=first,
        )
        await page.evaluate("scrollTo(0, 0)")

        # A different item moves the one allowed highlight to its own row.
        await links.nth(0).click()
        await links.nth(1).click()
        moved = await page.evaluate(
            "([first, second]) => ({"
            " first: document.querySelector(first).classList.contains('dh-focused'),"
            " second: document.querySelector(second).classList.contains('dh-focused')})",
            [first, second],
        )
        assert not moved["first"] and moved["second"]

        # Outside the gesture window, the next tap is another focus, not an open.
        await page.wait_for_timeout(650)
        before = page.url
        await links.nth(1).click()
        assert page.url == before
        assert await page.locator(second).evaluate(
            "card => card.classList.contains('dh-focused')"
        )

        # The same item tapped again promptly follows its normal chat link.
        # Dispatch directly so Playwright does not wait for smooth scrolling to
        # settle and accidentally turn this into a >600ms physical click.
        await links.nth(1).dispatch_event("click", {"button": 0, "detail": 1})
        await page.wait_for_url("**/sessions/claude_code:test:focus2")
        await browser.close()


async def test_deckhand_insight_keeps_link_and_mobile_activation_semantics(tmp_path):
    app = _app_with_state(tmp_path)
    app.state.assistant.config.enabled = True
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:focus1",
            account_key="claude_code:test",
            session_id="focus1",
            status=SessionStatus.LIVE,
            title="Focus target",
            last_role="agent",
            question="Ready to ship?",
        )
    )
    app.state.assistant.view = AssistantView(
        state="ready",
        summary="One agent needs your attention.",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:focus1",
                kind="waiting",
                headline="Asked you a question",
                detail="Ready to ship?",
            ),
        ),
    )
    async with _client(app) as client:
        response = await client.get("/")
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    focus_js = (static_dir / "deckhand_focus.js").read_text()
    html = response.text.replace(
        "<head>", '<head><base href="http://agentdeck.test/">', 1
    )
    html = html.replace("</head>", f"<style>{css}</style></head>")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()

        async def serve_chat(route):
            await route.fulfill(status=200, content_type="text/html", body="opened")

        # Enter is ordinary link activation and opens immediately.
        keyboard = await browser.new_page(viewport={"width": 900, "height": 600})
        await keyboard.route("http://agentdeck.test/sessions/**", serve_chat)
        await keyboard.set_content(html)
        await keyboard.add_script_tag(content=focus_js)
        await keyboard.locator(".assistant-insight-link").press("Enter")
        await keyboard.wait_for_url("**/sessions/claude_code:test:focus1")
        await keyboard.close()

        # Modified clicks remain unconsumed, preserving browser new-tab behavior.
        modified = await browser.new_page(viewport={"width": 900, "height": 600})
        await modified.set_content(html)
        await modified.add_script_tag(content=focus_js)
        prevented = await modified.locator(".assistant-insight-link").evaluate(
            """link => {
              var prevented;
              link.addEventListener('click', event => {
                prevented = event.defaultPrevented;
                event.preventDefault();
              }, {once: true});
              link.dispatchEvent(new MouseEvent('click', {
                bubbles: true, cancelable: true, button: 0, ctrlKey: true
              }));
              return prevented;
            }"""
        )
        assert not prevented
        await modified.close()

        # On the narrow touch surface, manipulation disables double-tap zoom and
        # two quick taps reliably become focus then navigation.
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, has_touch=True
        )
        touch = await context.new_page()
        await touch.route("http://agentdeck.test/sessions/**", serve_chat)
        await touch.set_content(html)
        await touch.add_script_tag(content=focus_js)
        link = touch.locator(".assistant-insight-link")
        assert await link.evaluate("node => getComputedStyle(node).touchAction") == "manipulation"
        await link.tap()
        assert await touch.locator(".session.dh-focused").count() == 1
        await link.tap()
        await touch.wait_for_url("**/sessions/claude_code:test:focus1")
        await context.close()
        await browser.close()


async def test_long_card_title_gets_full_width_three_line_layout(tmp_path):
    app = _app_with_state(tmp_path)
    long_title = (
        "store#2728 · Investigate refund validation failures and deploy the durable fix"
    )
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:sid1",
            account_key="claude_code:test",
            session_id="sid1",
            status=SessionStatus.LIVE,
            title=long_title,
            issue_url="https://github.com/example/store/issues/2728",
            deep_link="https://claude.ai/code/session_sid1",
            deep_link_label="open in claude.ai",
            last_prompt="Please deploy it",
        )
    )
    async with _client(app) as client:
        response = await client.get("/")

    assert response.text.index('class="session-actions"') < response.text.index(
        'class="session-main"'
    )
    assert f'class="card-title" title="{long_title}"' in response.text

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        metrics = await page.locator('.session[data-session-key="claude_code:test:sid1"]').evaluate(
            """card => {
              const title = card.querySelector('.card-title');
              const actions = card.querySelector('.session-actions');
              const cardRect = card.getBoundingClientRect();
              const titleRect = title.getBoundingClientRect();
              return {
                clamp: getComputedStyle(title).webkitLineClamp,
                titleWidth: titleRect.width,
                cardWidth: cardRect.width,
                actionsBottom: actions.getBoundingClientRect().bottom,
                titleTop: titleRect.top,
                scrollWidth: document.documentElement.scrollWidth,
                viewportWidth: document.documentElement.clientWidth,
              };
            }"""
        )
        await browser.close()

    assert metrics["clamp"] == "3"
    assert metrics["titleWidth"] > metrics["cardWidth"] * 0.8
    assert metrics["actionsBottom"] <= metrics["titleTop"]
    assert metrics["scrollWidth"] <= metrics["viewportWidth"]


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
    assert "/static/mobile_session_stack.js" in sw.text
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


async def test_push_client_assets_and_bell(tmp_path):
    # Issue #7 (#12): service worker push handlers, the opt-in bell, and the
    # client module are wired into the shell.
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw = await c.get("/sw.js")
        dash = await c.get("/")
        pushjs = await c.get("/static/push.js")
    # Service worker handles push + click, and caches the client module.
    assert "addEventListener('push'" in sw.text
    assert "addEventListener('notificationclick'" in sw.text
    assert "/static/push.js" in sw.text
    # The shell renders the (initially hidden) bell and loads the module.
    assert 'class="notif-bell"' in dash.text
    assert "/static/push.js" in dash.text
    # The client module talks to the backend endpoints.
    assert pushjs.status_code == 200
    assert "/push/public-key" in pushjs.text
    assert "/push/subscribe" in pushjs.text
    assert "/push/unsubscribe" in pushjs.text
    assert "streamStale" in dash.text
    assert 'class="build"' not in dash.text
    # Dashboard HTML must not be cached, so a deploy's inline JS actually lands.
    assert dash.headers["cache-control"] == "no-cache"


async def test_notification_click_always_routes_home_and_never_no_ops(tmp_path):
    # Issue #35: tapping a push notification must reliably surface the AgentDeck
    # front page, freshly loaded — never a deep-linked/stale chat view, and never
    # a silent no-op. Drive the *real* service-worker source under a mocked
    # ServiceWorkerGlobalScope so we test the shipped notificationclick handler,
    # not a hand-copied excerpt.
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw_source = (await c.get("/sw.js")).text
        dash = (await c.get("/")).text

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main></main>")
        results = await page.evaluate(
            """async (swSource) => {
                const handlers = {};
                const mockSelf = {
                    location: { origin: 'https://deck.example' },
                    addEventListener: (type, fn) => { handlers[type] = fn; },
                    skipWaiting: () => {},
                    registration: {},
                    clients: {},
                };
                const noop = () => Promise.resolve();
                const mockCaches = { open: noop, keys: () => Promise.resolve([]),
                                     match: () => Promise.resolve(null), delete: noop };
                // Execute the shipped worker with self/caches bound to the mocks.
                new Function('self', 'caches', swSource)(mockSelf, mockCaches);

                async function run(clients, dataUrl) {
                    const rec = { focused: [], navigated: [], posted: [], opened: [] };
                    mockSelf.clients = {
                        matchAll: async () => clients.map((c) => {
                            const client = {
                                url: c.url,
                                focus: async () => {
                                    if (c.focus === 'reject') throw new Error('focus failed');
                                    rec.focused.push(c.url);
                                },
                                postMessage: (m) => rec.posted.push(m),
                            };
                            if (c.navigate !== 'missing') {
                                client.navigate = async (u) => {
                                    if (c.navigate === 'reject') throw new Error('cannot navigate');
                                    rec.navigated.push(u);
                                    return client;
                                };
                            }
                            return client;
                        }),
                        openWindow: async (u) => { rec.opened.push(u); return {}; },
                    };
                    const ev = {
                        notification: { close() {}, data: { url: dataUrl } },
                        _p: null,
                        waitUntil(p) { this._p = p; },
                    };
                    handlers.notificationclick(ev);
                    await ev._p;
                    return rec;
                }

                const O = 'https://deck.example';
                return {
                    existing: await run([{ url: O + '/chat/x', navigate: true }], '/chat/x'),
                    none: await run([], '/chat/x'),
                    uncontrolled:
                        await run([{ url: O + '/chat/x', navigate: 'reject' }], '/chat/x'),
                    noNavigate:
                        await run([{ url: O + '/chat/x', navigate: 'missing' }], '/chat/x'),
                    crossOrigin: await run([
                        { url: 'https://evil.example/', navigate: true },
                        { url: O + '/chat/y', navigate: true },
                    ], '/chat/y'),
                };
            }""",
            sw_source,
        )
        await browser.close()

    home = "https://deck.example/"
    # An existing app window is focused and navigated *home* — the payload's chat
    # url is ignored, and no duplicate window is opened.
    assert results["existing"]["navigated"] == [home]
    assert results["existing"]["focused"] == ["https://deck.example/chat/x"]
    assert results["existing"]["opened"] == []
    assert results["existing"]["posted"] == []
    # No app window to reuse → open a fresh one at the front page.
    assert results["none"]["opened"] == [home]
    assert results["none"]["navigated"] == []
    # navigate() rejects (frozen/uncontrolled client): still focused, and the page
    # is asked to go home itself — never a no-op, never left on the stale view.
    assert results["uncontrolled"]["focused"] == ["https://deck.example/chat/x"]
    assert results["uncontrolled"]["navigated"] == []
    assert results["uncontrolled"]["posted"] == [{"type": "agentdeck:open-home"}]
    assert results["uncontrolled"]["opened"] == []
    # navigate() unsupported: same page-driven fallback.
    assert results["noNavigate"]["posted"] == [{"type": "agentdeck:open-home"}]
    assert results["noNavigate"]["opened"] == []
    # A cross-origin window is skipped; the same-origin app window is routed home.
    assert results["crossOrigin"]["navigated"] == [home]
    assert results["crossOrigin"]["opened"] == []

    # The page half of the fallback is wired in: the shell listens for the SW's
    # go-home message.
    assert "agentdeck:open-home" in dash
    assert "navigator.serviceWorker.addEventListener('message'" in dash


async def test_push_suppressed_only_when_a_window_is_foreground(tmp_path):
    # Issue #36: a push must not stack a notification when this device already has
    # the app open and visible, but must still notify when it's closed or
    # backgrounded. Drive the shipped push handler under a mocked worker scope.
    app = _app_with_state(tmp_path)
    async with _client(app) as c:
        sw_source = (await c.get("/sw.js")).text

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content("<main></main>")
        results = await page.evaluate(
            """async (swSource) => {
                const handlers = {};
                const shown = [];
                const mockSelf = {
                    location: { origin: 'https://deck.example' },
                    addEventListener: (type, fn) => { handlers[type] = fn; },
                    skipWaiting: () => {},
                    registration: {
                        showNotification: (title, opts) => { shown.push({ title, opts }); },
                    },
                    clients: {},
                };
                const noop = () => Promise.resolve();
                const mockCaches = { open: noop, keys: () => Promise.resolve([]),
                                     match: () => Promise.resolve(null), delete: noop };
                new Function('self', 'caches', swSource)(mockSelf, mockCaches);

                async function run(visibilities) {
                    shown.length = 0;
                    mockSelf.clients = {
                        matchAll: async () =>
                            visibilities.map((v) => ({ visibilityState: v })),
                    };
                    const ev = {
                        data: { json: () => ({ title: 'T', body: 'B', url: '/chat/x' }) },
                        _p: null,
                        waitUntil(p) { this._p = p; },
                    };
                    handlers.push(ev);
                    await ev._p;
                    return shown.map((s) => ({ title: s.title, tag: s.opts.tag }));
                }

                return {
                    foreground: await run(['visible']),
                    backgrounded: await run(['hidden']),
                    mixed: await run(['hidden', 'visible']),
                    none: await run([]),
                };
            }""",
            sw_source,
        )
        await browser.close()

    # A visible window on this device suppresses the notification...
    assert results["foreground"] == []
    assert results["mixed"] == []
    # ...but a hidden/backgrounded app, or no window at all, still notifies.
    assert results["backgrounded"] == [{"title": "T", "tag": "/chat/x"}]
    assert results["none"] == [{"title": "T", "tag": "/chat/x"}]


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
                '[x](https://e.com/"onmouseover=alert(1))',
                "[x](https://e.com)",
                "[preview](agentdeck-preview:demo/index.html)",
                "[relative](demo/index.html)",
                "[escape](agentdeck-preview:../outside.html)",
                "[absolute](agentdeck-preview:/tmp/outside.html)",
                "[source](agentdeck-preview:demo/index.py)",
            ],
        )
        preview_with_session = await page.evaluate(
            """() => {
                const output = document.querySelector('#output');
                output.innerHTML = mdToHtml(
                  '[preview](agentdeck-preview:demo/My%20Page.html#results)',
                  'claude_code:test:sid1'
                );
                const link = output.querySelector('a');
                return {
                  href: link && link.getAttribute('href'),
                  target: link && link.getAttribute('target'),
                  rel: link && link.getAttribute('rel'),
                };
            }"""
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
    for result in (rendered[5], *rendered[7:]):
        assert result["href"] is None
        assert "<a " not in result["html"]
    assert rendered[6]["href"] == "demo/index.html"
    assert preview_with_session == {
        "href": (
            "/sessions/claude_code%3Atest%3Asid1/preview/"
            "demo/My%20Page.html#results"
        ),
        "target": "_blank",
        "rel": "noopener",
    }


async def test_transcript_plain_web_urls_render_as_safe_links(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    lines[0]["message"]["content"] = (
        "See https://github.com/eerovil/agentdeck/pull/65. "
        "Keep `https://example.test/code` literal and "
        "[open docs](https://example.test/docs). "
        "[Open preview](demo/index.html)."
    )
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        results = []
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 800})
            await page.set_content(response.text)
            message = page.locator(".ev.user .ev-text")
            links = message.locator("a")
            await links.first.wait_for()
            results.append(
                {
                    "links": await links.evaluate_all(
                        """links => links.map(link => ({
                          text: link.textContent,
                          href: link.getAttribute('href'),
                          target: link.getAttribute('target'),
                          rel: link.getAttribute('rel'),
                        }))"""
                    ),
                    "code_links": await message.locator("code a").count(),
                    "code_text": await message.locator("code").text_content(),
                }
            )
            await page.close()
        await browser.close()

    expected_links = [
        {
            "text": "https://github.com/eerovil/agentdeck/pull/65",
            "href": "https://github.com/eerovil/agentdeck/pull/65",
            "target": "_blank",
            "rel": "noopener",
        },
        {
            "text": "open docs",
            "href": "https://example.test/docs",
            "target": "_blank",
            "rel": "noopener",
        },
        {
            "text": "Open preview",
            "href": (
                "/sessions/claude_code%3Atest%3Asid1/preview/demo/index.html"
            ),
            "target": "_blank",
            "rel": "noopener",
        },
    ]
    for result in results:
        assert result["links"] == expected_links
        assert result["code_links"] == 0
        assert result["code_text"] == "https://example.test/code"


async def test_local_markdown_file_opens_from_absolute_path_with_line_suffix(tmp_path):
    app = _app_with_state(tmp_path)
    handoff = tmp_path / "handoff.md"
    handoff.write_text("# Handoff\n\n- first\n- second\n")

    async with _client(app) as client:
        rendered = await client.get(f"{handoff}:1")
        source = await client.get(f"{handoff}:3")

    assert rendered.status_code == 200
    assert 'id="local-markdown"' in rendered.text
    assert "# Handoff" in rendered.text
    assert rendered.headers["cache-control"] == "no-store"
    assert source.status_code == 200
    assert 'class="local-source"' in source.text
    assert 'id="L3" class="selected"' in source.text
    assert "first" in source.text


async def test_local_file_route_escapes_active_text_and_serves_binary_inline(tmp_path):
    app = _app_with_state(tmp_path)
    active = tmp_path / "page.html"
    active.write_text("<script>alert('no')</script>")
    binary = tmp_path / "asset.bin"
    binary.write_bytes(b"\x00\x01agentdeck")

    async with _client(app) as client:
        active_response = await client.get(str(active))
        binary_response = await client.get(str(binary))
        missing_response = await client.get(str(tmp_path / "missing.txt"))

    assert active_response.status_code == 200
    assert "&lt;script&gt;alert" in active_response.text
    assert "<script>alert('no')</script>" not in active_response.text
    assert binary_response.status_code == 200
    assert binary_response.content == b"\x00\x01agentdeck"
    assert binary_response.headers["content-disposition"].startswith("inline;")
    assert missing_response.status_code == 404


async def test_session_html_preview_serves_relative_assets_in_an_opaque_sandbox(tmp_path):
    app = _app_with_state(tmp_path)
    control_attempts = []

    @app.post("/preview-control-probe")
    async def preview_control_probe():
        control_attempts.append(True)
        return {"ok": True}

    site = tmp_path / "site"
    (site / "demo" / "styles").mkdir(parents=True)
    (site / "demo" / "index.html").write_text(
        "<!doctype html><link rel=stylesheet href=styles/app.css>"
        "<body><h1>Rendered preview</h1><img id=pixel src=pixel.png>"
        "<script src=app.js></script>"
        "<script type=module src=module.mjs></script>"
        "<script>Promise.allSettled(["
        "fetch('data.json').then(()=>document.body.dataset.fetch='allowed')"
        ".catch(()=>document.body.dataset.fetch='blocked'),"
        "fetch('/preview-control-probe',{method:'POST',body:'x'})"
        ".then(()=>document.body.dataset.control='allowed')"
        ".catch(()=>document.body.dataset.control='blocked'),"
        "fetch('https://example.com').then(()=>document.body.dataset.external='allowed')"
        ".catch(()=>document.body.dataset.external='blocked'),"
        "Promise.resolve().then(()=>navigator.serviceWorker.register('sw.js'))"
        ".then(()=>document.body.dataset.worker='allowed')"
        ".catch(()=>document.body.dataset.worker='blocked')"
        "]).then(()=>document.body.dataset.settled='yes')</script>"
    )
    (site / "demo" / "styles" / "app.css").write_text("h1 { color: tomato; }")
    (site / "demo" / "app.js").write_text(
        "document.body.dataset.ready = 'yes';"
        "const p=document.querySelector('#pixel');"
        "const loaded=()=>document.body.dataset.asset='yes';"
        "p.addEventListener('load',loaded);if(p.complete)loaded();"
    )
    (site / "demo" / "module.mjs").write_text("document.body.dataset.module = 'yes';")
    (site / "demo" / "sw.js").write_text("self.addEventListener('fetch', () => {});")
    (site / "demo" / "data.json").write_text('{"ok":"yes"}')
    (site / "demo" / "pixel.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )
    (site / "demo" / "evil.svg").write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'><script><![CDATA["
        "fetch('/healthz').then(()=>document.documentElement.dataset.control='allowed')"
        ".catch(()=>document.documentElement.dataset.control='blocked')"
        ".finally(()=>document.documentElement.dataset.settled='yes')"
        "]]></script></svg>"
    )
    (site / "demo" / ".env").write_text("SECRET=not-for-the-preview")
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.cwd = site

    async with _client(app) as client:
        wrapper = await client.get(
            "/sessions/claude_code:test:sid1/preview/demo/index.html"
        )
        preview_src = re.search(r'<iframe src="([^"]+)"', wrapper.text).group(1)
        asset_base = preview_src.rsplit("/", 1)[0]
        html = await client.get(preview_src)
        css = await client.get(f"{asset_base}/styles/app.css")
        script = await client.get(f"{asset_base}/app.js")
        data = await client.get(f"{asset_base}/data.json")
        dotfile = await client.get(f"{asset_base}/.env")
        health = await client.get("/healthz")

    assert wrapper.status_code == 200
    assert 'sandbox="allow-scripts"' in wrapper.text
    assert "demo/index.html" in wrapper.text
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert "<h1>Rendered preview</h1>" in html.text
    assert "sandbox allow-scripts" in html.headers["content-security-policy"]
    assert "allow-same-origin" not in html.headers["content-security-policy"]
    assert "allow-forms" not in html.headers["content-security-policy"]
    assert "default-src 'none'" in html.headers["content-security-policy"]
    assert "connect-src 'none'" in html.headers["content-security-policy"]
    assert html.headers["cache-control"] == "private, no-store"
    assert html.headers["cross-origin-opener-policy"] == "same-origin"
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert css.headers["access-control-allow-origin"] == "null"
    assert script.status_code == 200
    assert "dataset.ready" in script.text
    assert data.headers["access-control-allow-origin"] == "null"
    assert dotfile.status_code == 404
    assert "access-control-allow-origin" not in health.headers

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off")
    )
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 320, "height": 640})
            await page.goto(
                f"http://127.0.0.1:{port}/sessions/claude_code:test:sid1/"
                "preview/demo/index.html"
            )
            frame = page.frame_locator("iframe")
            await frame.locator("body[data-settled=yes]").wait_for()
            browser_result = await frame.locator("body").evaluate(
                """() => ({
                  script: document.body.dataset.ready,
                  asset: document.body.dataset.asset,
                  fetch: document.body.dataset.fetch,
                  control: document.body.dataset.control,
                  external: document.body.dataset.external,
                  worker: document.body.dataset.worker,
                  module: document.body.dataset.module,
                  popup: window.open('about:blank') ? 'allowed' : 'blocked',
                  color: getComputedStyle(document.querySelector('h1')).color,
                })"""
            )
            iframe_src = await page.locator("iframe").get_attribute("src")
            svg_page = await browser.new_page()
            await svg_page.goto(iframe_src.rsplit("/", 1)[0] + "/evil.svg")
            await svg_page.wait_for_function(
                "document.documentElement.dataset.settled === 'yes'"
            )
            svg_control = await svg_page.evaluate(
                "document.documentElement.dataset.control"
            )
            await browser.close()
    finally:
        server.should_exit = True
        await server_task
        sock.close()

    assert browser_result == {
        "script": "yes",
        "asset": "yes",
        "fetch": "blocked",
        "control": "blocked",
        "external": "blocked",
        "worker": "blocked",
        "module": "yes",
        "popup": "blocked",
        "color": "rgb(255, 99, 71)",
    }
    assert control_attempts == []
    assert svg_control == "blocked"


async def test_session_html_preview_rejects_traversal_and_symlink_escape(tmp_path):
    app = _app_with_state(tmp_path)
    site = tmp_path / "site"
    (site / "demo").mkdir(parents=True)
    (site / "other").mkdir()
    (site / "demo" / "index.html").write_text("inside")
    (site / "demo" / "source.py").write_text("secret = True")
    (site / "other" / "secret.json").write_text('{"secret": true}')
    os.mkfifo(site / "demo" / "pipe.txt")
    outside = tmp_path / "outside.html"
    outside.write_text("outside")
    (site / "escape.html").symlink_to(outside)
    (site / "demo" / "escape-dir").symlink_to(site / "other", target_is_directory=True)
    rebound = tmp_path / "rebound"
    (rebound / "demo").mkdir(parents=True)
    (rebound / "demo" / "index.html").write_text("different cwd")
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.cwd = site

    async with _client(app) as client:
        wrapper = await client.get(
            "/sessions/claude_code:test:sid1/preview/demo/index.html"
        )
        preview_src = re.search(r'<iframe src="([^"]+)"', wrapper.text).group(1)
        asset_base = preview_src.rsplit("/", 1)[0]
        traversal = await client.get(
            "/sessions/claude_code:test:sid1/preview/%2e%2e/outside.html"
        )
        nested_traversal = await client.get(
            "/sessions/claude_code:test:sid1/preview/nested/../../outside.html"
        )
        symlink = await client.get(
            "/sessions/claude_code:test:sid1/preview/escape.html"
        )
        bundle_escape = await client.get(
            f"{asset_base}/%2e%2e/other/secret.json"
        )
        symlink_directory = await client.get(
            f"{asset_base}/escape-dir/secret.json"
        )
        special_file = await client.get(f"{asset_base}/pipe.txt")
        unknown_type = await client.get(f"{asset_base}/source.py")
        token = preview_src.split("/")[-2]
        replacement = ("A" if token[-1] != "A" else "B")
        tampered = await client.get(
            preview_src.replace(f"/{token}/", f"/{token[:-1] + replacement}/")
        )
        directory = await client.get(
            "/sessions/claude_code:test:sid1/preview/."
        )
        unknown_session = await client.get(
            "/sessions/claude_code:test:missing/preview/index.html"
        )
        session.cwd = rebound
        rebound_capability = await client.get(preview_src)
        session.cwd = None
        missing_cwd = await client.get(
            "/sessions/claude_code:test:sid1/preview/index.html"
        )

    assert traversal.status_code == 404
    assert nested_traversal.status_code == 404
    assert symlink.status_code == 404
    assert bundle_escape.status_code == 404
    assert symlink_directory.status_code == 404
    assert special_file.status_code == 404
    assert unknown_type.status_code == 404
    assert tampered.status_code == 404
    assert directory.status_code == 404
    assert unknown_session.status_code == 404
    assert rebound_capability.status_code == 404
    assert missing_cwd.status_code == 404


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
    end = response.text.index("// Keep unsent prompts", start)
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


async def test_session_bottom_follow_stops_on_manual_scroll_and_resumes_near_end():
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    bottom_follow = (static_dir / "session_bottom_follow.js").read_text()
    filler = "".join(
        f'<div class="ev" style="min-height:80px">older message {index}</div>'
        for index in range(30)
    )
    html = f"""
      <style>{css}</style>
      <body class="session-page session-stack-page"><main>
        <div class="session-layout">
          <aside class="session-sidebar"></aside>
          <section class="session-detail">
            <div class="transcript">{filler}
              <div class="ev user pending-message" data-pending-message data-queue-id="1">
                <div class="ev-text">queued message</div>
              </div>
              <div class="ev user pending-message" data-pending-message data-queue-id="2">
                <div class="ev-text">queued message</div>
              </div>
            </div>
            <div id="tool-activity"></div>
            <form class="inject-form"><div id="inject-result"></div></form>
          </section>
        </div>
      </main></body>
    """

    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            page = await browser.new_page(viewport={"width": width, "height": 700})
            await page.set_content(html)
            await page.add_script_tag(content=bottom_follow)
            result = await page.evaluate(
                """async () => {
                  const frame = () => new Promise(requestAnimationFrame);
                  const settle = async () => { await frame(); await frame(); };
                  const root = document.querySelector('.session-detail');
                  const transcript = document.querySelector('.transcript');
                  const status = document.querySelector('#inject-result');
                  const form = document.querySelector('.inject-form');
                  const bottomGap = () => root.scrollHeight - root.clientHeight - root.scrollTop;
                  const swap = target => {
                    target.dispatchEvent(new CustomEvent('htmx:beforeSwap', {bubbles: true}));
                    target.dispatchEvent(new CustomEvent('htmx:afterSwap', {bubbles: true}));
                  };
                  const appendTranscript = html => {
                    transcript.dispatchEvent(new CustomEvent(
                      'htmx:sseBeforeMessage', {bubbles: true}
                    ));
                    transcript.insertAdjacentHTML('beforeend', html);
                    transcript.dispatchEvent(new CustomEvent(
                      'htmx:sseMessage', {bubbles: true}
                    ));
                  };

                  await settle();
                  const initialGap = bottomGap();
                  document.body.dispatchEvent(new CustomEvent('agentdeck:optimistic-send'));
                  await settle();

                  root.scrollTo(0, root.scrollHeight - root.clientHeight - 400);
                  await settle();
                  const readingPosition = root.scrollTop;

                  // The request can finish after the reader has already moved.
                  // Its late acknowledgement must not re-arm bottom-follow.
                  form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                    bubbles: true, detail: {successful: true, elt: form}
                  }));
                  appendTranscript(
                    '<div class="ev user" data-observed-queue-id="1" style="min-height:80px">' +
                    '<div class="ev-text">queued message</div></div>'
                  );
                  swap(status);
                  await settle();
                  appendTranscript('<div class="ev" style="min-height:80px">working</div>');
                  swap(status);
                  await settle();
                  appendTranscript('<div class="ev" style="min-height:80px">reply</div>');
                  swap(status);
                  await settle();
                  const positionWhileReading = root.scrollTop;
                  const pendingAfterReconcile = document.querySelectorAll(
                    '[data-pending-message]'
                  ).length;
                  const remainingQueueId = document.querySelector(
                    '[data-pending-message]'
                  )?.dataset.queueId;

                  root.scrollTo(0, root.scrollHeight - root.clientHeight - 20);
                  await settle();
                  appendTranscript(
                    '<div class="ev" style="min-height:120px">continued reply</div>'
                  );
                  await settle();
                  return {
                    initialGap,
                    readingPosition,
                    positionWhileReading,
                    pendingAfterReconcile,
                    remainingQueueId,
                    resumedGap: bottomGap(),
                  };
                }"""
            )
            results.append(result)
            await page.close()
        await browser.close()

    for result in results:
        assert result["initialGap"] <= 1
        assert result["positionWhileReading"] == result["readingPosition"]
        assert result["pendingAfterReconcile"] == 1
        assert result["remainingQueueId"] == "2"
        assert result["resumedGap"] <= 1


async def test_message_draft_survives_reload_and_newer_text_is_not_cleared(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")
    html = response.text.replace(
        "</main>",
        '<form class="inject-form" data-clear-on-send '
        'hx-post="/sessions/codex:test:draft/inject">'
        '<textarea id="inject-message" name="message"></textarea>'
        '<input type="file" name="images"></form></main>',
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 412, "height": 800})

        async def serve(route):
            if route.request.resource_type == "document":
                await route.fulfill(status=200, content_type="text/html", body=html)
            else:
                await route.fulfill(status=200, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/claude_code:test:sid1")
        prompt = page.locator("#inject-message")
        await prompt.fill("unfinished draft")
        await page.reload()
        restored = await prompt.input_value()

        result = await page.evaluate(
            """() => {
                const form = document.querySelector('.inject-form');
                const input = form.querySelector('textarea[name="message"]');
                input.value = 'message being sent';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                form.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
                  bubbles: true,
                  detail: {elt: form}
                }));
                input.value = 'new draft typed while sending';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                const newerDraft = input.value;

                input.value = 'sent normally';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                form.dispatchEvent(new CustomEvent('htmx:beforeRequest', {
                  bubbles: true,
                  detail: {elt: form}
                }));
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                const afterNormalSend = input.value;

                input.value = 'next draft while queued';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                // HTMX re-homes completion events from a polling status node
                // after outerHTML removes it. That event reaches the connected
                // parent form without a matching form beforeRequest.
                form.dispatchEvent(new CustomEvent('htmx:afterRequest', {
                  bubbles: true,
                  detail: {successful: true, elt: form}
                }));
                return {
                  newerDraft,
                  afterNormalSend,
                  afterQueuedStatusPoll: input.value,
                };
            }"""
        )
        await page.reload()
        afterSentReload = await prompt.input_value()
        await browser.close()

    assert restored == "unfinished draft"
    assert result == {
        "newerDraft": "new draft typed while sending",
        "afterNormalSend": "",
        "afterQueuedStatusPoll": "next draft while queued",
    }
    assert afterSentReload == "next draft while queued"


async def test_working_marker_is_an_overlay_that_does_not_change_page_height(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 800, "height": 600})
        await page.set_content(html)
        result = await page.evaluate(
            """() => {
                const stage = document.querySelector('.transcript-stage');
                const activity = document.querySelector('#tool-activity');
                activity.replaceChildren();
                const before = stage.getBoundingClientRect().height;
                activity.innerHTML = '<div class="ev tool-wait" data-activity-elapsed="3">' +
                  '<span class="tool-wait-label">Working</span>' +
                  '<span class="tool-wait-elapsed"></span></div>';
                activity.firstElementChild._agentdeckMountedAt = Date.now() - 5000;
                const after = stage.getBoundingClientRect().height;
                return {
                    before,
                    after,
                    position: getComputedStyle(activity).position,
                    transcriptBottom: document.querySelector('.transcript')
                      .getBoundingClientRect().bottom,
                    activityTop: activity.getBoundingClientRect().top,
                    bottomGutter: getComputedStyle(stage).paddingBottom,
                };
            }"""
        )
        await page.wait_for_timeout(1100)
        elapsed = await page.locator(".tool-wait-elapsed").text_content()
        await browser.close()

    assert result["before"] == result["after"]
    assert result["position"] == "absolute"
    assert result["bottomGutter"] == "40px"
    assert result["activityTop"] >= result["transcriptBottom"]
    assert int(elapsed.removesuffix("s")) >= 8


async def test_mobile_session_composer_is_compact():
    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = f"""
      <style>{css}</style>
      <body class="session-page"><main><section>
        <form id="message-form" class="inject-form">
          <label for="inject-message">Message</label>
          <textarea id="inject-message" rows="3"></textarea>
          <label class="image-picker">
            ＋ Attach image <span class="paste-hint">or paste screenshot</span>
          </label>
          <input class="image-input" type="file">
          <div class="composer-actions">
            <span>Enter to send</span>
            <div class="composer-controls">
              <button class="stop-button" type="submit" form="interrupt-form">Stop</button>
              <button>Send</button>
            </div>
          </div>
          <div id="inject-result" class="inject-result running">
            <span class="send-spinner"></span>
          </div>
        </form><form id="interrupt-form"></form>
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
                const indicator = form.querySelector('#inject-result');
                const stop = form.querySelector('.stop-button').getBoundingClientRect();
                const send = form.querySelector('.composer-controls button:last-child')
                  .getBoundingClientRect();
                const main = document.querySelector('main');
                return {
                    formHeight: form.getBoundingClientRect().height,
                    textareaHeight: textarea.getBoundingClientRect().height,
                    controlsAligned: Math.abs(
                        picker.getBoundingClientRect().top - actions.getBoundingClientRect().top
                    ) < 2,
                    hintHidden: getComputedStyle(actions.querySelector('span')).display,
                    pasteHintHidden: getComputedStyle(form.querySelector('.paste-hint')).display,
                    indicatorPosition: getComputedStyle(indicator).position,
                    stopBesideSend: stop.right < send.left && Math.abs(stop.top - send.top) < 2,
                    controlsInsideForm: send.right <= form.getBoundingClientRect().right,
                    stopUsesInterruptForm: form.querySelector('.stop-button').form.id
                      === 'interrupt-form',
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
    assert metrics["indicatorPosition"] == "absolute"
    assert metrics["stopBesideSend"]
    assert metrics["controlsInsideForm"]
    assert metrics["stopUsesInterruptForm"]
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


def test_tool_calls_visible_outputs_hidden_and_live_marker(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_tool_activity, render_transcript_events

    app = _app_with_state(tmp_path)
    templates = app.state.templates
    timestamp = datetime(2026, 7, 15, 7, 42, tzinfo=UTC)
    events = [
        TranscriptEvent(seq=1, role="tool", text="huge noisy tool result"),
        TranscriptEvent(
            seq=2,
            role="assistant",
            tool_name="Bash",
            tool_summary="cmd: uv run pytest -q",
            tool_detail="uv run pytest -q --verbose tests/test_web.py",
            text=None,
            ts=timestamp,
        ),
        TranscriptEvent(
            seq=3,
            role="assistant",
            tool_name="exec",
            tool_display_name="Approval",
            tool_summary="reason: Allow deploying this change?",
            tool_detail="Reason\nAllow deploying this change?\n\nCommand\ngit push",
        ),
        TranscriptEvent(seq=4, role="assistant", text="here is my answer"),
        TranscriptEvent(seq=5, role="user", text="queued follow-up", queued=True),
    ]
    html = render_transcript_events(templates, events)
    assert "huge noisy tool result" not in html  # past tool result dropped
    assert "tool-call" in html
    assert '<details class="ev tool tool-call">' in html
    assert "Bash" in html
    assert "cmd: uv run pytest -q" in html
    assert "uv run pytest -q --verbose tests/test_web.py" in html
    assert "Approval" in html
    assert "reason: Allow deploying this change?" in html
    assert "Reason\nAllow deploying this change?" in html
    assert "here is my answer" in html  # real assistant text kept
    assert "queued follow-up" in html  # queued turns look like ordinary user chat
    assert "user · queued" not in html
    assert 'class="ev-time"' in html
    assert 'datetime="2026-07-15T07:42:00+00:00"' in html
    # the live marker appears only while actively working
    activity = render_tool_activity(templates, "Using tools", 12.9)
    assert "Using tools" in activity
    assert 'data-activity-elapsed="12"' in activity
    assert ">12s</span>" in activity
    assert "tool-wait" not in render_tool_activity(templates, None)


def test_transcript_user_message_renders_attached_images(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    html = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=1,
                role="user",
                text="See this",
                image_media_types=("image/png", "image/jpeg"),
            )
        ],
        session_key="claude_code:test:sid1",
    )

    assert 'aria-label="2 attached images"' in html
    assert html.count('class="ev-image"') == 2
    assert 'src="/sessions/claude_code:test:sid1/transcript-images/1/0"' in html
    assert 'alt="Attached image 2"' in html

    image_only = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=2,
                role="user",
                image_media_types=("image/png",),
            )
        ],
        session_key="claude_code:test:sid1",
    )
    assert 'class="ev user"' in image_only
    assert 'class="ev-image"' in image_only


def test_transcript_user_message_renders_observed_pending_identity(tmp_path):
    from agentdeck.inject import QueuedMessage
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    event = TranscriptEvent(seq=7, role="user", text="same text")
    item = QueuedMessage(41, "same text", client_action_id="action-41")

    html = render_transcript_events(
        app.state.templates,
        [event],
        session_key="claude_code:test:sid1",
        observed_messages={7: item},
    )

    assert 'data-observed-queue-id="41"' in html
    assert 'data-observed-action-id="action-41"' in html


async def test_transcript_image_is_served_outside_the_html_response(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    transcript = tmp_path / "projects" / "-tmp" / "sid1.jsonl"
    lines = [json.loads(line) for line in transcript.read_text().splitlines()]
    png = b"\x89PNG\r\n\x1a\nsmall"
    lines[0]["message"]["content"] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png).decode(),
            },
        },
        {"type": "text", "text": "first question"},
    ]
    transcript.write_text("".join(json.dumps(line) + "\n" for line in lines))

    async with _client(app) as client:
        page = await client.get("/sessions/claude_code:test:sid1")
        image = await client.get(
            "/sessions/claude_code:test:sid1/transcript-images/1/0"
        )
        missing = await client.get(
            "/sessions/claude_code:test:sid1/transcript-images/1/1"
        )

    assert "data:image" not in page.text
    assert "/sessions/claude_code:test:sid1/transcript-images/1/0" in page.text
    assert image.status_code == 200
    assert image.content == png
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "private, no-store"
    assert missing.status_code == 404


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
    from agentdeck.models import (
        TokenTotals,
        TranscriptEvent,
        detailed_activity_label,
        transcript_event_is_progress,
    )
    from agentdeck.web.render import activity_label

    tool_call = TranscriptEvent(seq=1, role="assistant", tool_name="Bash", text=None)
    tool_result = TranscriptEvent(seq=1, role="tool", text="output")
    user_msg = TranscriptEvent(seq=1, role="user", text="do it")
    reply = TranscriptEvent(seq=1, role="assistant", text="done")
    continuing_reply = TranscriptEvent(
        seq=2, role="assistant", text="checking", turn_continues=True
    )
    terminal_reply = TranscriptEvent(
        seq=3, role="assistant", text="done", turn_continues=False
    )

    # a long tool run: quiet (not streaming) but last line is a tool → still busy
    assert activity_label(True, False, tool_call) == "Using tools"
    assert activity_label(True, False, tool_result) == "Using tools"
    # unanswered prompt while quiet → working (slow first token doesn't read idle)
    assert activity_label(True, False, user_msg) == "Working"
    # finished reply, quiet → idle (no marker)
    assert activity_label(True, False, reply) is None
    # finished reply but actively writing → working
    assert activity_label(True, True, reply) == "Working"
    # Provider lifecycle beats write recency in both directions.
    assert activity_label(True, False, continuing_reply, age_s=60) == "Working"
    assert activity_label(True, True, terminal_reply) is None
    # dead process → never a marker
    assert activity_label(False, True, tool_call) is None
    # open turn but no write for ages → stalled worker, not "Using tools" forever
    assert activity_label(True, False, tool_call, age_s=10_000) is None
    assert activity_label(True, False, user_msg, age_s=10_000) is None
    assert activity_label(True, False, continuing_reply, age_s=599) == "Working"
    assert activity_label(True, False, continuing_reply, age_s=600) is None
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
    reasoning = TranscriptEvent(seq=4, role="system", tool_name="reasoning")
    waiting = TranscriptEvent(seq=5, role="assistant", tool_name="wait")
    assert detailed_activity_label("Using tools", reasoning) == "Thinking"
    assert detailed_activity_label("Using tools", waiting) == "Waiting for command output"
    assert detailed_activity_label("Working", command) == "Working"
    usage_only = TranscriptEvent(
        seq=6, role="system", tokens=TokenTotals(input_tokens=1)
    )
    assert transcript_event_is_progress(usage_only) is False
    assert transcript_event_is_progress(reasoning) is True


def test_resolve_activity_label_pipeline():
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import resolve_activity_label

    tool_call = TranscriptEvent(seq=1, role="assistant", tool_name="Bash", text=None)
    reply = TranscriptEvent(seq=1, role="assistant", text="done")

    def resolve(**kw):
        base = dict(
            has_question=False,
            live=True,
            streaming=False,
            last_event=tool_call,
            age_s=0.0,
            has_working_subagent=False,
        )
        return resolve_activity_label(**{**base, **kw})

    # A surfaced question suppresses the badge even mid-tool — this is the guard
    # the page-load path used to omit (it covers trailing natural-language
    # questions, not only AskUserQuestion tool events).
    assert resolve(has_question=True) is None
    # Normal open-turn label flows through activity_label ("Using tools") and is
    # then refined by detailed_activity_label to name the tool.
    assert resolve() == "Using Bash"
    command = TranscriptEvent(seq=2, role="assistant", tool_name="reasoning")
    assert resolve(last_event=command) == "Thinking"
    # Idle + no subagent → nothing; idle + a working subagent → "Working".
    assert resolve(last_event=reply, streaming=False) is None
    assert resolve(last_event=reply, streaming=False, has_working_subagent=True) == "Working"
    # A not-live session yields no own-turn label, but an active nested subagent
    # still surfaces "Working" through the fallback (unchanged from before).
    assert resolve(live=False, has_working_subagent=True) == "Working"
    assert resolve(live=False, has_working_subagent=False) is None


async def test_session_detail_hides_activity_badge_when_question_pending(tmp_path):
    # Regression: the initial page render must apply the same question guard the
    # SSE tail does, so a LIVE session surfacing a question shows no activity
    # badge on load (previously it flashed "Working" until the SSE tick blanked
    # it ~1.5s later).
    app = _app_with_state(tmp_path, with_transcript=True)
    state = app.state.app_state

    def _seed(question):
        state.update_session(
            Session(
                key="claude_code:test:sid1",
                account_key="claude_code:test",
                session_id="sid1",
                status=SessionStatus.LIVE,
                thinking=True,  # streaming → activity_label would say "Working"
                question=question,
                title="Hello World Session",
                capabilities=frozenset({Capability.TRANSCRIPT}),
            )
        )

    async with _client(app) as client:
        _seed("Which option do you want?")
        with_question = await client.get("/sessions/claude_code:test:sid1")
        _seed(None)
        without_question = await client.get("/sessions/claude_code:test:sid1")

    # Same streaming session: the badge is suppressed only because a question is
    # surfaced — proving the guard, not merely an always-empty marker.
    assert "tool-wait-label" not in with_question.text
    assert "tool-wait-label" in without_question.text
    assert "Working" in without_question.text


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


def test_session_presentation_nests_subagents_under_parent():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="a:p", account_key="a", session_id="p", status=SessionStatus.LIVE
    )
    child = Session(
        key="a:c",
        account_key="a",
        session_id="c",
        status=SessionStatus.LIVE,
        thinking=True,  # a live subagent nests under its parent
        parent_session_key="a:p",
    )
    dead = Session(
        key="a:d",
        account_key="a",
        session_id="d",
        status=SessionStatus.IDLE,
        thinking=False,  # a finished subagent is dead weight — dropped, not nested
        parent_session_key="a:p",
        show_when_idle=True,
    )
    orphan = Session(
        key="a:o",
        account_key="a",
        session_id="o",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key="a:gone",  # parent not visible
    )
    for s in (parent, child, dead, orphan):
        state.update_session(s)

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    top_keys = {s.key for s in top}
    assert "a:p" in top_keys  # parent stays top-level
    assert "a:c" not in top_keys  # child is nested, not top-level
    assert "a:d" not in top_keys  # dead subagent is dropped, not floated top-level
    assert "a:o" not in top_keys  # orphan (parent gone) is dropped, not floated top-level
    assert [c.key for c in children["a:p"]] == ["a:c"]  # only the live subagent nests
    assert "a:o" not in children  # orphan has no visible parent to nest under
    effective_parent = next(session for session in top if session.key == parent.key)
    assert effective_parent.thinking is True
    assert effective_parent.activity == "Working"
    assert state.sessions[parent.key].thinking is False  # provider state stays raw
    assert presentation.working_count == 1
    assert presentation.display(parent) is effective_parent
    with pytest.raises(TypeError):
        children["another"] = ()

    state.update_session(replace(child, status=SessionStatus.IDLE, thinking=False))
    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    effective_parent = next(session for session in top if session.key == parent.key)
    assert effective_parent.thinking is False
    assert parent.key not in children


def test_descendant_progress_sustains_parent_and_resets_effective_stall():
    from agentdeck.state import AppState

    now = datetime.now(UTC)
    state = AppState()
    parent = Session(
        key="a:p",
        account_key="a",
        session_id="p",
        status=SessionStatus.LIVE,
        stalled=True,
        last_progress=now - timedelta(minutes=12),
    )
    child = Session(
        key="a:c",
        account_key="a",
        session_id="c",
        status=SessionStatus.LIVE,
        thinking=True,
        last_progress=now,
        parent_session_key=parent.key,
    )
    state.update_session(parent)
    state.update_session(child)

    effective = state.session_presentation().display(parent)

    assert effective.thinking is True
    assert effective.stalled is False
    assert effective.last_progress == now
    assert state.sessions[parent.key].stalled is True  # display projection only


def test_session_presentation_includes_embedded_subagents_in_parent_activity():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="codex:p",
        account_key="codex",
        session_id="p",
        status=SessionStatus.IDLE,
        thinking=False,
        subagent_count=2,
        show_when_idle=True,
    )
    state.update_session(parent)

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of

    assert children == {}
    assert top[0].thinking is True
    assert top[0].activity == "Working"
    assert state.sessions[parent.key].thinking is False
    assert presentation.working_count == 1
    assert presentation.has_working_subagent(parent) is True


def test_active_child_sessions_excludes_duplicate_embedded_subagent():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="codex:test:parent",
        account_key="codex:test",
        session_id="parent",
        status=SessionStatus.LIVE,
        subagent_count=1,
        subagents=(
            SubagentProgress(agent_id="native", status="working"),
        ),
    )
    native_child = Session(
        key="codex:test:native",
        account_key="codex:test",
        session_id="native",
        status=SessionStatus.LIVE,
        thinking=True,
        parent_session_key=parent.key,
    )
    delegated_child = Session(
        key="codex:test:delegated",
        account_key="codex:test",
        session_id="delegated",
        status=SessionStatus.LIVE,
        thinking=True,
    )
    for session in (parent, native_child, delegated_child):
        state.update_session(session)
    state.mark_delegated_session(delegated_child.key, parent_session_id=parent.session_id)

    presentation = state.session_presentation()

    assert presentation.active_child_sessions(parent) == (
        presentation.display(delegated_child),
    )


def test_session_tree_nests_cross_provider_delegation():
    from agentdeck.state import AppState

    state = AppState()
    # A Claude chat that delegated a Codex session: child and parent come from
    # different provider scans, so the link lives in state.delegation_parents.
    parent = Session(
        key="claude_code:main:p",
        account_key="claude_code:main",
        session_id="p",
        status=SessionStatus.LIVE,
    )
    child = Session(
        key="codex:codex:c",
        account_key="codex:codex",
        session_id="c",
        status=SessionStatus.LIVE,  # no parent_session_key of its own
        thinking=True,
    )
    for s in (parent, child):
        state.update_session(s)
    state.set_delegation_parents("claude_code:main", {"codex:codex:c": "claude_code:main:p"})

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    assert "codex:codex:c" not in {s.key for s in top}  # nested cross-provider
    assert [c.key for c in children["claude_code:main:p"]] == ["codex:codex:c"]
    effective_parent = next(s for s in top if s.key == "claude_code:main:p")
    assert effective_parent.thinking is True


def test_mark_delegated_session_records_parent_and_nests():
    from agentdeck.state import AppState

    state = AppState()
    parent = Session(
        key="claude_code:main:p",
        account_key="claude_code:main",
        session_id="p",
        status=SessionStatus.LIVE,
    )
    child = Session(
        key="codex:codex:c",
        account_key="codex:codex",
        session_id="c",
        status=SessionStatus.LIVE,
    )
    for s in (parent, child):
        state.update_session(s)
    # The delegation bridge records the invoking session by its raw id; state
    # resolves it to the parent's full key at render, without any transcript
    # marker.
    state.mark_delegated_session("codex:codex:c", parent_session_id="p")

    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    assert "codex:codex:c" not in {s.key for s in top}
    assert [c.key for c in children["claude_code:main:p"]] == ["codex:codex:c"]
    assert state.sessions["codex:codex:c"].is_delegated is True


def test_recorded_delegation_parent_self_heals_when_parent_appears():
    from agentdeck.state import AppState

    state = AppState()
    child = Session(
        key="codex:codex:c",
        account_key="codex:codex",
        session_id="c",
        status=SessionStatus.LIVE,
    )
    state.update_session(child)
    # Parent not yet scanned in when the delegation starts: record the raw id
    # anyway; the child stays top-level for now rather than nesting under a
    # phantom.
    state.mark_delegated_session("codex:codex:c", parent_session_id="p")
    top = state.session_presentation().top_level
    assert "codex:codex:c" in {s.key for s in top}

    # Once the parent is scanned in, lazy resolution nests the child with no
    # second mark_delegated_session call — the one-shot race self-heals.
    state.update_session(
        Session(
            key="claude_code:main:p",
            account_key="claude_code:main",
            session_id="p",
            status=SessionStatus.LIVE,
        )
    )
    presentation = state.session_presentation()
    top, children = presentation.top_level, presentation.children_of
    assert "codex:codex:c" not in {s.key for s in top}
    assert [c.key for c in children["claude_code:main:p"]] == ["codex:codex:c"]


def test_list_subagents_extracts_parent_uuid_from_path(tmp_path):
    from agentdeck.providers.claude_code.provider import _list_subagents

    sub = (
        tmp_path
        / "projects"
        / "-var-home-eero-outdoor"
        / "parent-uuid-1234"
        / "subagents"
        / "agent-abc123.jsonl"
    )
    sub.parent.mkdir(parents=True)
    sub.write_text('{"isSidechain": true, "type": "user"}\n')

    out = _list_subagents(tmp_path)
    assert out == {"agent-abc123": (sub, "parent-uuid-1234")}


def test_subagent_task_reads_sidechain_first_prompt(tmp_path):
    from agentdeck.providers.claude_code.provider import _subagent_task

    path = tmp_path / "agent-x.jsonl"
    # isSidechain user lines are dropped by transcript_meta; _subagent_task must
    # still surface the first user prompt (the Task description) for the title.
    path.write_text(
        '{"type": "summary", "summary": "ignore"}\n'
        '{"type": "user", "isSidechain": true, "message": {"content": '
        '[{"type": "text", "text": "Audit the parser for bugs"}]}}\n'
        '{"type": "user", "message": {"content": "later prompt"}}\n'
    )
    assert _subagent_task(path) == "Audit the parser for bugs"


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


async def test_working_card_uses_pulsing_dot_without_text_badge(tmp_path):
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
    assert "thinking-badge" not in r.text
    assert "dot live thinking" in r.text
    assert 'data-session-key="claude_code:test:think1"' in r.text
    assert 'data-working="1"' in r.text


async def test_opening_session_card_acknowledges_and_times_navigation(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        dashboard = await client.get("/")
        detail = await client.get("/sessions/claude_code:test:sid1")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    action_script = (static_dir / "action_timing.js").read_text()
    stylesheet = (static_dir / "app.css").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        for width in (1200, 320):
            release = asyncio.Event()
            requested = asyncio.Event()
            context = await browser.new_context(
                viewport={"width": width, "height": 800}, service_workers="block"
            )
            page = await context.new_page()

            async def serve(route, request, requested=requested, release=release):
                path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
                if path == "/":
                    await route.fulfill(status=200, content_type="text/html", body=dashboard.text)
                elif path == "/sessions/claude_code:test:sid1":
                    requested.set()
                    await release.wait()
                    await route.fulfill(status=200, content_type="text/html", body=detail.text)
                elif path == "/static/action_timing.js":
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=action_script
                    )
                elif path == "/static/app.css":
                    await route.fulfill(status=200, content_type="text/css", body=stylesheet)
                else:
                    await route.fulfill(status=204, body="")

            await page.route("http://agentdeck.test/**", serve)
            await page.goto("http://agentdeck.test/")
            card = page.locator('a.session[data-session-key="claude_code:test:sid1"]')
            immediate_result = asyncio.get_running_loop().create_future()

            def report_immediate(value, immediate_result=immediate_result):
                if not immediate_result.done():
                    immediate_result.set_result(value)

            await page.expose_function("reportOpeningState", report_immediate)
            await page.evaluate(
                """() => document.addEventListener('click', event => {
                  var card = event.target.closest('a.session');
                  if (!card) return;
                  window.reportOpeningState({
                    opening: card.classList.contains('opening'),
                    busy: card.getAttribute('aria-busy'),
                    label: getComputedStyle(card, '::after').content,
                    timing: JSON.parse(JSON.stringify(
                      Array.from(AgentDeckActionTiming.records.values())[0]
                    )),
                    overflow: document.documentElement.scrollWidth > innerWidth,
                  });
                }, {once: true})"""
            )
            click = asyncio.create_task(card.click())
            immediate = await asyncio.wait_for(immediate_result, timeout=5)
            await requested.wait()
            await asyncio.sleep(0.15)
            release.set()
            await click
            await page.wait_for_url("**/sessions/claude_code:test:sid1")
            await page.wait_for_function(
                "AgentDeckActionTiming.summary().open_session?.settled_ms !== null"
            )
            completed = await page.evaluate(
                """() => ({
                  record: AgentDeckActionTiming.snapshot()[0],
                  summary: AgentDeckActionTiming.summary().open_session,
                  overflow: document.documentElement.scrollWidth > innerWidth,
                })"""
            )
            await page.go_back()
            restored = await page.locator(
                'a.session[data-session-key="claude_code:test:sid1"]'
            ).evaluate(
                "card => ({opening: card.classList.contains('opening'), "
                "busy: card.getAttribute('aria-busy')})"
            )
            await context.close()

            assert immediate["opening"] is True
            assert immediate["busy"] == "true"
            assert immediate["label"] == '"Opening…"'
            assert immediate["overflow"] is False
            assert immediate["timing"]["action"] == "open_session"
            assert (
                immediate["timing"]["epochMarks"]["acknowledged"]
                - immediate["timing"]["epochMarks"]["interaction"]
                < 16
            )
            assert completed["record"]["sessionKey"] == "claude_code:test:sid1"
            assert completed["record"]["successful"] is True
            assert completed["summary"]["samples"] == 1
            assert completed["summary"]["http_ms"]["p50"] >= 100
            assert completed["summary"]["settled_ms"]["p50"] >= 100
            assert completed["overflow"] is False
            assert restored == {"opening": False, "busy": None}
        await browser.close()


async def test_mobile_chat_layers_over_live_list_and_back_and_swipe_are_instant(tmp_path):
    app = _app_with_state(tmp_path)
    async with _client(app) as client:
        detail = await client.get("/sessions/claude_code:test:sid1")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    stack_script = (static_dir / "mobile_session_stack.js").read_text()
    list_requests = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800}, service_workers="block"
        )
        page = await context.new_page()

        async def serve(route, request):
            nonlocal list_requests
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/claude_code:test:sid1":
                await route.fulfill(status=200, content_type="text/html", body=detail.text)
            elif path == "/":
                list_requests += 1
                await route.fulfill(status=500, body="list navigation should stay client-side")
            elif path == "/static/mobile_session_stack.js":
                await route.fulfill(status=200, content_type="text/javascript", body=stack_script)
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/claude_code:test:sid1")
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )

        initial = await page.evaluate(
            """() => ({
              path: location.pathname,
              sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
              sidebarHidden: document.querySelector('.session-sidebar').getAttribute('aria-hidden'),
              detailHidden: document.querySelector('.session-detail').getAttribute('aria-hidden'),
              listCard: Boolean(document.querySelector('.session-sidebar a.session')),
              overflow: document.documentElement.scrollWidth > innerWidth,
            })"""
        )

        await page.locator("a.back").click()
        await page.wait_for_function(
            "location.pathname === '/' && "
            "document.body.classList.contains('mobile-list-open')"
        )
        after_back = await page.evaluate(
            """() => ({
              path: location.pathname,
              detailInert: document.querySelector('.session-detail').inert,
              sidebarInert: document.querySelector('.session-sidebar').inert,
              backVisibility: getComputedStyle(document.querySelector('.back')).visibility,
            })"""
        )

        await page.evaluate("history.forward()")
        await page.wait_for_function(
            "location.pathname.includes('/sessions/') && "
            "!document.body.classList.contains('mobile-list-open')"
        )
        # A vertical gesture near the edge remains transcript scrolling; it must
        # not claim the stack or leave a partial transform behind.
        vertical = await page.evaluate(
            """() => {
              const detail = document.querySelector('.session-detail');
              const fire = (type, x, y) => detail.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 6, pointerType: 'touch',
                isPrimary: true, clientX: x, clientY: y,
              }));
              fire('pointerdown', 8, 200);
              fire('pointermove', 15, 280);
              fire('pointerup', 15, 280);
              return {
                path: location.pathname,
                dragging: document.body.classList.contains('mobile-stack-dragging'),
                inlineTransform: detail.style.getPropertyValue('--mobile-chat-x'),
              };
            }"""
        )
        await page.evaluate(
            """() => {
              const detail = document.querySelector('.session-detail');
              const fire = (type, x, y) => detail.dispatchEvent(new PointerEvent(type, {
                bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch',
                isPrimary: true, clientX: x, clientY: y,
              }));
              fire('pointerdown', 8, 300);
              fire('pointermove', 190, 304);
              fire('pointerup', 190, 304);
            }"""
        )
        await page.wait_for_function(
            "location.pathname === '/' && "
            "document.body.classList.contains('mobile-list-open')"
        )
        after_swipe = await page.evaluate(
            """() => ({
              path: location.pathname,
              listOpen: document.body.classList.contains('mobile-list-open'),
              listCard: Boolean(document.querySelector('.session-sidebar a.session')),
              overflow: document.documentElement.scrollWidth > innerWidth,
            })"""
        )
        await browser.close()

    assert list_requests == 0
    assert initial == {
        "path": "/sessions/claude_code:test:sid1",
        "sidebar": "block",
        "sidebarHidden": "true",
        "detailHidden": "false",
        "listCard": True,
        "overflow": False,
    }
    assert after_back == {
        "path": "/",
        "detailInert": True,
        "sidebarInert": False,
        "backVisibility": "hidden",
    }
    assert vertical == {
        "path": "/sessions/claude_code:test:sid1",
        "dragging": False,
        "inlineTransform": "",
    }
    assert after_swipe == {
        "path": "/",
        "listOpen": True,
        "listCard": True,
        "overflow": False,
    }


async def test_mobile_fast_swipe_toward_earlier_messages_jumps_to_top(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as client:
        detail = await client.get("/sessions/claude_code:test:sid1")

    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    stylesheet = (static_dir / "app.css").read_text()
    stack_script = (static_dir / "mobile_session_stack.js").read_text()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 320, "height": 800},
            has_touch=True,
            service_workers="block",
        )
        page = await context.new_page()

        async def serve(route, request):
            path = request.url.split("?", 1)[0].removeprefix("http://agentdeck.test")
            if path == "/sessions/claude_code:test:sid1":
                await route.fulfill(status=200, content_type="text/html", body=detail.text)
            elif path == "/static/mobile_session_stack.js":
                await route.fulfill(
                    status=200, content_type="text/javascript", body=stack_script
                )
            elif path == "/static/app.css":
                await route.fulfill(status=200, content_type="text/css", body=stylesheet)
            else:
                await route.fulfill(status=204, body="")

        await page.route("http://agentdeck.test/**", serve)
        await page.goto("http://agentdeck.test/sessions/claude_code:test:sid1")
        await page.wait_for_function(
            "document.body.classList.contains('mobile-session-stack-ready')"
        )
        result = await page.evaluate(
            """async () => {
              const detail = document.querySelector('.session-detail');
              const transcript = document.querySelector('.transcript');
              const frame = () => new Promise(requestAnimationFrame);
              const settle = async () => { await frame(); await frame(); };
              const waitForTop = async () => {
                for (let index = 0; index < 120; index += 1) {
                  if (detail.scrollTop <= 1) return detail.scrollTop;
                  await frame();
                }
                return detail.scrollTop;
              };
              const fireTouch = (type, x, y, identifier) => {
                const touch = new Touch({
                  identifier, target: detail,
                  clientX: x, clientY: y, screenX: x, screenY: y,
                  pageX: x, pageY: y,
                });
                const active = type === 'touchend' ? [] : [touch];
                detail.dispatchEvent(new TouchEvent(type, {
                  bubbles: true, cancelable: true,
                  touches: active, targetTouches: active, changedTouches: [touch],
                }));
              };
              const swipe = async (from, to, delay, identifier) => {
                fireTouch('touchstart', from[0], from[1], identifier);
                await new Promise(resolve => setTimeout(resolve, delay));
                fireTouch('touchend', to[0], to[1], identifier);
                await settle();
                return detail.scrollTop;
              };

              transcript.insertAdjacentHTML(
                'afterbegin',
                Array.from({length: 50}, (_, index) =>
                  `<div class="ev" style="min-height:80px">older ${index}</div>`
                ).join('')
              );
              detail.scrollTo(0, detail.scrollHeight);
              await settle();
              const initial = detail.scrollTop;
              const nativeScrollTo = detail.scrollTo.bind(detail);
              const topScrolls = [];
              detail.scrollTo = (...args) => {
                if (typeof args[0] === 'object') topScrolls.push(args[0]);
                nativeScrollTo(...args);
              };
              const opposite = await swipe([160, 330], [160, 170], 20, 1);
              const horizontal = await swipe([40, 200], [220, 210], 20, 2);
              const shortFast = await swipe([160, 170], [160, 330], 20, 3);
              const longSlow = await swipe([160, 60], [160, 340], 300, 4);

              detail.scrollTo(0, detail.clientHeight * 1.5);
              await settle();
              const nearTopStart = detail.scrollTop;
              const nearTop = await swipe([160, 60], [160, 340], 20, 5);

              detail.scrollTo(0, detail.scrollHeight);
              await settle();
              const beforeFast = detail.scrollTop;
              const smoothStart = await swipe([160, 60], [160, 340], 20, 6);
              const fast = await waitForTop();
              return {
                initial, opposite, horizontal, shortFast, longSlow,
                nearTopStart, nearTop, beforeFast, smoothStart, fast, topScrolls,
              };
            }"""
        )
        await browser.close()

    assert result["initial"] > 1000
    assert result["opposite"] == result["initial"]
    assert result["horizontal"] == result["initial"]
    assert result["shortFast"] == result["initial"]
    assert result["longSlow"] == result["initial"]
    assert result["nearTop"] == result["nearTopStart"]
    assert result["beforeFast"] == result["initial"]
    assert 0 < result["smoothStart"] < result["beforeFast"]
    assert result["fast"] <= 1
    assert result["topScrolls"] == [{"top": 0, "behavior": "smooth"}]


async def test_subagent_count_renders_on_card_and_detail_header(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    session = app.state.app_state.sessions["claude_code:test:sid1"]
    session.subagent_count = 2
    session.subagents = (
        SubagentProgress(
            agent_id="agent-1",
            nickname="Faraday",
            role="scout",
            task="Inventory the Storm migration surface",
            status="working",
            updated_at=datetime(2026, 7, 17, 7, 42, tzinfo=UTC),
        ),
        SubagentProgress(
            agent_id="agent-2",
            nickname="Banach",
            role="investigator",
            task="Audit the canonical engine boundary",
            status="finished",
            result="Found one status-normalization bug.",
            updated_at=datetime(2026, 7, 17, 7, 43, tzinfo=UTC),
        ),
    )

    async with _client(app) as client:
        dashboard = await client.get("/")
        detail = await client.get("/sessions/claude_code:test:sid1")

    assert "2 sub-agents" in dashboard.text
    assert 'title="This chat is running spawned agents"' in dashboard.text
    assert "2 sub-agents" in detail.text
    assert 'aria-label="Subagent progress"' in detail.text
    assert 'class="subagent-activity active"' in detail.text
    assert "Faraday" in detail.text
    assert "scout" in detail.text
    assert "Inventory the Storm migration surface" in detail.text
    assert "Banach" in detail.text
    assert "Found one status-normalization bug." in detail.text
    assert (
        detail.text.index("an answer here")
        < detail.text.index('id="tool-activity"')
        < detail.text.index('id="subagent-activity"')
    )
    assert 'data-working="1"' in _session_card(dashboard.text, session.key)
    assert '<span class="status-tag thinking">thinking</span>' in detail.text
    assert "1 working" in dashboard.text


async def test_nested_subagents_collapse_by_default_with_toggle(tmp_path):
    # Issue #5: nested sub-agent rows render collapsed by default, behind a
    # count-pill toggle on the parent card, with an activity hint when a child
    # is working. The rows stay in the DOM (collapse is CSS only).
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    state.update_session(
        Session(
            key="claude_code:test:childA",
            account_key="claude_code:test",
            session_id="childA",
            status=SessionStatus.LIVE,
            title="Scout the parser",
            parent_session_key="claude_code:test:sid1",
            thinking=True,  # a working child → the pill shows the working hint
        )
    )
    state.update_session(
        Session(
            key="claude_code:test:childB",
            account_key="claude_code:test",
            session_id="childB",
            status=SessionStatus.LIVE,
            title="Audit the boundary",
            parent_session_key="claude_code:test:sid1",
            thinking=True,  # a second live child → both nest under the toggle
        )
    )

    async with _client(app) as client:
        dashboard = await client.get("/")
        detail = await client.get("/sessions/claude_code:test:sid1")

    text = dashboard.text
    # Collapsed by default, tied to the parent key.
    assert 'class="subagent-children collapsed" data-subagents-of="claude_code:test:sid1"' in text
    # Count-pill toggle affordance, collapsed (aria-expanded=false).
    assert 'class="subagent-toggle"' in text
    assert 'data-key="claude_code:test:sid1"' in text
    assert 'aria-expanded="false"' in text
    assert "2 sub-agents" in text
    # A working child surfaces the activity hint even while collapsed.
    assert "subagent-hint working" in text
    # The rows themselves are still rendered (hidden via CSS, not omitted).
    assert "Scout the parser" in text
    assert "Audit the boundary" in text
    assert 'data-working="1"' in _session_card(text, "claude_code:test:sid1")
    assert '<span class="status-tag thinking">thinking</span>' in detail.text
    assert 'aria-label="Subagent progress"' in detail.text
    assert "Scout the parser" in detail.text
    assert "Audit the boundary" in detail.text
    assert (
        '<a class="subagent-row working" '
        'href="/sessions/claude_code:test:childA"' in detail.text
    )
    assert (
        '<a class="subagent-row working" '
        'href="/sessions/claude_code:test:childB"' in detail.text
    )
    assert "1 working" in text


def test_subagent_notification_renders_as_compact_expandable_row(tmp_path):
    from agentdeck.models import TranscriptEvent
    from agentdeck.web.render import render_transcript_events

    app = _app_with_state(tmp_path)
    html = render_transcript_events(
        app.state.templates,
        [
            TranscriptEvent(
                seq=1,
                role="system",
                text="Audit complete.\n\nFull evidence follows.",
                tool_name="subagent",
                tool_summary="Audit complete.",
                subagent_status="finished",
                subagent_id="agent-1",
                subagent_name="Faraday",
            )
        ],
    )

    assert '<details class="ev tool subagent-update finished">' in html
    assert "Faraday finished" in html
    assert "Audit complete." in html
    assert "Full evidence follows." in html


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


def test_usage_stale_is_age_based_not_last_poll(tmp_path):
    # Issue #6: "stale" reflects data age, not a single rate-limited poll.
    from dataclasses import replace
    from datetime import timedelta

    from agentdeck.models import Account
    from agentdeck.state import AppState
    from agentdeck.web.render import _usage_rows

    state = AppState()
    state.usage_stale_after_s = 300.0
    acc = Account("claude_code:main", "claude_code", "main", tmp_path)
    now = datetime.now(UTC)
    fresh_but_failed = UsageSnapshot(
        account_key=acc.key,
        five_hour_pct=90.0,
        five_hour_resets_at=None,
        seven_day_pct=42.0,
        seven_day_resets_at=None,
        fetched_at=now - timedelta(seconds=60),
        stale=True,  # last poll 429'd, but the numbers are a minute old
    )
    state.usage[acc.key] = fresh_but_failed
    assert _usage_rows([acc], state)[0]["stale"] is False

    # Genuinely old data reads as stale even if the last poll succeeded.
    state.usage[acc.key] = replace(
        fresh_but_failed, fetched_at=now - timedelta(seconds=400), stale=False
    )
    row = _usage_rows([acc], state)[0]
    assert row["stale"] is True
    assert row["stale_after"] == 300.0


async def test_old_usage_renders_stale_badge_and_threshold_attr(tmp_path):
    from datetime import timedelta

    app = _app_with_state(tmp_path)
    # threshold = max(3 * usage_interval_s, 300) = 900s for the default config
    app.state.app_state.set_usage(
        UsageSnapshot(
            account_key="claude_code:test",
            five_hour_pct=90.0,
            five_hour_resets_at=None,
            seven_day_pct=42.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC) - timedelta(seconds=1000),
        )
    )
    async with _client(app) as c:
        r = await c.get("/partials/limit-bars")
    assert r.status_code == 200
    assert "stale-tag" in r.text  # server marks the aged snapshot stale
    assert 'data-stale-after="900.0"' in r.text  # client can flip it live


async def test_session_detail_renders_transcript(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    async with _client(app) as c:
        r = await c.get("/sessions/claude_code:test:sid1")
    assert r.status_code == 200
    assert "first question" in r.text
    assert "an answer here" in r.text
    assert "claude-opus-4-8" in r.text
    assert "15 tok" in r.text  # 10 input + 5 output summed from usage
    assert r.text.count('class="ev-time"') == 2
    assert 'datetime="2026-07-15T07:41:00+00:00"' in r.text
    assert 'datetime="2026-07-15T07:42:00+00:00"' in r.text
    # usage bars paint server-side in the topbar (no separate /events socket)
    assert "42%" in r.text
    # the page binds its single SSE connection to the per-session stream
    assert 'sse-connect="/events/sessions/claude_code:test:sid1?after_seq=2"' in r.text


def test_pr_reference_text_uses_conversation_not_system_or_tool_traffic():
    events = [
        TranscriptEvent(seq=1, role="system", text="Memory mentions PR #250."),
        TranscriptEvent(
            seq=2,
            role="assistant",
            tool_name="exec_command",
            tool_summary="gh pr view 251",
            tool_detail="Rendered page contains PR #252",
        ),
        TranscriptEvent(seq=3, role="tool", text="PR #253 merged"),
        TranscriptEvent(seq=4, role="user", text="Please inspect PR #91."),
        TranscriptEvent(
            seq=5,
            role="assistant",
            text="Opened https://github.com/eerovil/agentdeck/pull/92.",
        ),
    ]

    assert _pr_reference_text(events) == (
        "Please inspect PR #91.\n"
        "Opened https://github.com/eerovil/agentdeck/pull/92."
    )


async def test_session_detail_uses_responsive_split_view(tmp_path):
    app = _app_with_state(tmp_path, with_transcript=True)
    app.state.assistant.config.enabled = True
    app.state.assistant.view = AssistantView(
        state="ready",
        summary="One chat needs attention.",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="waiting",
                headline="Confirm the database choice",
                detail="The agent needs the database decision before it can continue.",
            ),
        ),
    )
    app.state.assistant.contexts["claude_code:test:sid1"] = GitContext(
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
                title="Follow-up PR",
                url="https://github.com/eerovil/agentdeck/pull/92",
                status="open",
                draft=True,
            ),
        ),
    )
    async with _client(app) as client:
        response = await client.get("/sessions/claude_code:test:sid1")

    assert 'class="session-page session-stack-page"' in response.text
    assert 'class="session-layout"' in response.text
    assert 'class="session-sidebar" aria-label="All sessions"' in response.text
    assert 'class="session-detail" aria-label="Selected chat"' in response.text
    assert 'aria-current="page"' in response.text
    assert response.text.index('id="assistant-panel"') < response.text.index('id="sessions"')
    panel_start = response.text.index('id="assistant-panel"')
    panel_end = response.text.index("</section>", panel_start)
    assert "Confirm the database choice" in response.text[panel_start:panel_end]
    assert (
        "The agent needs the database decision before it can continue."
        not in response.text[panel_start:panel_end]
    )
    assert 'id="assistant-session-details"' in response.text
    assert 'class="assistant-chat-detail insight-waiting"' in response.text
    assert "The agent needs the database decision before it can continue." in response.text
    assert "feature/deckhand" in response.text
    assert 'class="chip pr-link pr-merged"' in response.text
    assert 'href="https://github.com/eerovil/agentdeck/pull/91"' in response.text
    assert "PR #91 · merged" in response.text
    assert 'class="chip pr-link pr-open"' in response.text
    assert 'href="https://github.com/eerovil/agentdeck/pull/92"' in response.text
    assert "PR #92 · draft" in response.text

    css = (Path(__file__).parents[1] / "src/agentdeck/web/static/app.css").read_text()
    html = response.text.replace("</head>", f"<style>{css}</style></head>")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 1200, "height": 800})
        await page.set_content(html)
        await page.evaluate(
            "document.querySelector('.session-detail').scrollTop = "
            "document.querySelector('.session-detail').scrollHeight"
        )
        desktop = await page.evaluate(
            """() => ({
                sidebar: getComputedStyle(document.querySelector('.session-sidebar')).display,
                columns: getComputedStyle(document.querySelector('.session-layout'))
                    .gridTemplateColumns,
                detailOverflow: getComputedStyle(document.querySelector('.session-detail'))
                    .overflowY,
                back: getComputedStyle(document.querySelector('.back')).display,
                assistantRects: document.querySelector('#assistant-panel').getClientRects().length,
                assistantAboveSessions:
                    document.querySelector('#assistant-panel').getBoundingClientRect().bottom <=
                    document.querySelector('#sessions').getBoundingClientRect().top,
                headerPosition: getComputedStyle(document.querySelector('.session-detail-head'))
                    .position,
                prLinksVisible: [...document.querySelectorAll('.pr-link')].every(link => {
                    const linkRect = link.getBoundingClientRect();
                    const detailRect = document.querySelector('.session-detail')
                        .getBoundingClientRect();
                    return linkRect.top >= detailRect.top && linkRect.bottom <= detailRect.bottom;
                }),
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
                assistantRects: document.querySelector('#assistant-panel').getClientRects().length,
                headerPosition: getComputedStyle(document.querySelector('.session-detail-head'))
                    .position,
            })"""
        )
        await page.set_viewport_size({"width": 320, "height": 800})
        narrow = await page.evaluate(
            """() => ({
                pageOverflow: document.documentElement.scrollWidth >
                    document.documentElement.clientWidth,
                timestampsVisible: [...document.querySelectorAll('.ev-time')].every(time => {
                    const rect = time.getBoundingClientRect();
                    const parent = time.closest('.ev-head, summary, .tool-call-head')
                        .getBoundingClientRect();
                    return rect.width > 0 && rect.left >= parent.left && rect.right <= parent.right;
                }),
            })"""
        )
        await browser.close()

    assert desktop["sidebar"] == "block"
    assert len(desktop["columns"].split()) == 2
    assert desktop["detailOverflow"] == "auto"
    assert desktop["back"] == "none"
    assert desktop["assistantRects"] == 1
    assert desktop["assistantAboveSessions"] is True
    assert desktop["headerPosition"] == "sticky"
    assert desktop["prLinksVisible"] is True
    assert mobile == {
        "sidebar": "block",
        "layout": "block",
        "detailOverflow": "auto",
        "back": "flex",
        "assistantRects": 1,
        "headerPosition": "static",
    }
    assert narrow == {"pageOverflow": False, "timestampsVisible": True}


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
    app.state.assistant.view = AssistantView(
        state="ready",
        summary="One chat needs attention.",
        insights=(
            AssistantInsight(
                session_key="claude_code:test:sid1",
                kind="waiting",
                headline="Confirm the database choice",
                detail="The database choice is blocking this chat.",
            ),
        ),
    )

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
        assistant = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
        assistant_session = await asyncio.wait_for(gen.__anext__(), timeout=5.0)
    finally:
        await gen.aclose()

    assert "event: usage" in usage
    assert "event: sessions" in sessions
    assert "event: assistant" in assistant
    assert "Attention triage" in assistant
    assert "The database choice is blocking this chat." not in assistant
    assert "event: assistant-session" in assistant_session
    assert "The database choice is blocking this chat." in assistant_session
    assert "Hello World Session" in sessions
    assert 'aria-current="page"' in sessions


async def test_session_sse_marks_parent_working_for_active_child(tmp_path):
    from agentdeck.web.routes_sse import _session_stream

    app = _app_with_state(tmp_path)
    parent_key = "claude_code:test:sid1"
    app.state.app_state.update_session(
        Session(
            key="claude_code:test:child",
            account_key="claude_code:test",
            session_id="child",
            status=SessionStatus.LIVE,
            thinking=True,
            parent_session_key=parent_key,
        )
    )

    class FakeRequest:
        def __init__(self, application):
            self.app = application

        async def is_disconnected(self):
            return False

    gen = _session_stream(FakeRequest(app), parent_key)
    try:
        events = [
            await asyncio.wait_for(gen.__anext__(), timeout=5.0)
            for _ in range(7)
        ]
    finally:
        await gen.aclose()

    status, subagents = events[-2:]
    assert "event: status" in status
    assert 'status-tag thinking' in status
    assert "event: subagents" in subagents
    assert "child" in subagents


async def test_dead_subagents_drop_from_the_session_card(tmp_path):
    # A finished Task/Agent subagent (its transcript went quiet, so `thinking`
    # aged out) is dead weight: it must stop padding the parent's sub-agent count
    # and nested rows. Only live subagents nest; when none are live, no pill.
    app = _app_with_state(tmp_path)
    state = app.state.app_state
    parent_key = "claude_code:test:sid1"

    def _subagent(sid, *, thinking, title):
        return Session(
            key=f"claude_code:test:{sid}",
            account_key="claude_code:test",
            session_id=sid,
            status=SessionStatus.LIVE if thinking else SessionStatus.IDLE,
            thinking=thinking,
            kind="subagent",
            title=title,
            is_delegated=not thinking,
            parent_session_key=parent_key,
            show_when_idle=True,
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )

    state.update_session(_subagent("sub-live", thinking=True, title="Live subagent"))
    state.update_session(_subagent("sub-done", thinking=False, title="Finished subagent"))

    async with _client(app) as client:
        r = await client.get("/")
    # Only the live subagent counts, and the finished one is gone entirely.
    assert "1 sub-agent" in r.text
    assert "2 sub-agent" not in r.text  # never the stale count that includes the dead one
    assert "Live subagent" in r.text
    assert "Finished subagent" not in r.text

    # When the last live subagent finishes, the pill disappears completely.
    state.update_session(_subagent("sub-live", thinking=False, title="Live subagent"))
    async with _client(app) as client:
        r2 = await client.get("/")
    assert "1 sub-agent" not in r2.text  # no count pill at all
    assert "Live subagent" not in r2.text  # and its row is gone too


async def test_waiting_pill_converts_to_done_when_dismissed_no_card_control(tmp_path):
    # What the card must do: a WAITING pill converts to DONE when the item is
    # dismissed from the EXISTING done control (triage panel / detail page, i.e.
    # POST /assistant/handle) — with NO extra button or click-action on the card
    # itself. The card just reflects state; the pill flip is automatic.
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    key = "claude_code:test:sid1"
    session = app.state.app_state.sessions[key]
    # A pending question makes the card show the WAITING pill.
    app.state.app_state.update_session(replace(session, question="Ship it?"))
    assistant.view = AssistantView(
        state="ready",
        summary="One item needs attention.",
        insights=(
            AssistantInsight(
                session_key=key,
                kind="waiting",
                headline="It asked whether to ship.",
                detail="Waiting on your answer.",
            ),
        ),
    )
    updated = app.state.app_state.sessions[key]
    assistant._signatures[key] = assistant._evidence_signature(updated, None, None)

    async with _client(app) as client:
        waiting = await client.get("/")
    # The card shows WAITING and carries NO card-level done control.
    assert "dh-pill dh-waiting" in waiting.text
    assert "done-btn" not in waiting.text
    assert "dh-actionable" not in waiting.text
    assert f'data-done-key="{key}"' not in waiting.text

    # Dismiss it exactly as the existing done control does.
    async with _client(app) as client:
        resp = await client.post("/assistant/handle", data={"session_key": key})
    assert resp.status_code == 200

    # The same card's pill has converted WAITING -> DONE — no reload action needed.
    async with _client(app) as client:
        done = await client.get("/")
    assert "dh-pill dh-done" in done.text
    assert "dh-pill dh-waiting" not in done.text
    assert "done-btn" not in done.text  # still no card control


async def test_question_waiting_dismissable_from_detail_and_reverts_on_new_message(tmp_path):
    # A chat merely WAITING on your answer (a trailing question, no Deckhand
    # insight) can be marked done from the detail-page done-bar. The card pill
    # flips WAITING -> DONE with no card control, and a NEW message auto-reverts
    # it back to WAITING.
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    state = app.state.app_state
    key = "claude_code:test:sid1"
    # A pending question, but NO Deckhand insight in the view (attention empty).
    state.update_session(
        replace(state.sessions[key], question="Take them on next, or stop here?",
                last_text="Want me to take them on next?", last_role="agent")
    )
    assert assistant.view.insights == ()

    # Detail done-bar offers "Deckhand done" even without an insight.
    async with _client(app) as client:
        detail = await client.get(f"/sessions/{key}")
    bar = detail.text.split('id="deckhand-done-bar"', 1)[1].split("</div>", 1)[0]
    assert 'hx-post="/assistant/handle"' in bar
    assert "Deckhand done" in bar

    # Card shows WAITING and carries no card-level control.
    async with _client(app) as client:
        before = await client.get("/")
    assert "dh-pill dh-waiting" in before.text
    assert "done-btn" not in before.text and "dh-actionable" not in before.text

    # Mark done via the existing route.
    async with _client(app) as client:
        resp = await client.post("/assistant/handle", data={"session_key": key})
    assert resp.status_code == 200
    assert assistant.is_handled(key)

    # Card pill converted to DONE; detail bar now offers Undo.
    async with _client(app) as client:
        done_page = await client.get("/")
    assert "dh-pill dh-done" in done_page.text
    assert "dh-pill dh-waiting" not in done_page.text
    async with _client(app) as client:
        detail2 = await client.get(f"/sessions/{key}")
    bar2 = detail2.text.split('id="deckhand-done-bar"', 1)[1].split("</div>", 1)[0]
    assert 'hx-post="/assistant/unhandle"' in bar2

    # Evidence churn WITHOUT a new message (git/PR context re-resolved, poll
    # noise) must NOT revert the dismissal — this is the bug the message
    # signature fixes; the old evidence-signature key reverted on every refresh.
    assistant._signatures[key] = "totally-different-evidence-signature"
    assert assistant.is_handled(key)
    async with _client(app) as client:
        still_done = await client.get("/")
    assert "dh-pill dh-done" in still_done.text

    # A NEW message (question / last text changes) auto-reverts to WAITING.
    state.update_session(
        replace(state.sessions[key], question="A different question now?",
                last_text="Actually, one more thing?", last_role="agent")
    )
    assert not assistant.is_handled(key)
    async with _client(app) as client:
        reverted = await client.get("/")
    assert "dh-pill dh-waiting" in reverted.text
    assert "dh-pill dh-done" not in reverted.text


async def test_waiting_insight_dismissal_uses_message_key_and_survives_refresh(tmp_path):
    # When a chat has a pending question AND Deckhand raised a waiting card,
    # marking it done must key on the MESSAGE signature (not the evidence one),
    # so it survives evidence churn and a refresh that re-creates the card —
    # instead of reverting on the next 60s tick.
    app = _app_with_state(tmp_path)
    assistant = app.state.assistant
    assistant.config.enabled = True
    assistant.push = None  # no fire-and-forget push tasks from refresh() in this test
    state = app.state.app_state
    key = "claude_code:test:sid1"
    state.update_session(
        replace(state.sessions[key], question="Ship it?",
                last_text="Ready to ship?", last_role="agent")
    )
    assistant.view = AssistantView(
        state="ready", summary="1",
        insights=(AssistantInsight(key, "waiting", "It asked whether to ship.", ""),),
    )
    assistant._signatures[key] = assistant._evidence_signature(state.sessions[key], None, None)

    async with _client(app) as client:
        resp = await client.post("/assistant/handle", data={"session_key": key})
    assert resp.status_code == 200
    # Routed to the message-signature store, not the evidence-signature one.
    assert assistant.dismissals.is_waiting_dismissed(key)
    assert assistant.dismissals.insight_signature(key) is None
    assert assistant.is_handled(key)

    # Evidence churn alone does not revert it.
    assistant._signatures[key] = "changed-evidence-signature"
    assert assistant.is_handled(key)

    # A refresh re-creates the waiting card, but it must be suppressed (not
    # resurfaced) and the pill stays DONE.
    await assistant.refresh()
    assert all(i.session_key != key for i in assistant.view.insights)
    assert assistant.is_handled(key)
    async with _client(app) as client:
        page = await client.get("/")
    assert "dh-pill dh-done" in page.text
    assert "dh-pill dh-waiting" not in page.text

    # A NEW message reverts to WAITING.
    state.update_session(
        replace(state.sessions[key], question="A different question?", last_text="One more?")
    )
    assert not assistant.is_handled(key)


async def test_model_picker_filters_options_by_account_provider(tmp_path):
    """model_picker.js keeps the model <select> in sync with the chosen account's
    provider, at desktop and the narrowest supported mobile width. The app fixture
    only wires a Codex account, so drive the JS directly against a two-provider
    form that mirrors the dashboard markup."""
    static_dir = Path(__file__).parents[1] / "src/agentdeck/web/static"
    css = (static_dir / "app.css").read_text()
    picker = (static_dir / "model_picker.js").read_text()
    html = f"""<!doctype html><html><head><style>{css}</style></head>
      <body><div class="new-chat" open><form data-model-picker>
        <select id="new-chat-account" name="account_key" data-model-picker-account>
          <option value="codex:a" data-provider="codex">Codex A</option>
          <option value="claude:b" data-provider="claude_code">Claude B</option>
        </select>
        <div class="model-field" id="new-chat-model-field" hidden data-model-picker-field>
          <label for="new-chat-model">Model</label>
          <select id="new-chat-model" name="model" data-model-picker-select>
            <option value="">Default (account)</option>
            <option value="gpt-5.6-luna" data-provider="codex">Luna</option>
            <option value="gpt-5.6-sol" data-provider="codex">Sol</option>
            <option value="opus" data-provider="claude_code">Opus 4.8</option>
            <option value="haiku" data-provider="claude_code">Haiku</option>
          </select>
        </div>
      </form></div>
      <div class="continue-chat"><form data-model-picker>
        <select id="continue-chat-account" data-model-picker-account>
          <option value="claude:b" data-provider="claude_code">Claude B</option>
          <option value="codex:a" data-provider="codex">Codex A</option>
        </select>
        <div id="continue-chat-model-field" hidden data-model-picker-field>
          <select id="continue-chat-model" data-model-picker-select>
            <option value="">Default (account)</option>
            <option value="gpt-5.6-luna" data-provider="codex">Luna</option>
            <option value="opus" data-provider="claude_code">Opus 4.8</option>
          </select>
        </div>
      </form></div></body></html>"""

    def options(page):
        return page.evaluate(
            """() => {
              const sel = document.getElementById('new-chat-model');
              const field = document.getElementById('new-chat-model-field');
              return {
                values: Array.from(sel.options).map(o => o.value),
                selected: sel.value,
                hidden: field.hidden,
              };
            }"""
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page(viewport={"width": 320, "height": 800})
        await page.set_content(html)
        await page.add_script_tag(content=picker)

        # Codex selected first: only its models (plus Default) are offered.
        first = await options(page)
        assert first["values"] == ["", "gpt-5.6-luna", "gpt-5.6-sol"]
        assert first["hidden"] is False

        # Pick a Codex model, then switch to the Claude account: the stale Codex
        # slug is dropped and selection falls back to Default.
        await page.select_option("#new-chat-model", "gpt-5.6-sol")
        await page.select_option("#new-chat-account", "claude:b")
        after = await options(page)
        assert after["values"] == ["", "opus", "haiku"]
        assert after["selected"] == ""
        assert after["hidden"] is False

        # Behaviour holds at desktop width too.
        await page.set_viewport_size({"width": 900, "height": 800})
        await page.select_option("#new-chat-account", "codex:a")
        desktop = await options(page)
        assert desktop["values"] == ["", "gpt-5.6-luna", "gpt-5.6-sol"]

        # A second continuation form is initialized independently.
        assert await page.locator("#continue-chat-model").evaluate(
            "select => Array.from(select.options).map(option => option.value)"
        ) == ["", "opus"]
        await page.select_option("#continue-chat-account", "codex:a")
        assert await page.locator("#continue-chat-model").evaluate(
            "select => Array.from(select.options).map(option => option.value)"
        ) == ["", "gpt-5.6-luna"]
        await browser.close()
