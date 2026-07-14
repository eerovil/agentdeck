"""Long-lived Codex control plane, separate from the restartable web UI."""

from __future__ import annotations

import asyncio
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import AppConfig
from .models import Account, InjectResult
from .providers.codex.appserver import CodexAppServer
from .providers.codex.runtime_client import runtime_socket_path


class TurnRequest(BaseModel):
    thread_id: str
    message: str
    images: list[str] = Field(default_factory=list)


class ThreadRequest(BaseModel):
    thread_id: str


class StartRequest(BaseModel):
    cwd: str
    message: str
    images: list[str] = Field(default_factory=list)
    sandbox: str | None = None
    model: str | None = None
    approval_policy: str | None = None


class AnswerRequest(BaseModel):
    thread_id: str
    interaction_id: str
    answers: dict[str, list[str]] = Field(default_factory=dict)
    decision: str | None = None


def _result(result: InjectResult) -> dict[str, Any]:
    return asdict(result)


class CodexRuntime:
    """Own all app-server processes and pending interactions across web deploys."""

    def __init__(self, config: AppConfig) -> None:
        self.accounts = {
            account.label: account
            for account in config.build_accounts()
            if account.provider_id == "codex"
        }
        self.clients: dict[str, CodexAppServer] = {}

    async def start(self) -> None:
        try:
            for label, account in self.accounts.items():
                client = CodexAppServer(account)
                await client.start()
                self.clients[label] = client
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        await asyncio.gather(
            *(client.stop() for client in self.clients.values()),
            return_exceptions=True,
        )
        self.clients.clear()

    def client(self, label: str) -> CodexAppServer:
        client = self.clients.get(label)
        if client is None:
            raise HTTPException(status_code=404, detail="unknown Codex account")
        return client

    def snapshot(self, label: str) -> dict[str, Any]:
        client = self.client(label)
        threads = {}
        for thread_id in sorted(client.owned_threads()):
            interaction = client.interaction(thread_id)
            threads[thread_id] = {
                "active_turn": client.active_turn(thread_id),
                "status": client.thread_status(thread_id),
                "interaction": asdict(interaction) if interaction is not None else None,
            }
        return {"threads": threads}


def _paths(values: list[str]) -> list[Path]:
    return [Path(value) for value in values]


def _preserve_images(account: Account, values: list[str]) -> tuple[list[Path], Path | None]:
    """Copy uploads before a web restart can remove its temporary originals."""
    if not values:
        return [], None
    root = runtime_socket_path().parent / "uploads" / account.label / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    copied = []
    try:
        for value in values:
            source = Path(value)
            target = root / source.name
            shutil.copy2(source, target)
            copied.append(target)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return copied, root


def create_runtime_app(config: AppConfig) -> FastAPI:
    runtime = CodexRuntime(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="agentdeck-codex-runtime", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "accounts": sorted(runtime.clients)}

    @app.get("/accounts/{label}/state")
    async def state(label: str) -> dict[str, Any]:
        return runtime.snapshot(label)

    @app.post("/accounts/{label}/start")
    async def start(label: str, body: StartRequest) -> dict[str, Any]:
        client = runtime.client(label)
        return _result(
            await client.start_thread(
                Path(body.cwd),
                body.message,
                images=_paths(body.images),
                sandbox=body.sandbox,
                model=body.model,
                approval_policy=body.approval_policy,
            )
        )

    @app.post("/accounts/{label}/queue")
    async def queue(label: str, body: TurnRequest) -> dict[str, Any]:
        client = runtime.client(label)
        images, root = _preserve_images(client.account, body.images)
        try:
            return _result(await client.queue_turn(body.thread_id, body.message, images=images))
        finally:
            if root is not None:
                shutil.rmtree(root, ignore_errors=True)

    @app.post("/accounts/{label}/wait")
    async def wait(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(await runtime.client(label).wait_for_thread(body.thread_id))

    @app.post("/accounts/{label}/steer")
    async def steer(label: str, body: TurnRequest) -> dict[str, Any]:
        return _result(
            await runtime.client(label).steer(
                body.thread_id, body.message, images=_paths(body.images)
            )
        )

    @app.post("/accounts/{label}/interrupt")
    async def interrupt(label: str, body: ThreadRequest) -> dict[str, Any]:
        return _result(await runtime.client(label).interrupt(body.thread_id))

    @app.post("/accounts/{label}/answer")
    async def answer(label: str, body: AnswerRequest) -> dict[str, Any]:
        return _result(
            await runtime.client(label).answer(
                body.thread_id,
                body.interaction_id,
                answers=body.answers,
                decision=body.decision,
            )
        )

    return app
