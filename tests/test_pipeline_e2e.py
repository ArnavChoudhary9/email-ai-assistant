from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import respx

from email_intel.app import _process_email
from email_intel.integrations.openrouter import OPENROUTER_URL, OpenRouterClient
from email_intel.integrations.telegram import TelegramNotifier
from email_intel.runtime import RuntimeContext
from email_intel.security import FernetCipher, generate_key
from email_intel.storage import repo
from email_intel.storage.db import session_scope


def _llm_ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _new_stats() -> dict[str, int]:
    return {
        "fetched": 0,
        "skipped_ignore": 0,
        "extracted": 0,
        "telegram_sent": 0,
        "pending_events_created": 0,
        "errors": 0,
    }


def _runtime(session_factory) -> RuntimeContext:
    return RuntimeContext(
        settings=MagicMock(),
        engine=MagicMock(),
        session_factory=session_factory,
        cipher=FernetCipher(generate_key()),
        build_calendar=lambda: None,
        bot_runner=None,
    )


@respx.mock
def test_important_email_triggers_telegram_without_calendar(session_factory, email_factory):
    email = email_factory(
        subject="Urgent reply please",
        sender="manager@iitd.ac.in",
        body="Please reply ASAP — no meeting required.",
    )
    respx.post(OPENROUTER_URL).mock(
        return_value=_llm_ok(
            '{"summary":"Reply needed urgently","importance":"important",'
            '"action_required":true,"deadline":"",'
            '"meeting":{"exists":false,"date":"","time":"","location":""},'
            '"tasks":["Reply to manager"],"calendar_events":[],'
            '"reply_needed":true,"reply_priority":"urgent"}'
        )
    )

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.has_recipients = True
    stats = _new_stats()

    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=None,
            runtime=_runtime(session_factory),
            primary_model="primary",
            fallback_model="fallback",
            app_timezone="Asia/Kolkata",
            chat_ids=["123"],
            stats=stats,
        )

    assert stats["extracted"] == 1
    assert stats["telegram_sent"] == 1
    assert stats["pending_events_created"] == 0
    telegram.send.assert_called_once()
    sent_msg = telegram.send.call_args.args[0]
    assert "Urgent reply please" in sent_msg
    assert "[IMPORTANT]" in sent_msg
    assert "Reply to manager" in sent_msg


@respx.mock
def test_meeting_email_creates_pending_event(session_factory, email_factory):
    email = email_factory(
        subject="Interview invitation",
        sender="placement@iitd.ac.in",
        body="Your interview is scheduled Tuesday 3 PM.",
    )
    respx.post(OPENROUTER_URL).mock(
        return_value=_llm_ok(
            '{"summary":"Interview Tuesday 3 PM","importance":"important",'
            '"action_required":true,"deadline":"",'
            '"meeting":{"exists":true,"date":"2026-04-21","time":"15:00","location":"Room 204"},'
            '"tasks":["Prepare documents"],"calendar_events":[],'
            '"reply_needed":false,"reply_priority":""}'
        )
    )

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.has_recipients = True
    stats = _new_stats()

    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=None,
            runtime=_runtime(session_factory),
            primary_model="primary",
            fallback_model=None,
            app_timezone="Asia/Kolkata",
            chat_ids=[],  # no bot users → no prompt sent, but pending still created
            stats=stats,
        )

    assert stats["pending_events_created"] == 1

    with session_scope(session_factory) as s:
        pending_rows = repo.list_pending(s)
    assert len(pending_rows) == 1
    row = pending_rows[0]
    assert row.title.startswith("Meeting:")
    # Time stays in IST — not blindly stamped UTC.
    assert row.start_iso.startswith("2026-04-21T15:00")
    assert "+05:30" in row.start_iso
    assert row.timezone_name == "Asia/Kolkata"

    # Telegram fired and mentioned pending count.
    assert stats["telegram_sent"] == 1
    sent_msg = telegram.send.call_args.args[0]
    assert "1 calendar event" in sent_msg
    assert "Prepare documents" in sent_msg


@respx.mock
def test_reminder_email_does_not_duplicate_pending(session_factory, email_factory):
    """Two reminder emails about the same interview → one pending event."""

    def _llm_response() -> httpx.Response:
        return _llm_ok(
            '{"summary":"Interview at 3 PM","importance":"important",'
            '"action_required":true,"deadline":"",'
            '"meeting":{"exists":true,"date":"2026-04-21","time":"15:00","location":"Room 204"},'
            '"tasks":[],"calendar_events":[],'
            '"reply_needed":false,"reply_priority":""}'
        )

    respx.post(OPENROUTER_URL).mock(side_effect=[_llm_response(), _llm_response()])

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.has_recipients = True
    stats = _new_stats()

    def _run(email):
        with OpenRouterClient("sk-test") as llm:
            _process_email(
                email=email,
                session_factory=session_factory,
                llm=llm,
                telegram=telegram,
                calendar=None,
                runtime=_runtime(session_factory),
                primary_model="primary",
                fallback_model=None,
                app_timezone="Asia/Kolkata",
                chat_ids=[],
                stats=stats,
            )

    _run(email_factory(subject="Interview invitation", message_id="msg-a"))
    _run(email_factory(subject="Reminder: Interview invitation", message_id="msg-b"))

    # Both emails processed, but only ONE pending row survived dedup.
    assert stats["extracted"] == 2
    assert stats["pending_events_created"] == 1

    with session_scope(session_factory) as s:
        pending_rows = repo.list_pending(s)
    assert len(pending_rows) == 1


@respx.mock
def test_promo_email_skipped_without_llm_or_calendar(session_factory, email_factory):
    email = email_factory(
        subject="50% off this weekend",
        sender="newsletter@brand.com",
        body="Limited time offer. Click to unsubscribe.",
        headers={"list-unsubscribe": "<mailto:u@brand.com>"},
    )
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(500))

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.has_recipients = True
    stats = _new_stats()

    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=None,
            runtime=_runtime(session_factory),
            primary_model="primary",
            fallback_model=None,
            app_timezone="Asia/Kolkata",
            chat_ids=["123"],
            stats=stats,
        )

    assert stats["skipped_ignore"] == 1
    assert stats["extracted"] == 0
    assert stats["telegram_sent"] == 0
    assert stats["pending_events_created"] == 0
    telegram.send.assert_not_called()
    assert route.called is False
