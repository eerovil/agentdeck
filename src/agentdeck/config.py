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
