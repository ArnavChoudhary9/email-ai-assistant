from __future__ import annotations

import logging
import sys

from sqlalchemy.orm import Session, sessionmaker

from email_intel.accounts import load_runtime_accounts
from email_intel.config import AccountConfig, Settings, get_settings
from email_intel.integrations.google_calendar import GoogleCalendarClient
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
from email_intel.runtime import RuntimeContext, build_runtime
from email_intel.storage import repo
from email_intel.storage.db import session_scope
from email_intel.storage.schema import EmailRow, PendingEventRow

log = logging.getLogger(__name__)


def build_provider(account: AccountConfig) -> BaseEmailProvider:
    if account.type == "imap":
        return IMAPProvider(account)
    raise ValueError(f"Unsupported account type: {account.type}")


def run_cycle(
    settings: Settings | None = None,
    runtime: RuntimeContext | None = None,
) -> dict[str, int]:
    """Single poll cycle across all configured accounts."""
    runtime = runtime or build_runtime(settings)
    settings = runtime.settings

    stats = {
        "fetched": 0,
        "skipped_ignore": 0,
        "extracted": 0,
        "telegram_sent": 0,
        "pending_events_created": 0,
        "errors": 0,
    }

    accounts = load_runtime_accounts(runtime.session_factory, runtime.cipher)
    if not accounts:
        log.warning("No enabled accounts in DB. Add one via the bot: /add_account")
        return stats

    # Fan out outgoing notifications to all authorized chats.
    with session_scope(runtime.session_factory) as s:
        chat_ids = repo.list_authorized_chat_ids(s)

    calendar = runtime.build_calendar()

    with (
        OpenRouterClient(settings.openrouter_api_key.get_secret_value()) as llm,
        TelegramNotifier(
            settings.telegram_bot_token.get_secret_value(),
            chat_ids,
        ) as telegram,
    ):
        for account in accounts:
            try:
                provider = build_provider(account)
                _process_account(
                    provider=provider,
                    account_name=account.name,
                    session_factory=runtime.session_factory,
                    llm=llm,
                    telegram=telegram,
                    calendar=calendar,
                    runtime=runtime,
                    primary_model=settings.extraction_model,
                    fallback_model=settings.fallback_model,
                    app_timezone=settings.app_timezone,
                    chat_ids=chat_ids,
                    stats=stats,
                )
                with session_scope(runtime.session_factory) as s:
                    repo.mark_account_success(s, account.name)
            except Exception as e:
                log.exception("Account %s failed this cycle; continuing", account.name)
                stats["errors"] += 1
                with session_scope(runtime.session_factory) as s:
                    repo.mark_account_error(s, account.name, str(e))

    log.info("Cycle complete: %s", stats)
    return stats


def _process_account(
    *,
    provider: BaseEmailProvider,
    account_name: str,  # noqa: ARG001
    session_factory: sessionmaker[Session],
    llm: OpenRouterClient,
    telegram: TelegramNotifier,
    calendar: GoogleCalendarClient | None,
    runtime: RuntimeContext,
    primary_model: str,
    fallback_model: str | None,
    app_timezone: str,
    chat_ids: list[str],
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
                runtime=runtime,
                primary_model=primary_model,
                fallback_model=fallback_model,
                app_timezone=app_timezone,
                chat_ids=chat_ids,
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
    calendar: GoogleCalendarClient | None,  # noqa: ARG001  # kept for signature stability
    runtime: RuntimeContext,
    primary_model: str,
    fallback_model: str | None,
    app_timezone: str,
    chat_ids: list[str],
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

    try:
        extraction = extract(
            client=llm,
            email=email,
            body=body,
            primary_model=primary_model,
            fallback_model=fallback_model,
            app_timezone=app_timezone,
        )
    except Exception as e:
        with session_scope(session_factory) as s:
            row = s.get(EmailRow, email_row_id)
            if row is not None:
                repo.record_error(s, row, str(e))
        raise

    stats["extracted"] += 1

    pending_rows_snapshot: list[dict[str, str | int]] = []

    with session_scope(session_factory) as s:
        row = s.get(EmailRow, email_row_id)
        assert row is not None
        repo.save_extraction(s, row, extraction)

        try:
            new_pending = calendar_stage.propose_events_for_email(
                session=s,
                email_row=row,
                email=email,
                extraction=extraction,
                app_timezone=app_timezone,
            )
        except Exception:
            log.exception("propose_events failed for email_id=%s", row.id)
            new_pending = []

        stats["pending_events_created"] += len(new_pending)

        # Snapshot fields we need to send prompts outside the session scope.
        for p in new_pending:
            pending_rows_snapshot.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "start_iso": p.start_iso,
                    "end_iso": p.end_iso,
                    "timezone_name": p.timezone_name,
                }
            )

        if notify_stage.maybe_send_telegram(
            s, telegram, row, email, extraction, pending_events=len(new_pending)
        ):
            stats["telegram_sent"] += 1

    # Send per-event approval prompts via the bot (if running).
    bot_runner = runtime.bot_runner
    if bot_runner is not None and pending_rows_snapshot and chat_ids:
        for snap in pending_rows_snapshot:
            for chat_id in chat_ids:
                message_id = bot_runner.send_pending_prompt(  # type: ignore[attr-defined]
                    chat_id,
                    _FakePendingForPrompt(**snap),  # type: ignore[arg-type]
                )
                if message_id:
                    pid = int(snap["id"])
                    with session_scope(session_factory) as s:
                        repo.update_pending_prompt(s, pid, chat_id, message_id)


class _FakePendingForPrompt:
    """Minimal shape to satisfy BotRunner.send_pending_prompt's type.

    We pass this instead of the live PendingEventRow to avoid re-querying.
    """

    def __init__(
        self,
        id: int,  # noqa: A002  # matches attribute name used by handler
        title: str,
        start_iso: str,
        end_iso: str,
        timezone_name: str,
    ) -> None:
        self.id = id
        self.title = title
        self.start_iso = start_iso
        self.end_iso = end_iso
        self.timezone_name = timezone_name


# Type alias so BotRunner typing stays happy.
PendingEventLike = PendingEventRow | _FakePendingForPrompt


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
