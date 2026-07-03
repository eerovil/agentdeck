"""SessionProvider ABC — the contract every session source implements.

Providers without a feature return sessions whose .capabilities simply omit
it (e.g. DEEPLINK) — the web layer keys off capabilities, never off provider
type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import (
    Account,
    Capability,  # re-exported: providers/base is the canonical import site  # noqa: F401
    Session,
    TranscriptEvent,
    UsageSnapshot,
)

if TYPE_CHECKING:
    from ..events import EventBus
    from ..state import AppState


class SessionProvider(ABC):
    provider_id: ClassVar[str]

    @abstractmethod
    async def scan_sessions(self, account: Account) -> list[Session]: ...

    @abstractmethod
    def watch_paths(self, account: Account) -> list[Path]:  # for watchfiles (v0.2+)
        ...

    @abstractmethod
    async def read_transcript(
        self, account: Account, session: Session, after_seq: int = 0
    ) -> list[TranscriptEvent]: ...

    @abstractmethod
    async def fetch_usage(self, account: Account) -> UsageSnapshot | None: ...

    # --- optional hooks (non-abstract) ---------------------------------

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Cheap pid-liveness recheck; return only the sessions that changed."""
        return []

    async def transcript_cursor(self, account: Account, session: Session) -> tuple[int, int]:
        """Current end-of-transcript cursor ``(byte_offset, seq)`` — the point a
        live tail should start from so it streams only subsequent events."""
        return (0, 0)

    async def tail_transcript(
        self, account: Account, session: Session, byte_offset: int, seq: int
    ) -> tuple[list[TranscriptEvent], int, int]:
        """Incremental read from a cursor → (new_events, byte_offset, seq)."""
        return ([], byte_offset, seq)

    def make_usage_poller(
        self, account: Account, state: AppState, bus: EventBus, **kwargs: Any
    ) -> Any | None:
        """Return an object with an ``async run()`` loop, or None if the
        provider has no usage-limit source."""
        return None
