from __future__ import annotations

from email_intel.models import (
    CalendarEvent,
    Extraction,
    Importance,
    Meeting,
)
from email_intel.pipeline.calendar import build_events


def _email(email_factory, **kw):
    return email_factory(**kw)


def test_no_events_when_extraction_has_nothing(email_factory):
    email = _email(email_factory, subject="Hi")
    ex = Extraction(importance=Importance.NORMAL)
    assert build_events(email, ex) == []


def test_meeting_produces_event(email_factory):
    email = _email(email_factory, subject="1:1 with professor", sender="prof@x.edu")
    ex = Extraction(
        importance=Importance.IMPORTANT,
        meeting=Meeting(exists=True, date="2026-05-01", time="10:30", location="Room A"),
    )
    events = build_events(email, ex)
    assert len(events) == 1
    ev = events[0]
    assert ev["summary"].startswith("Meeting:")
    assert ev["location"] == "Room A"
    assert ev["start"]["dateTime"].startswith("2026-05-01T10:30")
    assert ev["end"]["dateTime"].startswith("2026-05-01T11:30")


def test_meeting_without_date_is_skipped(email_factory):
    email = _email(email_factory)
    ex = Extraction(
        importance=Importance.IMPORTANT,
        meeting=Meeting(exists=True, date="", time="10:00"),
    )
    assert build_events(email, ex) == []


def test_deadline_produces_event(email_factory):
    email = _email(email_factory, subject="Assignment due")
    ex = Extraction(importance=Importance.IMPORTANT, deadline="2026-04-25")
    events = build_events(email, ex)
    assert len(events) == 1
    assert events[0]["summary"].startswith("Deadline:")
    assert events[0]["start"]["dateTime"].startswith("2026-04-25T")


def test_deadline_matching_meeting_date_is_skipped(email_factory):
    """If the meeting already covers the deadline date, don't duplicate it."""
    email = _email(email_factory, subject="Final review")
    ex = Extraction(
        importance=Importance.IMPORTANT,
        meeting=Meeting(exists=True, date="2026-05-10", time="14:00"),
        deadline="2026-05-10",
    )
    events = build_events(email, ex)
    assert len(events) == 1
    assert events[0]["summary"].startswith("Meeting:")


def test_explicit_calendar_events_use_llm_title(email_factory):
    email = _email(email_factory, subject="Conference travel")
    ex = Extraction(
        importance=Importance.IMPORTANT,
        calendar_events=[
            CalendarEvent(
                title="Flight to Delhi",
                start="2026-06-01T06:00:00",
                end="2026-06-01T09:00:00",
                description="Indigo flight 6E-123",
            )
        ],
    )
    events = build_events(email, ex)
    assert len(events) == 1
    assert events[0]["summary"] == "Flight to Delhi"
    assert "Indigo flight" in events[0]["description"]


def test_unparseable_dates_are_dropped(email_factory):
    email = _email(email_factory)
    ex = Extraction(
        importance=Importance.IMPORTANT,
        deadline="sometime next week",
        meeting=Meeting(exists=True, date="tuesday", time=""),
    )
    assert build_events(email, ex) == []
