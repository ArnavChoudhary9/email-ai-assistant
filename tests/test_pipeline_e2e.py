from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import respx

from email_intel.app import _process_email
from email_intel.integrations.google_calendar import GoogleCalendarClient
from email_intel.integrations.openrouter import OPENROUTER_URL, OpenRouterClient
from email_intel.integrations.telegram import TelegramNotifier


def _llm_ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _new_stats() -> dict[str, int]:
    return {
        "fetched": 0,
        "skipped_ignore": 0,
        "extracted": 0,
        "telegram_sent": 0,
        "calendar_events_created": 0,
        "errors": 0,
    }


@respx.mock
def test_important_email_triggers_telegram_without_calendar(session_factory, email_factory):
    email = email_factory(
        subject="Interview invitation",
        sender="placement@iitd.ac.in",
        body="Your interview is scheduled Tuesday 3 PM. Please confirm.",
    )
    respx.post(OPENROUTER_URL).mock(
        return_value=_llm_ok(
            '{"summary":"Interview Tuesday 3 PM","importance":"important",'
            '"action_required":true,"deadline":"2026-04-21",'
            '"meeting":{"exists":true,"date":"2026-04-21","time":"15:00","location":""},'
            '"tasks":["Prepare documents"],"calendar_events":[],'
            '"reply_needed":true,"reply_priority":"urgent"}'
        )
    )

    telegram = MagicMock(spec=TelegramNotifier)
    stats = _new_stats()

    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=None,
            primary_model="primary",
            fallback_model="fallback",
            stats=stats,
        )

    assert stats["extracted"] == 1
    assert stats["telegram_sent"] == 1
    assert stats["calendar_events_created"] == 0
    telegram.send.assert_called_once()
    sent_msg = telegram.send.call_args.args[0]
    assert "Interview invitation" in sent_msg
    assert "[IMPORTANT]" in sent_msg
    assert "Prepare documents" in sent_msg
    # No calendar client → no "Calendar added." line.
    assert "Calendar added." not in sent_msg


@respx.mock
def test_meeting_email_creates_calendar_and_notes_it_in_telegram(
    session_factory, email_factory
):
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
    calendar = MagicMock(spec=GoogleCalendarClient)
    calendar.insert_event.return_value = {"id": "evt_abc123"}
    stats = _new_stats()

    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=calendar,
            primary_model="primary",
            fallback_model=None,
            stats=stats,
        )

    assert stats["calendar_events_created"] == 1
    calendar.insert_event.assert_called_once()
    body = calendar.insert_event.call_args.args[0]
    assert body["summary"].startswith("Meeting:")
    assert body["location"] == "Room 204"
    assert body["start"]["dateTime"].startswith("2026-04-21T15:00")

    # Telegram fired and mentioned the calendar.
    assert stats["telegram_sent"] == 1
    sent_msg = telegram.send.call_args.args[0]
    assert "Calendar added." in sent_msg
    assert "Prepare documents" in sent_msg


@respx.mock
def test_calendar_sync_is_idempotent(session_factory, email_factory):
    """Running the same email twice must not create duplicate calendar rows."""
    email = email_factory(
        subject="Exam schedule",
        sender="prof@iitd.ac.in",
        body="Your exam is on Friday.",
    )
    respx.post(OPENROUTER_URL).mock(
        return_value=_llm_ok(
            '{"summary":"Exam Friday","importance":"important","action_required":true,'
            '"deadline":"2026-04-24",'
            '"meeting":{"exists":false,"date":"","time":"","location":""},'
            '"tasks":[],"calendar_events":[],"reply_needed":false,"reply_priority":""}'
        )
    )

    telegram = MagicMock(spec=TelegramNotifier)
    calendar = MagicMock(spec=GoogleCalendarClient)
    calendar.insert_event.return_value = {"id": "evt_dead1"}

    # First pass.
    stats = _new_stats()
    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=calendar,
            primary_model="primary",
            fallback_model=None,
            stats=stats,
        )

    assert stats["calendar_events_created"] == 1
    assert calendar.insert_event.call_count == 1

    # Second pass over the same email — simulates a repeat within one cycle.
    # The DB insert will throw (uniqueness), so we only test the calendar dedup
    # helper directly.
    from email_intel.pipeline.calendar import sync_for_email
    from email_intel.storage.db import session_scope
    from email_intel.storage.schema import EmailRow

    with session_scope(session_factory) as s:
        row = s.query(EmailRow).filter_by(message_id=email.message_id).one()
        from email_intel.models import Extraction, Importance

        extraction = Extraction(
            summary="Exam Friday",
            importance=Importance.IMPORTANT,
            deadline="2026-04-24",
        )
        created = sync_for_email(
            session=s,
            client=calendar,
            email_row=row,
            email=email,
            extraction=extraction,
        )

    assert created == 0
    # Still only one call from the first pass.
    assert calendar.insert_event.call_count == 1


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
    calendar = MagicMock(spec=GoogleCalendarClient)
    stats = _new_stats()

    with OpenRouterClient("sk-test") as llm:
        _process_email(
            email=email,
            session_factory=session_factory,
            llm=llm,
            telegram=telegram,
            calendar=calendar,
            primary_model="primary",
            fallback_model=None,
            stats=stats,
        )

    assert stats["skipped_ignore"] == 1
    assert stats["extracted"] == 0
    assert stats["telegram_sent"] == 0
    assert stats["calendar_events_created"] == 0
    telegram.send.assert_not_called()
    calendar.insert_event.assert_not_called()
    assert route.called is False
