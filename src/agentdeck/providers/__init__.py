"""Provider registry. The web layer resolves providers only through this."""

from .base import SessionProvider
from .claude_code.provider import ClaudeCodeProvider
from .codex.provider import CodexProvider

PROVIDERS: dict[str, SessionProvider] = {
    ClaudeCodeProvider.provider_id: ClaudeCodeProvider(),
    CodexProvider.provider_id: CodexProvider(),
}

__all__ = ["PROVIDERS", "SessionProvider"]
