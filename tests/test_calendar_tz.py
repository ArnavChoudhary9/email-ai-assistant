from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from email_intel.models import CalendarEvent, Extraction, Meeting
from email_intel.pipeline.calendar import _combine_date_time, _parse_iso, build_events


def test_parse_iso_naive_uses_app_tz():
    tz = ZoneInfo("Asia/Kolkata")
    dt = _parse_iso("2026-04-20T15:00:00", tz)
    assert dt is not None
    assert dt.tzinfo is not None
    # 15:00 IST = 09:30 UTC — NOT 15:00 UTC (the old bug).
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)
    assert dt.astimezone(UTC).hour == 9
    assert dt.astimezone(UTC).minute == 30


def test_parse_iso_respects_explicit_offset():
    tz = ZoneInfo("Asia/Kolkata")
    dt = _parse_iso("2026-04-20T15:00:00+00:00", tz)
    assert dt is not None
    assert dt.utcoffset() == timedelta(0)


def test_combine_date_time_uses_app_tz():
    tz = ZoneInfo("Asia/Kolkata")
    dt = _combine_date_time("2026-04-20", "15:00", tz)
    assert dt is not None
    assert dt.utcoffset() == timedelta(hours=5, minutes=30)


def test_build_events_emits_app_timezone_label(email_factory):
    email = email_factory(subject="Interview")
    ex = Extraction(
        meeting=Meeting(exists=True, date="2026-04-20", time="15:00", location="IITD")
    )
    events = build_events(email, ex, app_timezone="Asia/Kolkata")
    assert len(events) == 1
    ev = events[0]
    assert ev["start"]["timeZone"] == "Asia/Kolkata"
    # ISO string is written in IST, so hour stays 15 (+05:30) — not 15 UTC.
    start = datetime.fromisoformat(ev["start"]["dateTime"])
    assert start.hour == 15
    assert start.utcoffset() == timedelta(hours=5, minutes=30)
    assert ev["location"] == "IITD"


def test_build_events_from_calendar_event_field(email_factory):
    email = email_factory(subject="Talk")
    ex = Extraction(
        calendar_events=[
            CalendarEvent(title="Guest lecture", start="2026-04-20T11:00:00", end="")
        ]
    )
    events = build_events(email, ex, app_timezone="Asia/Kolkata")
    assert len(events) == 1
    start = datetime.fromisoformat(events[0]["start"]["dateTime"])
    end = datetime.fromisoformat(events[0]["end"]["dateTime"])
    assert start.utcoffset() == timedelta(hours=5, minutes=30)
    assert end - start == timedelta(hours=1)  # default duration when end empty
