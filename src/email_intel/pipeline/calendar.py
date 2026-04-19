"""Convert extracted structured data into pending Google Calendar events.

Flow (post-Phase 2):
  - Build event bodies from extraction (meeting, deadline, calendar_events).
  - Insert each as a `pending_events` row (deduped by fingerprint).
  - The Telegram bot prompts the user and, on approval, creates the real
    Google Calendar event. This module no longer calls insert_event directly.

Time handling:
  - Naive datetimes coming out of the LLM are interpreted in the configured
    app timezone (default Asia/Kolkata) — NOT UTC. This fixes the bug where
    "3 PM" in an IST email was being scheduled at 15:00 UTC = 20:30 IST.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from email_intel.models import Email, Extraction
from email_intel.pipeline import pending
from email_intel.storage.schema import EmailRow, PendingEventRow

log = logging.getLogger(__name__)


def build_events(
    email: Email,
    extraction: Extraction,
    *,
    app_timezone: str = "Asia/Kolkata",
) -> list[dict[str, Any]]:
    """Build Google Calendar API event bodies from an Extraction.

    Bodies include title, description, start/end (tz-aware), location, reminders.
    """
    tz = ZoneInfo(app_timezone)
    events: list[dict[str, Any]] = []
    source_desc = f"From: {email.sender}\nSubject: {email.subject}"

    for ev in extraction.calendar_events:
        start = _parse_iso(ev.start, tz)
        if start is None:
            continue
        end = _parse_iso(ev.end, tz) or (start + timedelta(hours=1))
        events.append(
            _event_body(
                title=ev.title or extraction.summary or email.subject,
                start=start,
                end=end,
                description=_join_desc(ev.description, source_desc),
                timezone_name=app_timezone,
            )
        )

    if extraction.meeting.exists:
        dt = _combine_date_time(extraction.meeting.date, extraction.meeting.time, tz)
        if dt is not None:
            title = f"Meeting: {email.subject[:120]}" if email.subject else "Meeting"
            events.append(
                _event_body(
                    title=title,
                    start=dt,
                    end=dt + timedelta(hours=1),
                    description=source_desc,
                    location=extraction.meeting.location or "",
                    timezone_name=app_timezone,
                )
            )

    if extraction.deadline:
        dt = _parse_iso(extraction.deadline, tz)
        if dt is not None and not _covered_by_existing(dt, events):
            title = f"Deadline: {email.subject[:120]}" if email.subject else "Deadline"
            events.append(
                _event_body(
                    title=title,
                    start=dt,
                    end=dt + timedelta(minutes=30),
                    description=source_desc,
                    timezone_name=app_timezone,
                )
            )

    return events


def propose_events_for_email(
    *,
    session: Session,
    email_row: EmailRow,
    email: Email,
    extraction: Extraction,
    app_timezone: str = "Asia/Kolkata",
) -> list[PendingEventRow]:
    """Create pending-event rows for this email; return only the NEW ones.

    Rows that dedup against existing fingerprints are skipped (not returned),
    so the caller only prompts the user about genuinely new events.
    """
    bodies = build_events(email, extraction, app_timezone=app_timezone)
    if not bodies:
        return []

    new_rows: list[PendingEventRow] = []
    for body in bodies:
        start_iso = body.get("start", {}).get("dateTime", "")
        end_iso = body.get("end", {}).get("dateTime", "")
        title = str(body.get("summary", ""))
        row, is_new = pending.propose(
            session,
            email_id=email_row.id,
            title=title,
            start_iso=start_iso,
            end_iso=end_iso,
            timezone_name=app_timezone,
            event_body=body,
        )
        if is_new:
            new_rows.append(row)
    return new_rows


def _event_body(
    *,
    title: str,
    start: datetime,
    end: datetime,
    description: str = "",
    location: str = "",
    timezone_name: str = "Asia/Kolkata",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": title[:250],
        "description": description,
        "start": {"dateTime": _isoformat(start), "timeZone": timezone_name},
        "end": {"dateTime": _isoformat(end), "timeZone": timezone_name},
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
    return dt.isoformat()


def _parse_iso(value: str, default_tz: ZoneInfo) -> datetime | None:
    """Parse an ISO-ish date/time string into a tz-aware datetime.

    Naive datetimes are attached to `default_tz` (not UTC), fixing the wrong-
    time scheduling bug.
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
        parsed = parsed.replace(tzinfo=default_tz)
    return parsed


def _combine_date_time(date_str: str, time_str: str, default_tz: ZoneInfo) -> datetime | None:
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
    return datetime.combine(d, t, tzinfo=default_tz)


def _covered_by_existing(dt: datetime, events: list[dict[str, Any]]) -> bool:
    """True if `dt`'s date already appears in one of the pre-built events."""
    target = dt.date()
    for ev in events:
        start_iso = ev.get("start", {}).get("dateTime", "")
        try:
            existing = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if existing.date() == target:
            return True
    return False
