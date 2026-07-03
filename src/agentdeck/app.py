"""FastAPI application factory + lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .collector import Collector
from .config import AppConfig
from .db import make_db
from .state import AppState
from .web import render as render_mod
from .web.routes_pages import router as pages_router
from .web.routes_partials import router as partials_router
from .web.routes_sse import router as sse_router

log = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).parent / "web"
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def create_app(config: AppConfig) -> FastAPI:
    db = make_db(
        config.history.enabled, config.history.db_path, config.history.usage_retention_days
    )
    state = AppState(db=db)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    render_mod.register_filters(templates)
    collector = Collector(config, state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await collector.start()
        try:
            yield
        finally:
            await collector.stop()
            db.close()

    app = FastAPI(title="agentdeck", version="0.2.0", lifespan=lifespan)
    app.state.config = config
    app.state.app_state = state
    app.state.accounts = config.build_accounts()
    app.state.templates = templates
    app.state.collector = collector

    app.state.db = db

    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    app.include_router(pages_router)
    app.include_router(partials_router)
    app.include_router(sse_router)
    return app
