from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IMAPAccount(BaseModel):
    name: str
    type: Literal["imap"] = "imap"
    host: str
    port: int = 993
    use_ssl: bool = True
    email: str
    password: SecretStr
    folder: str = "INBOX"
    initial_lookback_days: int = 3

    @field_validator("password", mode="before")
    @classmethod
    def resolve_env_ref(cls, v: object) -> object:
        # YAML parses bare numeric passwords as int/float — coerce so SecretStr accepts them.
        if isinstance(v, (int, float)):
            v = str(v)
        # Allow "$VAR" in YAML to indirect into the environment.
        if isinstance(v, str) and v.startswith("$"):
            env_val = os.environ.get(v[1:])
            if not env_val:
                raise ValueError(f"Env var {v[1:]!r} referenced in accounts.yaml is empty")
            return env_val
        return v


AccountConfig = IMAPAccount  # union grows as providers are added


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openrouter_api_key: SecretStr
    telegram_bot_token: SecretStr
    telegram_chat_id: str

    # Google Calendar — opt-in. If the OAuth client-secrets file isn't present,
    # calendar sync is skipped cleanly and the rest of the pipeline still runs.
    google_client_secrets_path: Path = Path("config/google_client_secret.json")
    google_token_path: Path = Path("data/google_token.json")
    google_calendar_id: str = "primary"

    email_intel_db_path: Path = Path("data/email_intel.db")
    email_intel_log_path: Path = Path("logs/email_intel.log")
    email_intel_accounts_path: Path = Path("config/accounts.yaml")

    poll_interval_minutes: int = Field(default=5, ge=1, le=1440)

    triage_model: str = "google/gemini-2.0-flash-001"
    extraction_model: str = "anthropic/claude-sonnet-4.5"
    fallback_model: str = "openai/gpt-4o-mini"

    @property
    def accounts(self) -> list[AccountConfig]:
        return load_accounts(self.email_intel_accounts_path)


def load_accounts(path: Path) -> list[AccountConfig]:
    if not path.exists():
        raise FileNotFoundError(
            f"Accounts file not found at {path}. "
            "Copy config/accounts.example.yaml to config/accounts.yaml."
        )
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw_accounts = data.get("accounts") or []
    if not raw_accounts:
        raise ValueError(f"No accounts defined in {path}")
    return [IMAPAccount.model_validate(a) for a in raw_accounts]


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None
