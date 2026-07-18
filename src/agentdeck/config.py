"""TOML config loading + validation.

Real config lives outside the repo (default ~/.config/agentdeck/config.toml,
overridable via the AGENTDECK_CONFIG env var). The config file contains no
secrets — credentials always come from the provider's own store.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, field_validator, model_validator

from .models import Account

DEFAULT_CONFIG_PATH = Path("~/.config/agentdeck/config.toml")
_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class ServerConfig(BaseModel):
    bind: str = "127.0.0.1"
    port: int = 8756


class PollingConfig(BaseModel):
    usage_interval_s: float = 300.0
    scan_interval_s: float = 15.0
    liveness_interval_s: float = 10.0
    host_interval_s: float = 3.0


class UsageConfig(BaseModel):
    shared_cache_dir: str = ""  # default: $XDG_RUNTIME_DIR/agentdeck


class HistoryConfig(BaseModel):  # used from v0.2
    enabled: bool = True
    db_path: str = "~/.local/share/agentdeck/agentdeck.db"
    usage_retention_days: int = 30


class InjectConfig(BaseModel):
    enabled: bool = False
    timeout_s: float = 900.0
    max_message_chars: int = 16_000
    max_image_bytes: int = 10 * 1024 * 1024
    max_image_total_bytes: int = 20 * 1024 * 1024
    max_images: int = 4

    @field_validator("timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("inject.timeout_s must be positive")
        return value

    @field_validator(
        "max_message_chars",
        "max_image_bytes",
        "max_image_total_bytes",
        "max_images",
    )
    @classmethod
    def _positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("inject limits must be positive")
        return value


class AssistantConfig(BaseModel):
    enabled: bool = False
    account_key: str = ""
    model: str = "gpt-5.6-luna"
    refresh_interval_s: float = 60.0
    timeout_s: float = 120.0
    max_sessions: int = 30
    auto_answer: bool = False
    auto_answer_confidence: float = 0.9

    @field_validator("refresh_interval_s", "timeout_s")
    @classmethod
    def _positive_interval(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("assistant intervals must be positive")
        return value

    @field_validator("auto_answer_confidence")
    @classmethod
    def _confidence_range(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("assistant.auto_answer_confidence must be between 0 and 1")
        return value

    @field_validator("max_sessions")
    @classmethod
    def _positive_session_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("assistant.max_sessions must be positive")
        return value


class ClaudeWorkerOverrides(BaseModel):
    """Per-account overrides for [claude_workers]; unset fields inherit the base.

    Lets e.g. an autonomous worker account run with permission_mode =
    "bypassPermissions" while interactive accounts keep the CLI default.
    """

    max_workers: int | None = None
    permission_mode: str | None = None
    model: str | None = None
    usage_ceiling_pct: float | None = None
    stall_after_s: float | None = None

    @field_validator("max_workers")
    @classmethod
    def _positive_max_workers(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("claude_workers accounts max_workers must be positive")
        return value

    @field_validator("usage_ceiling_pct")
    @classmethod
    def _usage_ceiling_range(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 100:
            raise ValueError(
                "claude_workers accounts usage_ceiling_pct must be between 0 and 100"
            )
        return value

    @field_validator("stall_after_s")
    @classmethod
    def _non_negative_stall(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("claude_workers accounts stall_after_s must be non-negative")
        return value


class ClaudeWorkersConfig(BaseModel):
    """Deck-owned Claude worker processes (spawn/steer/revive via the runtime API)."""

    enabled: bool = False
    max_workers: int = 4  # per account; delivery to a live worker is exempt
    permission_mode: str = ""  # e.g. "acceptEdits" / "bypassPermissions"; "" = CLI default
    model: str = ""  # "" = account default model
    usage_ceiling_pct: float = 0.0  # refuse (re)spawns at/above this 5h-or-7d usage %; 0 disables
    stall_after_s: float = (
        0.0  # flag a live worker stalled after this many silent seconds; 0 disables
    )
    state_dir: str = "~/.local/share/agentdeck/claude-workers"
    # Keyed by account label: [claude_workers.accounts.<label>]
    accounts: dict[str, ClaudeWorkerOverrides] = {}

    def for_account(self, label: str) -> ClaudeWorkersConfig:
        """Effective settings for one account (base + that account's overrides)."""
        override = self.accounts.get(label)
        if override is None:
            return self
        merged = {
            field: value
            for field, value in override.model_dump().items()
            if value is not None
        }
        return self.model_copy(update=merged)

    @field_validator("max_workers")
    @classmethod
    def _positive_max_workers(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("claude_workers.max_workers must be positive")
        return value

    @field_validator("usage_ceiling_pct")
    @classmethod
    def _usage_ceiling_range(cls, value: float) -> float:
        if not 0 <= value <= 100:
            raise ValueError("claude_workers.usage_ceiling_pct must be between 0 and 100")
        return value

    @field_validator("stall_after_s")
    @classmethod
    def _non_negative_stall(cls, value: float) -> float:
        if value < 0:
            raise ValueError("claude_workers.stall_after_s must be non-negative")
        return value

    @property
    def state_path(self) -> Path:
        return Path(self.state_dir).expanduser()


class AccountConfig(BaseModel):
    provider: str
    label: str
    config_dir: str

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        from .providers import PROVIDERS  # local import: providers never import config

        if v not in PROVIDERS:
            raise ValueError(f"unknown provider {v!r}; known: {sorted(PROVIDERS)}")
        return v

    @field_validator("label")
    @classmethod
    def _slug_label(cls, v: str) -> str:
        if not _LABEL_RE.match(v):
            raise ValueError(
                f"label {v!r} must be a lowercase slug (it appears in URLs and cache filenames)"
            )
        return v

    @property
    def root(self) -> Path:
        return Path(self.config_dir).expanduser()


class AppConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    polling: PollingConfig = PollingConfig()
    usage: UsageConfig = UsageConfig()
    history: HistoryConfig = HistoryConfig()
    inject: InjectConfig = InjectConfig()
    assistant: AssistantConfig = AssistantConfig()
    claude_workers: ClaudeWorkersConfig = ClaudeWorkersConfig()
    accounts: list[AccountConfig] = []

    @model_validator(mode="after")
    def _unique_labels(self) -> AppConfig:
        seen: set[tuple[str, str]] = set()
        for acc in self.accounts:
            k = (acc.provider, acc.label)
            if k in seen:
                raise ValueError(f"duplicate account label {acc.label!r} for {acc.provider}")
            seen.add(k)
        return self

    def build_accounts(self) -> list[Account]:
        return [
            Account(
                key=f"{acc.provider}:{acc.label}",
                provider_id=acc.provider,
                label=acc.label,
                root=acc.root,
            )
            for acc in self.accounts
        ]


def config_path() -> Path:
    env = os.environ.get("AGENTDECK_CONFIG")
    return Path(env).expanduser() if env else DEFAULT_CONFIG_PATH.expanduser()


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    with path.open("rb") as f:
        data = tomllib.load(f)
    return AppConfig.model_validate(data)
