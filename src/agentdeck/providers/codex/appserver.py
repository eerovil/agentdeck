"""Persistent Codex app-server client for agentdeck-owned chats."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...models import (
    Account,
    InjectResult,
    InteractionOption,
    InteractionQuestion,
    PendingInteraction,
)
from . import (
    NETWORK_ACCESS_CONFIG_OVERRIDE,
    WEB_SEARCH_CONFIG_OVERRIDE,
    WRITABLE_ROOTS_CONFIG_OVERRIDE,
)

log = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 30.0

# The app-server emits one JSON-RPC message per stdout line. asyncio's default
# StreamReader limit is 64 KiB, so a single large event (e.g. a big tool result)
# raises LimitOverrunError ("Separator is found, but chunk is longer than
# limit") and kills the whole turn, dropping Codex's final message. Raise the
# per-line buffer so realistic tool output does not break the transport.
STDOUT_LINE_LIMIT = 16 * 1024 * 1024


class AppServerError(RuntimeError):
    """The Codex app-server could not complete a request."""


@dataclass
class _PendingRequest:
    rpc_id: int | str
    method: str
    params: dict[str, Any]
    interaction: PendingInteraction


def _status_type(value: object) -> str:
    if isinstance(value, dict) and isinstance(value.get("type"), str):
        return value["type"]
    return "notLoaded"


def _safe_http_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    return value if parsed.scheme in ("http", "https") and parsed.netloc else None


def _turn_input(message: str, images: list[Path] | None = None) -> list[dict[str, str]]:
    """Build app-server user input items."""
    items = [{"type": "text", "text": message}]
    items.extend({"type": "localImage", "path": str(path)} for path in images or [])
    return items


def _questions(params: dict[str, Any]) -> tuple[InteractionQuestion, ...]:
    result = []
    raw_questions = params.get("questions")
    if not isinstance(raw_questions, list):
        return ()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        question_id = raw.get("id")
        prompt = raw.get("question")
        if not isinstance(question_id, str) or not isinstance(prompt, str):
            continue
        options = []
        for option in raw.get("options") or []:
            if not isinstance(option, dict) or not isinstance(option.get("label"), str):
                continue
            options.append(
                InteractionOption(
                    option["label"],
                    option.get("description") if isinstance(option.get("description"), str) else "",
                )
            )
        result.append(
            InteractionQuestion(
                id=question_id,
                header=raw.get("header") if isinstance(raw.get("header"), str) else "Question",
                prompt=prompt,
                options=tuple(options),
                allow_other=bool(raw.get("isOther")),
                secret=bool(raw.get("isSecret")),
            )
        )
    return tuple(result)


def _mcp_questions(params: dict[str, Any]) -> tuple[InteractionQuestion, ...]:
    schema = params.get("requestedSchema")
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return ()
    required = set(schema.get("required") or [])
    result = []
    for name, raw in schema["properties"].items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            continue
        options: list[InteractionOption] = []
        labels = raw.get("enumNames")
        values = raw.get("enum")
        if isinstance(values, list):
            for index, value in enumerate(values):
                if not isinstance(value, str):
                    continue
                label = (
                    labels[index]
                    if isinstance(labels, list)
                    and index < len(labels)
                    and isinstance(labels[index], str)
                    else value
                )
                options.append(InteractionOption(label=label, value=value))
        for option in raw.get("oneOf") or []:
            if isinstance(option, dict) and isinstance(option.get("const"), str):
                options.append(
                    InteractionOption(
                        label=option.get("title")
                        if isinstance(option.get("title"), str)
                        else option["const"],
                        value=option["const"],
                    )
                )
        result.append(
            InteractionQuestion(
                id=name,
                header=raw.get("title") if isinstance(raw.get("title"), str) else name,
                prompt=raw.get("description")
                if isinstance(raw.get("description"), str)
                else name,
                options=tuple(options),
                allow_other=not options,
                secret=False,
            )
        )
        if name not in required and result[-1].prompt == name:
            result[-1] = InteractionQuestion(
                **{**result[-1].__dict__, "prompt": f"{name} (optional)"}
            )
    return tuple(result)


class CodexAppServer:
    """One long-lived, single-owner app-server process for a Codex account."""

    def __init__(
        self,
        account: Account,
        *,
        on_change: Callable[[str], None] | None = None,
        process_factory=None,
    ) -> None:
        self.account = account
        self._on_change = on_change or (lambda _thread_id: None)
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._start_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._responses: dict[int, asyncio.Future] = {}
        self._owned: set[str] = set()
        self._loaded: set[str] = set()
        self._threads: dict[str, dict[str, Any]] = {}
        self._active_turn: dict[str, str] = {}
        self._turn_waiters: dict[str, asyncio.Future] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self._interactions: dict[str, _PendingRequest] = {}

    async def start(self) -> None:
        async with self._start_lock:
            if self._process is not None and self._process.returncode is None:
                return
            env = os.environ.copy()
            env["CODEX_HOME"] = str(self.account.root)
            try:
                self._process = await self._process_factory(
                    "codex",
                    "app-server",
                    "--config",
                    WEB_SEARCH_CONFIG_OVERRIDE,
                    "--config",
                    NETWORK_ACCESS_CONFIG_OVERRIDE,
                    "--config",
                    WRITABLE_ROOTS_CONFIG_OVERRIDE,
                    "--stdio",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                    limit=STDOUT_LINE_LIMIT,
                )
            except (OSError, ValueError) as exc:
                self._process = None
                raise AppServerError(f"could not start Codex app-server: {exc}") from exc
            self._reader_task = asyncio.create_task(
                self._read_loop(), name=f"codex-app-server:{self.account.key}"
            )
            try:
                await self._request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "agentdeck",
                            "title": "agentdeck",
                            "version": "0.3.1",
                        },
                        "capabilities": {
                            "experimentalApi": True,
                            "mcpServerOpenaiFormElicitation": True,
                        },
                    },
                )
                await self._notify("initialized", {})
                await self._recover_owned()
            except Exception:
                await self.stop()
                raise

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.stdin is not None:
            process.stdin.close()
        if process is not None and process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        self._fail_pending(AppServerError("Codex app-server stopped"))

    def _fail_pending(self, exc: Exception) -> None:
        for future in (*self._responses.values(), *self._turn_waiters.values()):
            if not future.done():
                future.set_exception(exc)
        self._responses.clear()
        self._turn_waiters.clear()

    async def _write(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerError("Codex app-server is not running")
        async with self._write_lock:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()

    async def _request(
        self, method: str, params: dict[str, Any], *, timeout_s: float = REQUEST_TIMEOUT_S
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            result = await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError as exc:
            raise AppServerError(f"Codex app-server {method} timed out") from exc
        finally:
            self._responses.pop(request_id, None)
        if not isinstance(result, dict):
            raise AppServerError(f"Codex app-server returned an invalid {method} result")
        return result

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id in self._responses and "method" not in message:
                    future = self._responses[request_id]
                    if "error" in message:
                        future.set_exception(AppServerError(str(message["error"])))
                    else:
                        future.set_result(message.get("result"))
                    continue
                method = message.get("method")
                params = message.get("params")
                if isinstance(method, str) and isinstance(params, dict):
                    if request_id is not None:
                        self._server_request(request_id, method, params)
                    else:
                        self._notification(method, params)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- reader failure is reported to callers
            log.exception("Codex app-server reader failed for %s", self.account.key)
            self._fail_pending(AppServerError(str(exc)))
        finally:
            if self._process is process:
                self._process = None

    async def _recover_owned(self) -> None:
        result = await self._request(
            # Codex currently persists threads started through app-server with
            # source kind ``vscode``. Include both persisted kinds; the list
            # response loses ``threadSource`` after restart, so ownership must
            # be recovered from AgentDeck's durable transcript originator.
            "thread/list", {"sourceKinds": ["appServer", "vscode"], "limit": 200}
        )
        for thread in result.get("data") or []:
            if not isinstance(thread, dict) or not self._was_started_by_agentdeck(thread):
                continue
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._owned.add(thread_id)
                self._threads[thread_id] = thread

    def _was_started_by_agentdeck(self, thread: dict[str, Any]) -> bool:
        """Check the persisted session marker without trusting arbitrary paths."""
        raw_path = thread.get("path")
        if not isinstance(raw_path, str):
            return False
        try:
            path = Path(raw_path).resolve()
            sessions_root = (self.account.root / "sessions").resolve()
            path.relative_to(sessions_root)
            with path.open(encoding="utf-8") as handle:
                first = json.loads(handle.readline())
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        payload = first.get("payload") if isinstance(first, dict) else None
        source = payload.get("source") if isinstance(payload, dict) else None
        return (
            first.get("type") == "session_meta"
            and isinstance(payload, dict)
            and payload.get("originator") == "agentdeck"
            and payload.get("thread_source") != "subagent"
            and not (isinstance(source, dict) and "subagent" in source)
        )

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if method == "thread/started":
            thread = params.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                thread_id = thread["id"]
                self._threads[thread_id] = thread
                self._loaded.add(thread_id)
        elif method == "thread/status/changed" and isinstance(thread_id, str):
            self._threads.setdefault(thread_id, {})["status"] = params.get("status")
        elif method == "thread/closed" and isinstance(thread_id, str):
            self._loaded.discard(thread_id)
        elif method == "turn/started" and isinstance(thread_id, str):
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                self._active_turn[thread_id] = turn["id"]
        elif method == "turn/completed" and isinstance(thread_id, str):
            turn = params.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                turn_id = turn["id"]
                self._active_turn.pop(thread_id, None)
                waiter = self._turn_waiters.pop(turn_id, None)
                if waiter is not None and not waiter.done():
                    waiter.set_result(turn)
                else:
                    self._completed_turns[turn_id] = turn
        elif method == "serverRequest/resolved":
            resolved = params.get("requestId")
            for token, request in list(self._interactions.items()):
                if request.rpc_id == resolved:
                    del self._interactions[token]
        if isinstance(thread_id, str):
            self._on_change(thread_id)

    def _server_request(self, request_id: int | str, method: str, params: dict[str, Any]) -> None:
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str) or thread_id not in self._owned:
            return
        token = secrets.token_urlsafe(18)
        turn_id = params.get("turnId") if isinstance(params.get("turnId"), str) else None
        if method == "item/tool/requestUserInput":
            interaction = PendingInteraction(
                id=token,
                kind="question",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Codex needs your answer",
                questions=_questions(params),
            )
        elif method == "item/commandExecution/requestApproval":
            network = params.get("networkApprovalContext")
            network_message = None
            if isinstance(network, dict):
                protocol = network.get("protocol", "network")
                network_message = f"{protocol}://{network.get('host', '')}"
            interaction = PendingInteraction(
                id=token,
                kind="command_approval",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Approve command?",
                message=network_message
                or (params.get("reason") if isinstance(params.get("reason"), str) else None),
                command=params.get("command") if isinstance(params.get("command"), str) else None,
                cwd=str(params["cwd"]) if params.get("cwd") is not None else None,
                decisions=("accept", "acceptForSession", "decline", "cancel"),
            )
        elif method == "item/fileChange/requestApproval":
            interaction = PendingInteraction(
                id=token,
                kind="file_approval",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Approve file changes?",
                message=params.get("reason") if isinstance(params.get("reason"), str) else None,
                cwd=params.get("grantRoot")
                if isinstance(params.get("grantRoot"), str)
                else None,
                decisions=("accept", "acceptForSession", "decline", "cancel"),
            )
        elif method == "item/permissions/requestApproval":
            interaction = PendingInteraction(
                id=token,
                kind="permission",
                thread_id=thread_id,
                turn_id=turn_id,
                title="Grant additional permissions?",
                message=params.get("reason") if isinstance(params.get("reason"), str) else None,
                cwd=params.get("cwd") if isinstance(params.get("cwd"), str) else None,
                decisions=("accept", "acceptForSession", "decline", "cancel"),
            )
        elif method == "mcpServer/elicitation/request":
            mode = params.get("mode")
            interaction = PendingInteraction(
                id=token,
                kind="mcp_url" if mode == "url" else "mcp_form",
                thread_id=thread_id,
                turn_id=turn_id,
                title=f"Input requested by {params.get('serverName', 'an app')}",
                message=params.get("message") if isinstance(params.get("message"), str) else None,
                questions=_mcp_questions(params),
                url=_safe_http_url(params.get("url")),
                decisions=("accept", "decline", "cancel"),
            )
        else:
            return
        self._interactions[token] = _PendingRequest(request_id, method, params, interaction)
        self._on_change(thread_id)

    def owns(self, thread_id: str) -> bool:
        return thread_id in self._owned

    def active_turn(self, thread_id: str) -> str | None:
        return self._active_turn.get(thread_id)

    def thread_status(self, thread_id: str) -> str:
        if thread_id in self._active_turn:
            return "active"
        return _status_type(self._threads.get(thread_id, {}).get("status"))

    def interaction(self, thread_id: str) -> PendingInteraction | None:
        for request in reversed(tuple(self._interactions.values())):
            if request.interaction.thread_id == thread_id:
                return request.interaction
        return None

    async def start_thread(
        self,
        cwd: Path,
        message: str,
        *,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
    ) -> InjectResult:
        await self.start()
        params = {
            "cwd": str(cwd),
            "ephemeral": False,
            "threadSource": "agentdeck",
        }
        if sandbox is not None:
            params["sandbox"] = sandbox
        if model is not None:
            params["model"] = model
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        result = await self._request(
            "thread/start",
            params,
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            return InjectResult(False, "Codex did not return a new thread id")
        thread_id = thread["id"]
        self._owned.add(thread_id)
        self._loaded.add(thread_id)
        self._threads[thread_id] = thread
        self._on_change(thread_id)
        await self.start_turn(thread_id, message, images=images, wait=False)
        return InjectResult(True, session_id=thread_id)

    async def _ensure_loaded(self, thread_id: str) -> None:
        if thread_id in self._loaded:
            return
        await self._request("thread/resume", {"threadId": thread_id})
        self._loaded.add(thread_id)

    async def start_turn(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
        wait: bool = True,
    ) -> InjectResult:
        await self.start()
        if thread_id not in self._owned:
            return InjectResult(False, "agentdeck does not own this Codex thread")
        await self._ensure_loaded(thread_id)
        if self.active_turn(thread_id) is not None:
            return InjectResult(False, "Codex is already working on this thread")
        result = await self._request(
            "turn/start",
            {"threadId": thread_id, "input": _turn_input(message, images)},
        )
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            return InjectResult(False, "Codex did not start the turn")
        turn_id = turn["id"]
        self._active_turn[thread_id] = turn_id
        self._on_change(thread_id)
        if not wait:
            return InjectResult(True)
        completed = await self._wait_for_turn(turn_id)
        status = completed.get("status") if isinstance(completed, dict) else None
        if status in ("completed", "interrupted"):
            reason = None if status == "completed" else "turn interrupted"
            return InjectResult(status == "completed", reason)
        return InjectResult(False, f"Codex turn ended with status {status or 'unknown'}")

    async def _wait_for_turn(self, turn_id: str) -> dict[str, Any]:
        completed = self._completed_turns.pop(turn_id, None)
        if completed is None:
            future = self._turn_waiters.get(turn_id)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._turn_waiters[turn_id] = future
            completed = await future
        return completed

    async def queue_turn(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
    ) -> InjectResult:
        """Wait for the active turn, then run one ordinary follow-up turn."""
        active = self.active_turn(thread_id)
        if active is not None:
            await self._wait_for_turn(active)
        return await self.start_turn(thread_id, message, images=images)

    async def wait_for_thread(self, thread_id: str) -> InjectResult:
        turn_id = self.active_turn(thread_id)
        if turn_id is None:
            return InjectResult(True)
        completed = await self._wait_for_turn(turn_id)
        status = completed.get("status") if isinstance(completed, dict) else None
        if status == "completed":
            return InjectResult(True)
        if status == "interrupted":
            return InjectResult(False, "turn interrupted")
        return InjectResult(False, f"Codex turn ended with status {status or 'unknown'}")

    async def steer(
        self,
        thread_id: str,
        message: str,
        *,
        images: list[Path] | None = None,
    ) -> InjectResult:
        await self.start()
        turn_id = self.active_turn(thread_id)
        if thread_id not in self._owned or turn_id is None:
            return InjectResult(False, "there is no agentdeck-owned active turn")
        await self._request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": _turn_input(message, images),
            },
        )
        return InjectResult(True)

    async def interrupt(self, thread_id: str) -> InjectResult:
        await self.start()
        turn_id = self.active_turn(thread_id)
        if thread_id not in self._owned or turn_id is None:
            return InjectResult(False, "there is no agentdeck-owned active turn")
        await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        return InjectResult(True)

    async def answer(
        self,
        thread_id: str,
        interaction_id: str,
        *,
        answers: Mapping[str, list[str]],
        decision: str | None = None,
    ) -> InjectResult:
        request = self._interactions.get(interaction_id)
        if request is None or request.interaction.thread_id != thread_id:
            return InjectResult(False, "this interaction is no longer pending")
        method = request.method
        if method == "item/tool/requestUserInput":
            result: dict[str, Any] = {
                "answers": {key: {"answers": values} for key, values in answers.items()}
            }
        elif method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            allowed = request.interaction.decisions
            if decision not in allowed:
                return InjectResult(False, "invalid approval decision")
            result = {"decision": decision}
        elif method == "item/permissions/requestApproval":
            allowed = request.interaction.decisions
            if decision not in allowed:
                return InjectResult(False, "invalid permission decision")
            result = {
                "permissions": request.params.get("permissions", {})
                if decision in ("accept", "acceptForSession")
                else {},
                "scope": "session" if decision == "acceptForSession" else "turn",
            }
        elif method == "mcpServer/elicitation/request":
            if decision not in request.interaction.decisions:
                return InjectResult(False, "invalid elicitation decision")
            content = {
                key: values[0] if len(values) == 1 else values for key, values in answers.items()
            }
            result = {
                "action": decision,
                "content": content if decision == "accept" else None,
            }
        else:
            return InjectResult(False, "unsupported interaction")
        await self._write({"id": request.rpc_id, "result": result})
        del self._interactions[interaction_id]
        self._on_change(thread_id)
        if decision == "cancel" and method == "item/permissions/requestApproval":
            await self.interrupt(thread_id)
        return InjectResult(True)
