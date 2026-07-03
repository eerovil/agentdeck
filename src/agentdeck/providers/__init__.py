"""Provider registry. The web layer resolves providers only through this."""

from .base import SessionProvider
from .claude_code.provider import ClaudeCodeProvider

PROVIDERS: dict[str, SessionProvider] = {
    ClaudeCodeProvider.provider_id: ClaudeCodeProvider(),
}

__all__ = ["PROVIDERS", "SessionProvider"]
