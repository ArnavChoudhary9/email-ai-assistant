"""Outgoing Telegram messages used by the sync pipeline.

This is send-only. The interactive bot lives in `telegram_bot.py` and runs in
its own thread with its own asyncio loop. Outgoing messages here fan out to
every authorized chat_id from the `bot_users` table.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from email_intel.models import Email, Extraction, Importance, ReplyPriority

log = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def should_alert(extraction: Extraction) -> bool:
    """PRD §12 Telegram rules."""
    if extraction.importance in (Importance.CRITICAL, Importance.IMPORTANT):
        return True
    if extraction.deadline:
        return True
    if extraction.meeting.exists:
        return True
    if extraction.reply_needed and extraction.reply_priority == ReplyPriority.URGENT:
        return True
    return False


def format_message(
    email: Email,
    extraction: Extraction,
    *,
    pending_events: int = 0,
) -> str:
    tag = "[CRITICAL]" if extraction.importance == Importance.CRITICAL else "[IMPORTANT]"

    parts = [
        tag,
        "",
        f"From: {email.sender}",
        f"Subject: {email.subject}",
        "",
        "Summary:",
        extraction.summary or "(no summary)",
    ]

    if extraction.deadline:
        parts += ["", f"Deadline: {extraction.deadline}"]

    if extraction.meeting.exists:
        m = extraction.meeting
        meeting_line = " ".join(filter(None, [m.date, m.time, m.location]))
        parts += ["", f"Meeting: {meeting_line}"]

    if extraction.tasks:
        parts += ["", "Tasks:"]
        parts += [f"- {t}" for t in extraction.tasks]

    if extraction.reply_needed:
        priority = extraction.reply_priority.value or "normal"
        parts += ["", f"Reply needed ({priority})."]

    if pending_events:
        parts += [
            "",
            f"{pending_events} calendar event(s) proposed — approve via the prompt below.",
        ]

    return "\n".join(parts)


class TelegramNotifier:
    """Synchronous Telegram sender. Fans messages out to multiple chats.

    Uses a plain sync httpx client against the Bot API directly.
    """

    def __init__(
        self,
        bot_token: str,
        chat_ids: Iterable[str],
        timeout: float = 15.0,
    ) -> None:
        self._url = TELEGRAM_API_URL.format(token=bot_token)
        self._chat_ids = [c for c in chat_ids if c]
        self._client = httpx.Client(timeout=timeout)

    @property
    def has_recipients(self) -> bool:
        return bool(self._chat_ids)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send(self, message: str) -> int:
        """Send `message` to every recipient. Returns count delivered."""
        delivered = 0
        for chat_id in self._chat_ids:
            try:
                self._send_one(chat_id, message)
                delivered += 1
            except Exception:
                log.exception("Telegram send failed for chat_id=%s", chat_id)
        if delivered:
            log.info("Telegram alert sent to %d chat(s) (len=%d)", delivered, len(message))
        return delivered

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPError),
    )
    def _send_one(self, chat_id: str, message: str) -> None:
        resp = self._client.post(
            self._url,
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code >= 400:
            # Don't retry on 4xx — bad token/chat_id/payload, not transient.
            raise RuntimeError(f"Telegram API {resp.status_code}: {resp.text[:300]}")
