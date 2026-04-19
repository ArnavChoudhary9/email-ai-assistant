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
    pending_events: int = 0,
) -> bool:
    """Send a Telegram alert if rules match and we haven't already sent one.

    Idempotent across restarts via the `notifications` table. Skips silently
    if no authorized chats are registered yet.
    """
    if not should_alert(extraction):
        return False
    if repo.telegram_already_sent(session, email_row.id):
        log.debug("Telegram already sent for email_id=%s; skipping", email_row.id)
        return False
    if not notifier.has_recipients:
        log.debug("No authorized Telegram chats; skipping alert for email_id=%s", email_row.id)
        return False

    notifier.send(format_message(email, extraction, pending_events=pending_events))
    repo.mark_telegram_sent(session, email_row.id)
    return True
