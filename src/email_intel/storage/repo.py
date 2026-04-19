from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from email_intel.models import Email, Extraction
from email_intel.storage.schema import EmailRow, NotificationRow, TaskRow


def is_seen(session: Session, provider: str, message_id: str, raw_hash: str) -> bool:
    stmt = select(EmailRow.id).where(
        (EmailRow.provider == provider)
        & ((EmailRow.message_id == message_id) | (EmailRow.raw_hash == raw_hash))
    )
    return session.execute(stmt).first() is not None


def insert_email(session: Session, email: Email) -> EmailRow:
    row = EmailRow(
        provider=email.provider,
        account_name=email.account_name,
        message_id=email.message_id,
        sender=email.sender,
        subject=email.subject,
        received_at=email.received_at,
        raw_hash=email.raw_hash,
        processed=False,
    )
    session.add(row)
    session.flush()
    return row


def save_extraction(session: Session, email_row: EmailRow, extraction: Extraction) -> None:
    email_row.processed = True
    email_row.importance = extraction.importance.value
    email_row.summary = extraction.summary
    email_row.extraction_json = extraction.model_dump_json()
    email_row.last_error = None

    for task in extraction.tasks:
        session.add(TaskRow(email_id=email_row.id, title=task, due_date=extraction.deadline or None))


def record_error(session: Session, email_row: EmailRow, err: str) -> None:
    email_row.processed = False
    email_row.last_error = err[:2000]


def get_or_create_notification(session: Session, email_id: int) -> NotificationRow:
    stmt = select(NotificationRow).where(NotificationRow.email_id == email_id)
    existing = session.execute(stmt).scalar_one_or_none()
    if existing is not None:
        return existing
    row = NotificationRow(email_id=email_id)
    session.add(row)
    session.flush()
    return row


def mark_telegram_sent(session: Session, email_id: int) -> None:
    n = get_or_create_notification(session, email_id)
    n.telegram_sent = True


def telegram_already_sent(session: Session, email_id: int) -> bool:
    stmt = select(NotificationRow.telegram_sent).where(NotificationRow.email_id == email_id)
    result = session.execute(stmt).scalar_one_or_none()
    return bool(result)


def record_calendar_event(
    session: Session, email_id: int, google_event_id: str | None = None
) -> None:
    from email_intel.storage.schema import CalendarEventRow

    session.add(CalendarEventRow(email_id=email_id, google_event_id=google_event_id))


def calendar_event_exists(session: Session, email_id: int, google_event_id: str) -> bool:
    from email_intel.storage.schema import CalendarEventRow

    stmt = select(CalendarEventRow.id).where(
        (CalendarEventRow.email_id == email_id)
        & (CalendarEventRow.google_event_id == google_event_id)
    )
    return session.execute(stmt).first() is not None


def count_calendar_events_for_email(session: Session, email_id: int) -> int:
    from email_intel.storage.schema import CalendarEventRow

    stmt = select(CalendarEventRow.id).where(CalendarEventRow.email_id == email_id)
    return len(session.execute(stmt).all())
