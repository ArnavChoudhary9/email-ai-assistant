from __future__ import annotations

import logging
import sys

from sqlalchemy.orm import Session, sessionmaker

from email_intel.config import AccountConfig, Settings, get_settings
from email_intel.integrations.google_calendar import (
    GoogleCalendarClient,
    build_calendar_client,
)
from email_intel.integrations.openrouter import OpenRouterClient
from email_intel.integrations.telegram import TelegramNotifier
from email_intel.logging_setup import setup_logging
from email_intel.models import Email
from email_intel.pipeline import calendar as calendar_stage
from email_intel.pipeline import fetch as fetch_stage
from email_intel.pipeline import notify as notify_stage
from email_intel.pipeline.classify import classify
from email_intel.pipeline.parse import clean_body
from email_intel.pipeline.summarize import extract
from email_intel.providers.base import BaseEmailProvider
from email_intel.providers.imap import IMAPProvider
from email_intel.storage import repo
from email_intel.storage.db import make_engine, make_session_factory, session_scope
from email_intel.storage.schema import EmailRow

log = logging.getLogger(__name__)


def build_provider(account: AccountConfig) -> BaseEmailProvider:
    if account.type == "imap":
        return IMAPProvider(account)
    raise ValueError(f"Unsupported account type: {account.type}")


def run_cycle(settings: Settings | None = None) -> dict[str, int]:
    """Single poll cycle across all configured accounts.

    Returns a stats dict for observability / tests.
    """
    settings = settings or get_settings()
    engine = make_engine(settings.email_intel_db_path)
    session_factory = make_session_factory(engine)

    stats = {
        "fetched": 0,
        "skipped_ignore": 0,
        "extracted": 0,
        "telegram_sent": 0,
        "calendar_events_created": 0,
        "errors": 0,
    }

    calendar = build_calendar_client(
        settings.google_client_secrets_path,
        settings.google_token_path,
        settings.google_calendar_id,
    )

    with (
        OpenRouterClient(settings.openrouter_api_key.get_secret_value()) as llm,
        TelegramNotifier(
            settings.telegram_bot_token.get_secret_value(),
            settings.telegram_chat_id,
        ) as telegram,
    ):
        for account in settings.accounts:
            try:
                provider = build_provider(account)
                _process_account(
                    provider=provider,
                    session_factory=session_factory,
                    llm=llm,
                    telegram=telegram,
                    calendar=calendar,
                    primary_model=settings.extraction_model,
                    fallback_model=settings.fallback_model,
                    stats=stats,
                )
            except Exception:
                log.exception("Account %s failed this cycle; continuing", account.name)
                stats["errors"] += 1

    log.info("Cycle complete: %s", stats)
    return stats


def _process_account(
    *,
    provider: BaseEmailProvider,
    session_factory: sessionmaker[Session],
    llm: OpenRouterClient,
    telegram: TelegramNotifier,
    calendar: GoogleCalendarClient | None,
    primary_model: str,
    fallback_model: str | None,
    stats: dict[str, int],
) -> None:
    for email in fetch_stage.fetch_unseen(provider, session_factory):
        stats["fetched"] += 1
        try:
            _process_email(
                email=email,
                session_factory=session_factory,
                llm=llm,
                telegram=telegram,
                calendar=calendar,
                primary_model=primary_model,
                fallback_model=fallback_model,
                stats=stats,
            )
        except Exception:
            log.exception("Email %s failed; will retry next cycle", email.message_id)
            stats["errors"] += 1


def _process_email(
    *,
    email: Email,
    session_factory: sessionmaker[Session],
    llm: OpenRouterClient,
    telegram: TelegramNotifier,
    calendar: GoogleCalendarClient | None,
    primary_model: str,
    fallback_model: str | None,
    stats: dict[str, int],
) -> None:
    body = clean_body(email)
    cls = classify(email, body)

    with session_scope(session_factory) as s:
        email_row = repo.insert_email(s, email)
        email_row_id = email_row.id

        if not cls.should_call_llm:
            email_row.processed = True
            email_row.importance = cls.importance_guess.value
            email_row.summary = f"(heuristic-skipped: {cls.reason})"
            stats["skipped_ignore"] += 1
            return

    # LLM call outside the DB transaction to avoid holding it open across network I/O.
    try:
        extraction = extract(
            client=llm,
            email=email,
            body=body,
            primary_model=primary_model,
            fallback_model=fallback_model,
        )
    except Exception as e:
        with session_scope(session_factory) as s:
            row = s.get(EmailRow, email_row_id)
            if row is not None:
                repo.record_error(s, row, str(e))
        raise

    stats["extracted"] += 1

    with session_scope(session_factory) as s:
        row = s.get(EmailRow, email_row_id)
        assert row is not None
        repo.save_extraction(s, row, extraction)

        calendar_added = 0
        try:
            calendar_added = calendar_stage.sync_for_email(
                session=s,
                client=calendar,
                email_row=row,
                email=email,
                extraction=extraction,
            )
        except Exception:
            log.exception("Calendar sync failed for email_id=%s", row.id)
        stats["calendar_events_created"] += calendar_added

        if notify_stage.maybe_send_telegram(
            s, telegram, row, email, extraction, calendar_added=calendar_added > 0
        ):
            stats["telegram_sent"] += 1


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log.info("Running one-shot cycle")
    try:
        run_cycle(settings)
        return 0
    except Exception:
        log.exception("Fatal error in cycle")
        return 1


if __name__ == "__main__":
    sys.exit(main())
