"""Outgoing Telegram messages used by the sync pipeline.

Send-only. The interactive bot lives in `telegram_bot.py` and runs in its own
thread. Outgoing messages are directed at a specific chat_id (the account
owner), not broadcast. Per-chat routing keeps one user's mail off another
user's Telegram.
"""

from __future__ import annotations

import logging

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
    """Synchronous Telegram sender. Target chat is supplied per send call."""

    def __init__(self, bot_token: str, timeout: float = 15.0) -> None:
        self._url = TELEGRAM_API_URL.format(token=bot_token)
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def send(self, chat_id: str, message: str) -> bool:
        """Send to a single chat. Returns True if delivered."""
        if not chat_id:
            return False
        try:
            self._send_one(chat_id, message)
        except Exception:
            log.exception("Telegram send failed for chat_id=%s", chat_id)
            return False
        log.info("Telegram alert sent to chat_id=%s (len=%d)", chat_id, len(message))
        return True

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
