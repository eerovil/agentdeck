"""Provider registry. The web layer resolves providers only through this."""

from ..models import Account, PendingInteraction, Session
from .base import SessionProvider
from .claude_code.provider import ClaudeCodeProvider
from .codex.provider import CodexProvider

PROVIDERS: dict[str, SessionProvider] = {
    ClaudeCodeProvider.provider_id: ClaudeCodeProvider(),
    CodexProvider.provider_id: CodexProvider(),
}


def pending_interaction_for(
    session: Session, accounts: list[Account]
) -> PendingInteraction | None:
    """A session's actionable interaction, for callers that hold a Session but
    not its account/provider. The provider applies the INTERACT gate internally,
    so this is only the account lookup; a session whose account is gone (or whose
    provider has no live interaction) resolves to None."""
    account = next((a for a in accounts if a.key == session.account_key), None)
    if account is None:
        return None
    return PROVIDERS[account.provider_id].pending_interaction(account, session)


__all__ = ["PROVIDERS", "SessionProvider", "pending_interaction_for"]
