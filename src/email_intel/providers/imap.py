from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

from imap_tools import AND, MailBox, MailBoxUnencrypted
from imap_tools.message import MailMessage

from email_intel.config import IMAPAccount
from email_intel.models import Attachment, Email
from email_intel.providers.base import BaseEmailProvider

log = logging.getLogger(__name__)


def _normalize_headers(msg: MailMessage) -> dict[str, str]:
    """Flatten imap_tools' per-key header tuples into a lowercase-keyed str dict.

    imap_tools types headers loosely; be defensive about shapes and Nones.
    """
    raw = getattr(msg, "headers", None) or {}
    out: dict[str, str] = {}
    for key, vals in raw.items():
        if not vals:
            continue
        first = vals[0] if isinstance(vals, (list, tuple)) else vals
        if first:
            out[str(key).lower()] = str(first)
    return out


class IMAPProvider(BaseEmailProvider):
    provider_type = "imap"

    def __init__(self, account: IMAPAccount) -> None:
        self.account = account
        self.account_name = account.name

    def _connect(self) -> MailBox | MailBoxUnencrypted:
        box: MailBox | MailBoxUnencrypted = (
            MailBox(self.account.host, port=self.account.port)
            if self.account.use_ssl
            else MailBoxUnencrypted(self.account.host, port=self.account.port)
        )
        box.login(
            self.account.email,
            self.account.password.get_secret_value(),
            initial_folder=self.account.folder,
        )
        return box

    def fetch_new(self, since: datetime | None = None) -> Iterator[Email]:
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(days=self.account.initial_lookback_days)
        # IMAP SINCE is date-granular.
        since_date = since.date()

        log.info(
            "IMAP fetch: account=%s folder=%s since=%s",
            self.account.name,
            self.account.folder,
            since_date,
        )

        with self._connect() as box:
            # mark_seen=False — we track state locally, not via \Seen flag.
            for msg in box.fetch(AND(date_gte=since_date), mark_seen=False, bulk=False):
                yield self._to_email(msg)

    def _to_email(self, msg: MailMessage) -> Email:
        headers = _normalize_headers(msg)

        message_id = headers.get("message-id", "").strip().strip("<>")
        if not message_id:
            message_id = (msg.uid or "").strip()

        body_text = (msg.text or "").strip()
        body_html = msg.html or None

        raw_hash = hashlib.sha256(
            f"{message_id}|{msg.from_}|{msg.subject}|{msg.date_str}|{len(body_text)}".encode()
        ).hexdigest()

        attachments = [
            Attachment(
                filename=a.filename or "unnamed",
                mime_type=a.content_type or "application/octet-stream",
                size_bytes=a.size or 0,
            )
            for a in (msg.attachments or [])
        ]

        received_at = msg.date or datetime.now(timezone.utc)
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)

        return Email(
            provider=self.provider_type,
            account_name=self.account.name,
            message_id=message_id or raw_hash,
            sender=msg.from_ or "",
            subject=msg.subject or "",
            received_at=received_at,
            body_text=body_text,
            body_html=body_html,
            folder=self.account.folder,
            labels=list(msg.flags or []),
            headers=headers,
            attachments=attachments,
            raw_hash=raw_hash,
        )
