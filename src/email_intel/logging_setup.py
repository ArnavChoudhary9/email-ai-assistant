from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from pydantic import SecretStr

from email_intel.config import Settings


class RedactFilter(logging.Filter):
    """Replace known secret values in log records with ***REDACTED***."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self._secrets:
            if s in redacted:
                redacted = redacted.replace(s, "***REDACTED***")
        if redacted != msg:
            record.msg = redacted
            record.args = ()
        return True


def _collect_secrets(settings: Settings) -> list[str]:
    values: list[str] = []
    for attr in ("openrouter_api_key", "telegram_bot_token"):
        v = getattr(settings, attr, None)
        if isinstance(v, SecretStr):
            values.append(v.get_secret_value())
    for account in settings.accounts:
        values.append(account.password.get_secret_value())
    return values


def setup_logging(settings: Settings, level: int = logging.INFO) -> None:
    log_path: Path = settings.email_intel_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    # Clear handlers to make setup_logging idempotent (tests call it repeatedly).
    root.handlers.clear()

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    try:
        secrets = _collect_secrets(settings)
    except FileNotFoundError:
        # Accounts file may not exist yet (e.g. during tests). That's fine.
        secrets = []
    redact = RedactFilter(secrets)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.addFilter(redact)
    root.addHandler(stream)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redact)
    root.addHandler(file_handler)

    # Quiet a couple of noisy third parties.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
