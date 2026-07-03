"""EventBus: per-topic asyncio pub/sub feeding SSE.

Publishers never block: each subscriber has a bounded queue and slow
consumers drop the oldest event (every published fragment is a full
re-render, so dropping intermediates is always safe).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any


class Subscription:
    """One subscriber queue registered under one or more topics."""

    def __init__(self, bus: EventBus, topics: tuple[str, ...], maxsize: int):
        self._bus = bus
        self.topics = topics
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=maxsize)

    async def get(self) -> tuple[str, Any]:
        return await self.queue.get()

    def get_nowait(self) -> tuple[str, Any] | None:
        try:
            return self.queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def _deliver(self, topic: str, data: Any) -> None:
        try:
            self.queue.put_nowait((topic, data))
        except asyncio.QueueFull:  # slow consumer: drop oldest, keep newest
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait((topic, data))

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self._bus._unsubscribe(self)


class EventBus:
    def __init__(self, maxsize: int = 64):
        self._maxsize = maxsize
        self._subs: dict[str, set[Subscription]] = {}

    def subscribe(self, *topics: str) -> Subscription:
        sub = Subscription(self, topics, self._maxsize)
        for topic in topics:
            self._subs.setdefault(topic, set()).add(sub)
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        for topic in sub.topics:
            self._subs.get(topic, set()).discard(sub)

    def publish(self, topic: str, data: Any = None) -> None:
        for sub in tuple(self._subs.get(topic, ())):
            sub._deliver(topic, data)
