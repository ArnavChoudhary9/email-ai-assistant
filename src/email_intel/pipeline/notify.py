from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from email_intel.integrations.telegram import TelegramNotifier, format_message, should_alert
from email_intel.models import Email, Extraction
from email_intel.storage import repo
from email_intel.storage.schema import EmailRow

log = logging.getLogger(__name__)


def maybe_send_telegram(
    session: Session,
    notifier: TelegramNotifier,
    email_row: EmailRow,
    email: Email,
    extraction: Extraction,
    *,
    owner_chat_id: str | None,
    pending_events: int = 0,
) -> bool:
    """Send a Telegram alert to the account owner if rules match.

    Idempotent across restarts via the `notifications` table. Skips if the
    account has no owner (shouldn't happen post-migration) or if alerting
    rules don't fire.
    """
    if not should_alert(extraction):
        return False
    if not owner_chat_id:
        log.debug("No owner_chat_id for email_id=%s; skipping alert", email_row.id)
        return False
    if repo.telegram_already_sent(session, email_row.id):
        log.debug("Telegram already sent for email_id=%s; skipping", email_row.id)
        return False

    delivered = notifier.send(
        owner_chat_id, format_message(email, extraction, pending_events=pending_events)
    )
    if delivered:
        repo.mark_telegram_sent(session, email_row.id)
    return delivered
