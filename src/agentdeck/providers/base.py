"""SessionProvider ABC — the contract every session source implements.

Providers without a feature return sessions whose .capabilities simply omit
it (e.g. DEEPLINK) — the web layer keys off capabilities, never off provider
type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple

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


class ModelChoice(NamedTuple):
    """One selectable model for a provider's create/turn model picker.

    ``value`` is passed verbatim to the provider (``--model`` for Claude, the
    ``thread/start`` ``model`` param for Codex). An empty value never appears
    here — "account default" is rendered by the templates as a blank option.
    """

    value: str
    label: str


class SessionProvider(ABC):
    provider_id: ClassVar[str]
    supports_new_session: ClassVar[bool] = False
    # Curated models the UI offers for this provider at new-chat creation (empty
    # = no picker). The web layer validates any submitted model against this list
    # before it reaches a CLI/app-server, so an unknown slug is a 422, never a raw
    # arg. Model is fixed at spawn: the Claude worker is one long-lived process and
    # Codex binds model at thread/start, so neither switches model mid-session.
    selectable_models: ClassVar[tuple[ModelChoice, ...]] = ()

    @classmethod
    def is_valid_model(cls, model: str) -> bool:
        """True if ``model`` is one of this provider's offered slugs."""
        return any(choice.value == model for choice in cls.selectable_models)

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

    async def transcript_image(
        self, account: Account, session: Session, seq: int, image_index: int
    ) -> tuple[str, bytes] | None:
        """Return one transcript image as ``(media_type, bytes)``."""
        return None

    async def recent_conversation(
        self, account: Account, session: Session, limit: int = 4
    ) -> list[TranscriptEvent]:
        """Bounded recent user/assistant messages for lightweight summaries."""
        events = await self.read_transcript(account, session)
        return [event for event in events if event.role in ("user", "assistant")][-limit:]

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

    def can_start_session(self, account: Account) -> bool:
        """Whether this configured account can start sessions right now.

        Most providers have a static capability. Providers backed by optional
        runtime services can override this with account-scoped availability.
        """
        return self.supports_new_session

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

    def pending_interaction(
        self, account: Account, session: Session
    ) -> PendingInteraction | None:
        """Return only a currently actionable, runtime-available interaction."""
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
