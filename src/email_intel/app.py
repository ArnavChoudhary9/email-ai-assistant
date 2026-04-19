"""Scheduler tick: fetch -> drain email queue -> drain event queue.

All processing goes through the SQLite-backed queues so a mid-cycle crash
just leaves the current job in a `processing` state; the next tick picks it
back up after its lease expires.
"""

from __future__ import annotations

import logging
import sys

from email_intel.accounts import load_runtime_accounts
from email_intel.config import AccountConfig, Settings, get_settings
from email_intel.integrations.openrouter import OpenRouterClient
from email_intel.integrations.telegram import TelegramNotifier
from email_intel.logging_setup import setup_logging
from email_intel.pipeline import worker
from email_intel.providers.base import BaseEmailProvider
from email_intel.providers.imap import IMAPProvider
from email_intel.runtime import RuntimeContext, build_runtime
from email_intel.storage import repo
from email_intel.storage.db import session_scope

log = logging.getLogger(__name__)


def build_provider(account: AccountConfig) -> BaseEmailProvider:
    if account.type == "imap":
        return IMAPProvider(account)
    raise ValueError(f"Unsupported account type: {account.type}")


def run_cycle(
    settings: Settings | None = None,
    runtime: RuntimeContext | None = None,
) -> dict[str, int]:
    """Fetch new emails, enqueue them, drain email queue, drain event queue."""
    runtime = runtime or build_runtime(settings)
    settings = runtime.settings

    stats = {
        "enqueued": 0,
        "email_processed": 0,
        "email_skipped_ignore": 0,
        "email_failed": 0,
        "email_retried": 0,
        "events_created": 0,
        "events_failed": 0,
        "events_retried": 0,
        "events_skipped": 0,
    }

    accounts = load_runtime_accounts(runtime.session_factory, runtime.cipher)
    if not accounts:
        log.warning("No enabled accounts in DB. /start the bot and /add_account.")

    # 1. Fetch phase: drain IMAP into email_jobs.
    for account, owner_chat_id in accounts:
        try:
            provider = build_provider(account)
            enqueued = worker.fetch_and_enqueue(
                provider, runtime.session_factory, owner_chat_id
            )
            stats["enqueued"] += enqueued
            with session_scope(runtime.session_factory) as s:
                repo.mark_account_success(
                    s, account.name, owner_chat_id=owner_chat_id
                )
        except Exception as e:
            log.exception("Fetch failed for account %s; continuing", account.name)
            with session_scope(runtime.session_factory) as s:
                repo.mark_account_error(
                    s, account.name, str(e), owner_chat_id=owner_chat_id
                )

    # 2. Email worker drain.
    with (
        OpenRouterClient(settings.openrouter_api_key.get_secret_value()) as llm,
        TelegramNotifier(settings.telegram_bot_token.get_secret_value()) as telegram,
    ):
        email_stats = worker.drain_emails(
            session_factory=runtime.session_factory,
            llm=llm,
            telegram=telegram,
            bot_runner=runtime.bot_runner,  # type: ignore[arg-type]
            primary_model=settings.extraction_model,
            fallback_model=settings.fallback_model,
            app_timezone=settings.app_timezone,
        )
    stats["email_processed"] += email_stats.get("processed", 0)
    stats["email_skipped_ignore"] += email_stats.get("skipped_ignore", 0)
    stats["email_failed"] += email_stats.get("failed", 0)
    stats["email_retried"] += email_stats.get("retried", 0)

    # 3. Event worker drain.
    calendar = runtime.build_calendar()
    event_stats = worker.drain_events(
        session_factory=runtime.session_factory,
        calendar=calendar,
        bot_runner=runtime.bot_runner,  # type: ignore[arg-type]
    )
    stats["events_created"] += event_stats.get("created", 0)
    stats["events_failed"] += event_stats.get("failed", 0)
    stats["events_retried"] += event_stats.get("retried", 0)
    stats["events_skipped"] += event_stats.get("skipped", 0)

    log.info("Cycle complete: %s", stats)
    return stats


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
