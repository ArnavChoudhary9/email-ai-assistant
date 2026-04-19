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
    calendar_added: bool = False,
) -> bool:
    """Send a Telegram alert if rules match and we haven't already sent one.

    `calendar_added` appends a "Calendar added." line when the calendar stage
    actually created events for this email, matching the PRD §3.4 template.

    Returns True if a message was sent. Idempotent across restarts via the
    notifications table.
    """
    if not should_alert(extraction):
        return False
    if repo.telegram_already_sent(session, email_row.id):
        log.debug("Telegram already sent for email_id=%s; skipping", email_row.id)
        return False

    notifier.send(format_message(email, extraction, calendar_added=calendar_added))
    repo.mark_telegram_sent(session, email_row.id)
    return True
