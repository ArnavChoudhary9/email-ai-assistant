from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class IMAPAccount(BaseModel):
    """Runtime account config passed into the provider.

    Source of truth is the `accounts` DB table; this is the in-memory shape
    after decryption. YAML remains as a seed format for first-run migration.
    """

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
        if isinstance(v, (int, float)):
            v = str(v)
        if isinstance(v, str) and v.startswith("$"):
            env_val = os.environ.get(v[1:])
            if not env_val:
                raise ValueError(f"Env var {v[1:]!r} referenced in accounts.yaml is empty")
            return env_val
        return v


AccountConfig = IMAPAccount


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    openrouter_api_key: SecretStr
    telegram_bot_token: SecretStr
    # Optional now — bot auto-captures chat_id on first /start. If set, it's
    # seeded as the first authorized owner.
    telegram_chat_id: str = ""

    # Fernet key for encrypting account passwords at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    app_encryption_key: SecretStr = SecretStr("")

    # Timezone used when LLM-extracted datetimes are naive. Default IST.
    app_timezone: str = "Asia/Kolkata"

    # Google Calendar — opt-in.
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


def load_accounts_from_yaml(path: Path) -> list[IMAPAccount]:
    """Read legacy accounts.yaml. Used only as a one-time seed source."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    raw_accounts = data.get("accounts") or []
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
