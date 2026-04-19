"""Shared long-lived runtime state.

A single RuntimeContext is built at scheduler startup and passed into each
poll cycle. The one-shot CLI (`email-intel-once`) builds a transient one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from email_intel.accounts import seed_from_yaml_if_empty
from email_intel.config import Settings, get_settings
from email_intel.storage.migrate import run_migrations
from email_intel.integrations.google_calendar import (
    GoogleCalendarClient,
    build_calendar_client,
)
from email_intel.security import FernetCipher
from email_intel.storage.db import make_engine, make_session_factory

log = logging.getLogger(__name__)


@dataclass
class RuntimeContext:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    cipher: FernetCipher
    build_calendar: Callable[[], GoogleCalendarClient | None]
    # Set by the scheduler when it spawns the bot. One-shot CLI leaves it None.
    bot_runner: object | None = None


def build_runtime(settings: Settings | None = None) -> RuntimeContext:
    settings = settings or get_settings()
    cipher = FernetCipher(settings.app_encryption_key.get_secret_value())
    engine = make_engine(settings.email_intel_db_path)
    session_factory = make_session_factory(engine)

    # Apply in-place migrations (adds owner_chat_id cols, backfills to bot owner).
    run_migrations(engine, session_factory)

    seeded = seed_from_yaml_if_empty(
        session_factory, cipher, settings.email_intel_accounts_path
    )
    if seeded:
        log.info("Seeded %d account(s) from accounts.yaml", seeded)

    calendar_builder = _make_calendar_builder(
        settings.google_client_secrets_path,
        settings.google_token_path,
        settings.google_calendar_id,
    )

    return RuntimeContext(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        cipher=cipher,
        build_calendar=calendar_builder,
    )


def _make_calendar_builder(
    client_secrets: Path,
    token_path: Path,
    calendar_id: str,
) -> Callable[[], GoogleCalendarClient | None]:
    @lru_cache(maxsize=1)
    def _cached() -> GoogleCalendarClient | None:
        return build_calendar_client(client_secrets, token_path, calendar_id)

    return _cached
