"""SessionProvider ABC — the contract every session source implements.

Providers without a feature return sessions whose .capabilities simply omit
it (e.g. DEEPLINK) — the web layer keys off capabilities, never off provider
type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..models import (
    Account,
    Capability,  # re-exported: providers/base is the canonical import site  # noqa: F401
    InjectResult,
    PendingInteraction,
    Session,
    TranscriptEvent,
    UsageSnapshot,
)

if TYPE_CHECKING:
    from ..events import EventBus
    from ..state import AppState


class SessionProvider(ABC):
    provider_id: ClassVar[str]
    supports_new_session: ClassVar[bool] = False

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

    async def last_event(self, account: Account, session: Session) -> TranscriptEvent | None:
        """The most recent renderable event (for activity detection); None if
        the provider has no transcript."""
        return None

    def make_usage_poller(
        self, account: Account, state: AppState, bus: EventBus, **kwargs: Any
    ) -> Any | None:
        """Return an object with an ``async run()`` loop, or None if the
        provider has no usage-limit source."""
        return None

    async def start_account(self, account: Account, state: AppState) -> None:
        """Start optional provider-owned runtime services for one account."""
        return None

    async def stop_account(self, account: Account) -> None:
        """Stop optional provider-owned runtime services for one account."""
        return None

    async def inject(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
    ) -> InjectResult:
        """Append one turn to a safely injectable session."""
        return InjectResult(False, "this provider does not support injection")

    async def start_session(
        self,
        account: Account,
        cwd: Path,
        message: str,
        *,
        timeout_s: float,
        images: list[Path] | None = None,
        sandbox: str | None = None,
        model: str | None = None,
        approval_policy: str | None = None,
    ) -> InjectResult:
        """Start one persisted session with its first turn."""
        return InjectResult(False, "this provider cannot start sessions")

    async def wait_for_session(
        self,
        account: Account,
        session_id: str,
        *,
        timeout_s: float,
    ) -> InjectResult:
        """Wait for the active turn in a newly started session to finish."""
        return InjectResult(False, "this provider cannot wait for delegated sessions")

    async def session_result(self, account: Account, session_id: str) -> str | None:
        """Return the latest assistant text for a delegated session."""
        return None

    def owns_session(self, account: Account, session: Session) -> bool:
        return False

    def pending_interaction(
        self, account: Account, session: Session
    ) -> PendingInteraction | None:
        return None

    async def steer(
        self,
        account: Account,
        session: Session,
        message: str,
        *,
        images: list[Path] | None = None,
    ) -> InjectResult:
        return InjectResult(False, "this provider cannot steer active turns")

    async def interrupt(self, account: Account, session: Session) -> InjectResult:
        return InjectResult(False, "this provider cannot interrupt active turns")

    async def answer_interaction(
        self,
        account: Account,
        session: Session,
        interaction_id: str,
        *,
        answers: Mapping[str, list[str]],
        decision: str | None,
    ) -> InjectResult:
        return InjectResult(False, "this provider cannot answer interactions")
