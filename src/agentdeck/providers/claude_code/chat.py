"""Interactive chat: a long-lived `claude` stream-json child per session.

Spawned as::

    claude -p --resume <id> --input-format stream-json \
           --output-format stream-json --verbose

We write user messages as JSON lines to stdin and parse the stream-json events
on stdout into normalized chat bubbles. One child per session (the caller holds
the lock); the child is the sole writer of that session's transcript while open.

The process is started in its own session/group (``start_new_session=True``) so
``stop()`` can signal the whole group, and a replay buffer lets a reconnecting
SSE viewer catch up on the conversation so far.

The flag set below is verified end-to-end against real Claude Code v2.1.198: a
resumed session appends the chat turn to the same transcript (not a fork).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal

log = logging.getLogger(__name__)


class ChatRefused(Exception):
    """Interlock refused opening a chat (session live / cwd / trust)."""


_ARGS = (
    "-p",
    "--resume",
    "{session_id}",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--verbose",
)


def _blocks_to_events(content: object, role: str) -> list[dict]:
    """Normalize a message.content into chat-bubble dicts."""
    out: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            out.append({"role": role, "text": content})
        return out
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and isinstance(block.get("text"), str) and block["text"].strip():
            out.append({"role": role, "text": block["text"]})
        elif btype == "tool_use":
            inp = block.get("input")
            summary = None
            if isinstance(inp, dict):
                for k in ("command", "file_path", "path", "query", "pattern", "description"):
                    if isinstance(inp.get(k), str) and inp[k]:
                        summary = f"{k}: {inp[k]}"[:160]
                        break
            out.append({"role": "tool", "tool_name": block.get("name"), "tool_summary": summary})
        elif btype == "tool_result":
            inner = block.get("content")
            text = inner if isinstance(inner, str) else ""
            if isinstance(inner, list):
                text = "\n".join(b.get("text", "") for b in inner if isinstance(b, dict))
            out.append({"role": "tool", "text": text[:2000]})
    return out


def normalize_stream_event(obj: dict) -> list[dict]:
    """Translate one stream-json output object into zero or more bubbles."""
    t = obj.get("type")
    if t == "assistant":
        msg = obj.get("message") or {}
        return _blocks_to_events(msg.get("content"), "assistant")
    if t == "user":
        msg = obj.get("message") or {}
        return _blocks_to_events(msg.get("content"), "tool")
    if t == "result":
        return [{"role": "system", "event": "turn-end"}]
    return []  # system/init and unknown types are ignored


class ChatSession:
    def __init__(
        self,
        session_id: str,
        *,
        cwd: str,
        config_dir: str,
        claude_bin: str = "claude",
    ):
        self.session_id = session_id
        self._cwd = cwd
        self._config_dir = config_dir
        self._claude_bin = claude_bin
        self.proc: asyncio.subprocess.Process | None = None
        self.owned_pid: int | None = None
        self.history: list[dict] = []  # replay buffer for reconnecting viewers
        self._waiters: set[asyncio.Event] = set()
        self._reader: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        args = tuple(a.format(session_id=self.session_id) for a in _ARGS)
        env = {**os.environ, "CLAUDE_CONFIG_DIR": self._config_dir}
        self.proc = await asyncio.create_subprocess_exec(
            self._claude_bin,
            *args,
            cwd=self._cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.owned_pid = self.proc.pid
        self._reader = asyncio.create_task(self._read_stdout())

    def _emit(self, event: dict) -> None:
        self.history.append(event)
        for w in tuple(self._waiters):
            w.set()

    async def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    for ev in normalize_stream_event(obj):
                        self._emit(ev)
        except Exception as exc:  # noqa: BLE001 — reader must never crash the app
            log.debug("chat reader error (%s): %s", self.session_id, exc)
        finally:
            self._closed = True
            self._emit({"role": "system", "event": "closed"})

    async def send(self, text: str) -> bool:
        if self._closed or not self.proc or not self.proc.stdin:
            return False
        msg = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        self._emit({"role": "user", "text": text})
        try:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            self._closed = True
            return False

    async def stream(self, start: int = 0):
        """Yield events from ``start`` onward, blocking for new ones."""
        idx = start
        ev = asyncio.Event()
        self._waiters.add(ev)
        try:
            while True:
                while idx < len(self.history):
                    yield idx, self.history[idx]
                    idx += 1
                if self._closed and idx >= len(self.history):
                    return
                ev.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ev.wait(), timeout=15.0)
        finally:
            self._waiters.discard(ev)

    @property
    def closed(self) -> bool:
        return self._closed

    async def stop(self) -> None:
        self._closed = True
        if self.proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
