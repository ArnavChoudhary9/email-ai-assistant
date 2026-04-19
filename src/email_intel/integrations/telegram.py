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
    calendar_added: bool = False,
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

    if calendar_added:
        parts += ["", "Calendar added."]

    return "\n".join(parts)


class TelegramNotifier:
    """Synchronous Telegram sender.

    Uses a plain sync httpx client against the Bot API directly. This avoids
    the event-loop-closed failures you get when re-entering asyncio.run() while
    reusing a Bot whose AsyncClient was bound to a previously-closed loop.
    """

    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0) -> None:
        self._url = TELEGRAM_API_URL.format(token=bot_token)
        self._chat_id = chat_id
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPError),
    )
    def send(self, message: str) -> None:
        resp = self._client.post(
            self._url,
            json={
                "chat_id": self._chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
        )
        if resp.status_code >= 400:
            # Don't retry on 4xx — it means bad token/chat_id/payload, not a transient error.
            raise RuntimeError(f"Telegram API {resp.status_code}: {resp.text[:300]}")
        log.info("Telegram alert sent (len=%d)", len(message))
