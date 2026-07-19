"""FastAPI application factory + lifespan wiring."""

from __future__ import annotations

import logging
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .action_context import client_action_context
from .assistant import AssistantService
from .collector import Collector
from .config import AppConfig
from .db import make_db
from .inject import InjectionService
from .providers.codex.runtime_client import runtime_socket_path
from .push import PushService
from .state import AppState
from .titles import TitleService
from .web import render as render_mod
from .web.action_timing import ActionTiming, identify_action
from .web.routes_actions import router as actions_router
from .web.routes_api import router as api_router
from .web.routes_files import router as files_router
from .web.routes_pages import router as pages_router
from .web.routes_partials import router as partials_router
from .web.routes_push import router as push_router
from .web.routes_pwa import cache_stamp
from .web.routes_pwa import router as pwa_router
from .web.routes_sse import router as sse_router

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"

VERSION = "0.3.1"


def _build_id() -> str:
    """Short git SHA of the running tree, shown in the footer so you can tell at
    a glance (on the phone) whether an install picked up the latest code. Falls
    back to 'dev' outside a checkout."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent.parent.parent),
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip() or "dev"
    except Exception:
        return "dev"


def create_app(config: AppConfig) -> FastAPI:
    db = make_db(
        config.history.enabled, config.history.db_path, config.history.usage_retention_days
    )
    state = AppState(db=db)
    # Show "stale" usage only when the data is genuinely old — at least a few
    # poll cycles behind — rather than after a single rate-limited poll (issue #6).
    state.usage_stale_after_s = max(3 * config.polling.usage_interval_s, 300.0)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    render_mod.register_filters(templates)
    templates.env.globals["app_version"] = VERSION
    templates.env.globals["build_id"] = _build_id()
    # Content hash appended to static asset URLs so a changed file is fetched
    # fresh — a request for app.css?v=<new> misses any stale SW/HTTP cache keyed
    # to the old URL and falls through to the network.
    templates.env.globals["asset_ver"] = cache_stamp()
    collector = Collector(config, state)
    injector = InjectionService(
        config.inject,
        on_change=lambda _session_key: state.bus.publish("sessions"),
        on_delegation_started=state.mark_delegated_session,
    )
    push = PushService(config.push, db)
    assistant = AssistantService(config, state, push=push)
    titles = TitleService(config, state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Proxy channel to the long-lived runtime service (deck-owned Claude
        # workers). Lazy per-request use; unreachable → the route returns 503.
        app.state.runtime_http = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(runtime_socket_path())),
            base_url="http://agentdeck-runtime",
            timeout=httpx.Timeout(30.0, read=None),
        )
        push.start()
        await collector.start()
        await titles.start()
        await assistant.start()
        try:
            yield
        finally:
            await assistant.stop()
            await titles.stop()
            await injector.stop()
            await collector.stop()
            await app.state.runtime_http.aclose()
            db.close()

    app = FastAPI(title="agentdeck", version=VERSION, lifespan=lifespan)
    app.state.config = config
    app.state.app_state = state
    app.state.accounts = config.build_accounts()
    app.state.templates = templates
    app.state.collector = collector
    app.state.injector = injector
    app.state.assistant = assistant
    app.state.titles = titles
    app.state.push = push
    app.state.db = db

    @app.middleware("http")
    async def measure_direct_actions(request, call_next):
        action, session_key = identify_action(request)
        if action is None:
            return await call_next(request)
        timing = ActionTiming.from_request(request, action, session_key)
        request.state.action_timing = timing
        with client_action_context(timing.client_action_id):
            response = await call_next(request)
        server_timing, _ = timing.finish(response.status_code)
        response.headers["Server-Timing"] = server_timing
        response.headers["X-AgentDeck-Action-ID"] = timing.client_action_id
        return response

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(pages_router)
    app.include_router(api_router)
    app.include_router(actions_router)
    app.include_router(partials_router)
    app.include_router(sse_router)
    app.include_router(pwa_router)
    app.include_router(push_router)
    # Deliberately last: the local-file route is a catch-all for absolute paths
    # such as /tmp/report.md:12 and must never shadow AgentDeck's own routes.
    app.include_router(files_router)
    return app
