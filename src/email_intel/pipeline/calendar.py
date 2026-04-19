"""Convert extracted structured data into Google Calendar events.

Rules (PRD §3.5, §14):
  - Create events for `extraction.calendar_events` (explicit LLM output).
  - Create a meeting event when `extraction.meeting.exists` and we have a date.
  - Create a deadline event when `extraction.deadline` is present and no
    meeting was already covered by the same date.
  - Dedupe per-email via the `calendar_events` table: if this email already
    produced any calendar events, skip further creates for that email.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from email_intel.integrations.google_calendar import GoogleCalendarClient
from email_intel.models import Email, Extraction
from email_intel.storage import repo
from email_intel.storage.schema import EmailRow

log = logging.getLogger(__name__)


def build_events(email: Email, extraction: Extraction) -> list[dict[str, Any]]:
    """Build Google Calendar API event bodies from an Extraction.

    Each body matches https://developers.google.com/calendar/api/v3/reference/events#resource
    """
    events: list[dict[str, Any]] = []
    source_desc = f"From: {email.sender}\nSubject: {email.subject}"

    for ev in extraction.calendar_events:
        start = _parse_iso(ev.start)
        if start is None:
            continue
        end = _parse_iso(ev.end) or (start + timedelta(hours=1))
        events.append(
            _event_body(
                title=ev.title or extraction.summary or email.subject,
                start=start,
                end=end,
                description=_join_desc(ev.description, source_desc),
            )
        )

    if extraction.meeting.exists:
        dt = _combine_date_time(extraction.meeting.date, extraction.meeting.time)
        if dt is not None:
            title = f"Meeting: {email.subject[:120]}" if email.subject else "Meeting"
            events.append(
                _event_body(
                    title=title,
                    start=dt,
                    end=dt + timedelta(hours=1),
                    description=source_desc,
                    location=extraction.meeting.location or "",
                )
            )

    if extraction.deadline:
        dt = _parse_iso(extraction.deadline)
        if dt is not None and not _covered_by_existing(dt, events):
            title = f"Deadline: {email.subject[:120]}" if email.subject else "Deadline"
            events.append(
                _event_body(
                    title=title,
                    start=dt,
                    end=dt + timedelta(minutes=30),
                    description=source_desc,
                )
            )

    return events


def sync_for_email(
    *,
    session: Session,
    client: GoogleCalendarClient | None,
    email_row: EmailRow,
    email: Email,
    extraction: Extraction,
) -> int:
    """Create calendar events for this email if client is available.

    Returns the number of events successfully created. Idempotent per-email:
    if this email already has calendar rows, skip.
    """
    if client is None:
        return 0
    if repo.count_calendar_events_for_email(session, email_row.id) > 0:
        log.debug("Calendar events already created for email_id=%s; skipping", email_row.id)
        return 0

    bodies = build_events(email, extraction)
    if not bodies:
        return 0

    created = 0
    for body in bodies:
        try:
            result = client.insert_event(body)
        except Exception:
            log.exception("Calendar insert failed for email_id=%s; continuing", email_row.id)
            continue
        event_id = result.get("id") if isinstance(result, dict) else None
        repo.record_calendar_event(session, email_row.id, event_id)
        created += 1
        log.info(
            "Calendar event created: email_id=%s google_event_id=%s title=%r",
            email_row.id,
            event_id,
            body.get("summary"),
        )
    return created


def _event_body(
    *,
    title: str,
    start: datetime,
    end: datetime,
    description: str = "",
    location: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": title[:250],
        "description": description,
        "start": {"dateTime": _isoformat(start), "timeZone": "UTC"},
        "end": {"dateTime": _isoformat(end), "timeZone": "UTC"},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 10},
            ],
        },
    }
    if location:
        body["location"] = location
    return body


def _join_desc(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def _isoformat(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_iso(value: str) -> datetime | None:
    """Parse an ISO-ish date/time string. Returns None if unparseable or empty.

    Accepts trailing Z, bare dates (YYYY-MM-DD), and datetime strings.
    """
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(s)
    except ValueError:
        try:
            d = date.fromisoformat(s[:10])
        except ValueError:
            return None
        parsed = datetime.combine(d, time(9, 0))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _combine_date_time(date_str: str, time_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        d = date.fromisoformat(date_str.strip())
    except ValueError:
        return None
    t = time(9, 0)
    if time_str:
        try:
            t = time.fromisoformat(time_str.strip())
        except ValueError:
            pass
    return datetime.combine(d, t, tzinfo=timezone.utc)


def _covered_by_existing(dt: datetime, events: list[dict[str, Any]]) -> bool:
    """True if `dt`'s date already appears in one of the pre-built events."""
    target = dt.date()
    for ev in events:
        start_iso = ev.get("start", {}).get("dateTime", "")
        existing = _parse_iso(start_iso)
        if existing is not None and existing.date() == target:
            return True
    return False
