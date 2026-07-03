"""SessionProvider ABC — the contract every session source implements.

Providers without a feature return sessions whose .capabilities simply omit
INJECT/CHAT — the web layer keys off capabilities, never off provider type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import (
    Account,
    Capability,  # re-exported: providers/base is the canonical import site  # noqa: F401
    ChatHandle,
    InjectResult,
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

    @abstractmethod
    async def inject(self, account: Account, session: Session, message: str) -> InjectResult: ...

    @abstractmethod
    async def open_chat(self, account: Account, session: Session) -> ChatHandle: ...

    # --- optional hooks (non-abstract) ---------------------------------

    def sweep_liveness(self, account: Account, sessions: list[Session]) -> list[Session]:
        """Cheap pid-liveness recheck; return only the sessions that changed."""
        return []

    def make_usage_poller(
        self, account: Account, state: AppState, bus: EventBus, **kwargs: Any
    ) -> Any | None:
        """Return an object with an ``async run()`` loop, or None if the
        provider has no usage-limit source."""
        return None
