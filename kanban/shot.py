#!/usr/bin/env python3
"""Render AgentDeck pages in-process and screenshot them for a PR.

Fully isolated from any running instance and from live data: it builds the app
with ``create_app()`` over a synthetic in-memory state (one demo session with a
tiny transcript), renders each requested route through ASGITransport, inlines the
app CSS, screenshots the HTML in headless Chromium, and uploads the PNGs to the
repo's ``kanban-shots`` GitHub release — printing Markdown image tags to paste
into the PR body.

This is best-effort: a route that fails to render is skipped with a note, never
crashing the worker. It exercises exactly the pattern the browser test-suite uses
(ASGI render → inline CSS → ``page.set_content`` → screenshot), so it needs no
server, no port, no DB, and no live account access.

Usage:
    shot.py [--repo OWNER/NAME] [--number N] [--out DIR] [--width PX] ROUTE [ROUTE ...]

A ``{session}`` token in a route is replaced with the synthetic session key:
    shot.py "/" "/sessions/{session}"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_CSS = REPO_ROOT / "src" / "agentdeck" / "web" / "static" / "app.css"
SESSION_KEY = "claude_code:shot:demo"
RELEASE_TAG = "kanban-shots"


def _note(msg: str) -> None:
    print(f"[shot] {msg}", file=sys.stderr, flush=True)


def _route_slug(route: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-")
    return s or "root"


def _build_app(config_dir: Path):
    """A synthetic AgentDeck app with one demo session + a two-message transcript,
    mirroring tests/test_web.py::_app_with_state(with_transcript=True)."""
    from agentdeck.app import create_app
    from agentdeck.config import AccountConfig, AppConfig, HistoryConfig
    from agentdeck.models import Capability, Session, SessionStatus, UsageSnapshot

    proj = config_dir / "projects" / "-tmp"
    proj.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "type": "user",
            "timestamp": "2026-07-15T07:41:00Z",
            "message": {"role": "user", "content": "demo question"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-07-15T07:42:00Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "a demo answer"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]
    (proj / "demo.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))

    config = AppConfig(
        history=HistoryConfig(enabled=False),
        accounts=[AccountConfig(provider="claude_code", label="shot", config_dir=str(config_dir))],
    )
    app = create_app(config)
    state = app.state.app_state
    state.update_session(
        Session(
            key=SESSION_KEY,
            account_key="claude_code:shot",
            session_id="demo",
            status=SessionStatus.LIVE,
            title="Demo Session",
            capabilities=frozenset({Capability.TRANSCRIPT}),
        )
    )
    state.set_usage(
        UsageSnapshot(
            account_key="claude_code:shot",
            five_hour_pct=42.0,
            five_hour_resets_at=None,
            seven_day_pct=7.0,
            seven_day_resets_at=None,
            fetched_at=datetime.now(UTC),
        )
    )
    return app


async def _render(app, route: str) -> str | None:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://shot") as client:
        resp = await client.get(route)
    if resp.status_code != 200 or "<html" not in resp.text.lower():
        _note(f"skip {route}: status {resp.status_code}")
        return None
    return resp.text


async def _shoot(routes: list[str], out_dir: Path, width: int, number: int | None) -> list[Path]:
    from playwright.async_api import async_playwright

    css = STATIC_CSS.read_text() if STATIC_CSS.exists() else ""
    out_dir.mkdir(parents=True, exist_ok=True)
    shots: list[Path] = []
    with tempfile.TemporaryDirectory() as tmp:
        app = _build_app(Path(tmp))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            page = await browser.new_page(viewport={"width": width, "height": 900})
            for route in routes:
                resolved = route.replace("{session}", SESSION_KEY)
                html = await _render(app, resolved)
                if html is None:
                    continue
                html = html.replace("</head>", f"<style>{css}</style></head>", 1)
                await page.set_content(html, wait_until="networkidle")
                prefix = f"shot-{number}-" if number is not None else "shot-"
                path = out_dir / f"{prefix}{_route_slug(resolved)}.png"
                await page.screenshot(path=str(path), full_page=True)
                shots.append(path)
                _note(f"captured {resolved} → {path.name}")
            await browser.close()
    return shots


def _upload(repo: str, shots: list[Path]) -> None:
    """Upload PNGs to the repo's kanban-shots release and print Markdown tags."""
    have = subprocess.run(
        ["gh", "release", "view", RELEASE_TAG, "-R", repo],
        capture_output=True, text=True,
    ).returncode == 0
    if not have:
        subprocess.run(
            ["gh", "release", "create", RELEASE_TAG, "-R", repo,
             "-t", "kanban screenshots", "-n", "Auto-uploaded PR screenshots from kanban workers."],
            check=True, capture_output=True, text=True,
        )
    subprocess.run(
        ["gh", "release", "upload", RELEASE_TAG, "-R", repo, "--clobber", *[str(s) for s in shots]],
        check=True, capture_output=True, text=True,
    )
    print("\n<!-- kanban screenshots -->")
    for shot in shots:
        url = f"https://github.com/{repo}/releases/download/{RELEASE_TAG}/{shot.name}"
        print(f"![{shot.stem}]({url})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shot", description=__doc__)
    parser.add_argument("routes", nargs="+")
    parser.add_argument("--repo", default=os.environ.get("KANBAN_REPO", "eerovil/agentdeck"))
    parser.add_argument("--number", type=int)
    parser.add_argument("--out", default=str(Path(tempfile.gettempdir()) / "kanban-shots"))
    parser.add_argument("--width", type=int, default=430)
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args(argv)

    try:
        shots = asyncio.run(_shoot(args.routes, Path(args.out), args.width, args.number))
    except Exception as exc:  # noqa: BLE001 -- screenshots are best-effort; never fail the worker
        _note(f"screenshot render failed: {exc}")
        return 1
    if not shots:
        _note("no routes rendered; nothing to upload")
        return 1
    if args.no_upload:
        for shot in shots:
            print(shot)
        return 0
    try:
        _upload(args.repo, shots)
    except subprocess.CalledProcessError as exc:  # noqa: BLE001
        _note(f"upload failed: {exc.stderr or exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
