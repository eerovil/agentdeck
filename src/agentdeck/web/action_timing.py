"""Low-noise timing and correlation for direct user actions."""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from fastapi import Request

log = logging.getLogger(__name__)

_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_SESSION_ACTION_RE = re.compile(
    r"^/sessions/(?P<session_key>[^/]+)/(?P<action>inject|steer|interrupt|interaction)$"
)


def _request_action(request: Request) -> tuple[str | None, str | None]:
    if request.method != "POST":
        return None, None
    match = _SESSION_ACTION_RE.match(request.url.path)
    if match:
        action = {
            "inject": "send",
            "steer": "steer",
            "interrupt": "stop",
            "interaction": "interaction",
        }[match.group("action")]
        return action, match.group("session_key")
    if request.url.path == "/sessions/new":
        return "new_session", None
    return None, None


@dataclass
class ActionTiming:
    client_action_id: str
    action: str
    session_key: str | None
    started_ns: int = field(default_factory=time.perf_counter_ns)
    provider: str | None = None
    spans_ms: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_request(cls, request: Request, action: str, session_key: str | None):
        supplied = request.headers.get("x-agentdeck-action-id", "")
        client_action_id = supplied if _ACTION_ID_RE.fullmatch(supplied) else uuid.uuid4().hex
        return cls(client_action_id, action, session_key)

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        started = time.perf_counter_ns()
        try:
            yield
        finally:
            self.spans_ms[name] = (time.perf_counter_ns() - started) / 1_000_000

    def bind(self, *, session_key: str | None = None, provider: str | None = None) -> None:
        if session_key:
            self.session_key = session_key
        if provider:
            self.provider = provider

    def finish(self, status_code: int) -> tuple[str, float]:
        total_ms = (time.perf_counter_ns() - self.started_ns) / 1_000_000
        parts = [
            f"{name};dur={duration:.1f}"
            for name, duration in self.spans_ms.items()
        ]
        parts.append(f"total;dur={total_ms:.1f}")
        log.debug(
            "action_timing action_id=%s action=%s session_key=%s provider=%s "
            "status=%d elapsed_ms=%.1f spans_ms=%s",
            self.client_action_id,
            self.action,
            self.session_key,
            self.provider,
            status_code,
            total_ms,
            {name: round(value, 1) for name, value in self.spans_ms.items()},
        )
        return ", ".join(parts), total_ms


def action_timing(request: Request) -> ActionTiming | None:
    value = getattr(request.state, "action_timing", None)
    return value if isinstance(value, ActionTiming) else None


@contextmanager
def timing_span(request: Request, name: str) -> Iterator[None]:
    timing = action_timing(request)
    if timing is None:
        yield
        return
    with timing.span(name):
        yield


def bind_action(
    request: Request, *, session_key: str | None = None, provider: str | None = None
) -> str | None:
    timing = action_timing(request)
    if timing is None:
        return None
    timing.bind(session_key=session_key, provider=provider)
    return timing.client_action_id


def identify_action(request: Request) -> tuple[str | None, str | None]:
    return _request_action(request)
