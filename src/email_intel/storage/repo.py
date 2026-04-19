from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from email_intel.models import Email, Extraction
from email_intel.storage.schema import (
    AccountRow,
    BotUserRow,
    CalendarEventRow,
    EmailJobRow,
    EmailRow,
    EventJobRow,
    NotificationRow,
    PendingEventRow,
    TaskRow,
)


def is_seen(session: Session, provider: str, message_id: str, raw_hash: str) -> bool:
    stmt = select(EmailRow.id).where(
        (EmailRow.provider == provider)
        & ((EmailRow.message_id == message_id) | (EmailRow.raw_hash == raw_hash))
    )
    return session.execute(stmt).first() is not None


def insert_email(
    session: Session, email: Email, *, owner_chat_id: str | None = None
) -> EmailRow:
    row = EmailRow(
        provider=email.provider,
        account_name=email.account_name,
        owner_chat_id=owner_chat_id,
        message_id=email.message_id,
        sender=email.sender,
        subject=email.subject,
        received_at=email.received_at,
        raw_hash=email.raw_hash,
        body_text=email.body_text,
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


# --- Notifications --------------------------------------------------------


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


# --- Calendar events ------------------------------------------------------


def record_calendar_event(
    session: Session,
    email_id: int,
    google_event_id: str | None = None,
    fingerprint: str | None = None,
) -> None:
    session.add(
        CalendarEventRow(
            email_id=email_id,
            google_event_id=google_event_id,
            fingerprint=fingerprint,
        )
    )


def calendar_event_exists(session: Session, email_id: int, google_event_id: str) -> bool:
    stmt = select(CalendarEventRow.id).where(
        (CalendarEventRow.email_id == email_id)
        & (CalendarEventRow.google_event_id == google_event_id)
    )
    return session.execute(stmt).first() is not None


def count_calendar_events_for_email(session: Session, email_id: int) -> int:
    stmt = select(CalendarEventRow.id).where(CalendarEventRow.email_id == email_id)
    return len(session.execute(stmt).all())


# --- Accounts -------------------------------------------------------------


def list_accounts(
    session: Session,
    enabled_only: bool = True,
    *,
    owner_chat_id: str | None = None,
) -> list[AccountRow]:
    stmt = select(AccountRow)
    if enabled_only:
        stmt = stmt.where(AccountRow.enabled == True)  # noqa: E712
    if owner_chat_id is not None:
        stmt = stmt.where(AccountRow.owner_chat_id == owner_chat_id)
    return list(session.execute(stmt).scalars().all())


def get_account_by_name(
    session: Session, name: str, *, owner_chat_id: str | None = None
) -> AccountRow | None:
    """Look up an account by name. When owner_chat_id is given, the row must
    also belong to that chat — mismatches return None (same UX as not found)."""
    stmt = select(AccountRow).where(AccountRow.name == name)
    if owner_chat_id is not None:
        stmt = stmt.where(AccountRow.owner_chat_id == owner_chat_id)
    return session.execute(stmt).scalar_one_or_none()


def insert_account(
    session: Session,
    *,
    name: str,
    owner_chat_id: str,
    host: str,
    port: int,
    use_ssl: bool,
    email: str,
    password_encrypted: str,
    folder: str = "INBOX",
    initial_lookback_days: int = 3,
) -> AccountRow:
    row = AccountRow(
        name=name,
        owner_chat_id=owner_chat_id,
        type="imap",
        host=host,
        port=port,
        use_ssl=use_ssl,
        email=email,
        password_encrypted=password_encrypted,
        folder=folder,
        initial_lookback_days=initial_lookback_days,
        enabled=True,
    )
    session.add(row)
    session.flush()
    return row


def delete_account(
    session: Session, name: str, *, owner_chat_id: str | None = None
) -> bool:
    row = get_account_by_name(session, name, owner_chat_id=owner_chat_id)
    if row is None:
        return False
    session.delete(row)
    return True


def delete_accounts_for_chat(session: Session, owner_chat_id: str) -> int:
    """Hard-delete every account owned by this chat. Returns rows removed."""
    rows = list_accounts(session, enabled_only=False, owner_chat_id=owner_chat_id)
    for r in rows:
        session.delete(r)
    return len(rows)


def mark_account_success(
    session: Session, name: str, *, owner_chat_id: str | None = None
) -> None:
    row = get_account_by_name(session, name, owner_chat_id=owner_chat_id)
    if row is not None:
        row.last_success_at = datetime.now(UTC)
        row.last_error = None


def mark_account_error(
    session: Session, name: str, err: str, *, owner_chat_id: str | None = None
) -> None:
    row = get_account_by_name(session, name, owner_chat_id=owner_chat_id)
    if row is not None:
        row.last_error = err[:2000]


# --- Bot users ------------------------------------------------------------


def get_bot_user(session: Session, chat_id: str) -> BotUserRow | None:
    stmt = select(BotUserRow).where(BotUserRow.chat_id == chat_id)
    return session.execute(stmt).scalar_one_or_none()


def count_bot_users(session: Session) -> int:
    return len(list(session.execute(select(BotUserRow.id)).all()))


def upsert_bot_user(
    session: Session,
    chat_id: str,
    telegram_username: str | None,
    *,
    auto_authorize_if_first: bool,
) -> tuple[BotUserRow, bool]:
    """Register a chat. Returns (row, is_newly_created).

    If `auto_authorize_if_first` is True and no rows exist yet, the new row
    is marked as owner + authorized.
    """
    existing = get_bot_user(session, chat_id)
    if existing is not None:
        if telegram_username and existing.telegram_username != telegram_username:
            existing.telegram_username = telegram_username
        return existing, False

    make_owner = auto_authorize_if_first and count_bot_users(session) == 0
    row = BotUserRow(
        chat_id=chat_id,
        telegram_username=telegram_username,
        is_authorized=make_owner,
        is_owner=make_owner,
    )
    session.add(row)
    session.flush()
    return row, True


def authorize_bot_user(session: Session, chat_id: str) -> bool:
    row = get_bot_user(session, chat_id)
    if row is None:
        return False
    row.is_authorized = True
    return True


def list_authorized_chat_ids(session: Session) -> list[str]:
    stmt = select(BotUserRow.chat_id).where(BotUserRow.is_authorized == True)  # noqa: E712
    return [r for r in session.execute(stmt).scalars().all()]


# --- Pending events -------------------------------------------------------


def find_pending_by_fingerprint(
    session: Session, fingerprint: str, *, owner_chat_id: str | None = None
) -> PendingEventRow | None:
    """Find a pending event by fingerprint, scoped to a specific owner.

    Scoping means two different users with the same event in their inboxes
    each get their own prompt — we don't cross-pollinate.
    """
    stmt = select(PendingEventRow).where(PendingEventRow.fingerprint == fingerprint)
    if owner_chat_id is not None:
        stmt = stmt.where(PendingEventRow.owner_chat_id == owner_chat_id)
    return session.execute(stmt).scalar_one_or_none()


def insert_pending_event(
    session: Session,
    *,
    email_id: int,
    owner_chat_id: str | None,
    fingerprint: str,
    title: str,
    start_iso: str,
    end_iso: str,
    timezone_name: str,
    event_body_json: str,
) -> PendingEventRow:
    row = PendingEventRow(
        email_id=email_id,
        owner_chat_id=owner_chat_id,
        fingerprint=fingerprint,
        title=title,
        start_iso=start_iso,
        end_iso=end_iso,
        timezone_name=timezone_name,
        event_body_json=event_body_json,
        status="pending",
    )
    session.add(row)
    session.flush()
    return row


def get_pending(session: Session, pending_id: int) -> PendingEventRow | None:
    return session.get(PendingEventRow, pending_id)


def list_pending(
    session: Session, *, owner_chat_id: str | None = None
) -> list[PendingEventRow]:
    stmt = select(PendingEventRow).where(PendingEventRow.status == "pending")
    if owner_chat_id is not None:
        stmt = stmt.where(PendingEventRow.owner_chat_id == owner_chat_id)
    return list(session.execute(stmt).scalars().all())


def update_pending_prompt(
    session: Session, pending_id: int, chat_id: str, message_id: str
) -> None:
    row = session.get(PendingEventRow, pending_id)
    if row is not None:
        row.prompt_chat_id = chat_id
        row.prompt_message_id = message_id


def mark_pending_status(
    session: Session,
    pending_id: int,
    status: str,
    *,
    google_event_id: str | None = None,
    error: str | None = None,
) -> PendingEventRow | None:
    row = session.get(PendingEventRow, pending_id)
    if row is None:
        return None
    row.status = status
    row.decided_at = datetime.now(UTC)
    if google_event_id:
        row.google_event_id = google_event_id
    if error:
        row.last_error = error[:2000]
    return row


# --- Queue helpers --------------------------------------------------------
#
# Two durable job queues live in SQLite: email_jobs (fetch->process) and
# event_jobs (approve->GCal insert). Each job row has a lease-like state
# machine: queued -> processing (with locked_at/locked_by) -> done | failed.
# Stale locks age out so a crashed worker's jobs get re-claimed.


from datetime import timedelta  # noqa: E402


def enqueue_email_job(
    session: Session, *, email_id: int, owner_chat_id: str | None
) -> EmailJobRow:
    row = EmailJobRow(
        email_id=email_id,
        owner_chat_id=owner_chat_id,
        status="queued",
    )
    session.add(row)
    session.flush()
    return row


def enqueue_event_job(
    session: Session, *, pending_event_id: int, owner_chat_id: str | None
) -> EventJobRow:
    row = EventJobRow(
        pending_event_id=pending_event_id,
        owner_chat_id=owner_chat_id,
        status="queued",
    )
    session.add(row)
    session.flush()
    return row


def _claim_next(
    session: Session, model, worker_id: str, *, stale_after_sec: int = 600
):  # type: ignore[no-untyped-def]
    """Atomically pick the oldest runnable job and mark it processing.

    Runnable = `queued` with `next_run_at <= now`, OR `processing` whose
    `locked_at` is older than `stale_after_sec` (crashed worker).
    """
    now = datetime.now(UTC)
    stale_cutoff = now - timedelta(seconds=stale_after_sec)

    # Claim queued or stale-processing rows. Use explicit WHERE and a single
    # UPDATE with RETURNING would be ideal, but SQLite's support is uneven;
    # instead we do a pick-then-update inside the open transaction.
    stmt = (
        select(model)
        .where(
            (
                (model.status == "queued") & (model.next_run_at <= now)
            )
            | (
                (model.status == "processing") & (model.locked_at < stale_cutoff)
            )
        )
        .order_by(model.next_run_at.asc())
        .limit(1)
    )
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return None
    row.status = "processing"
    row.locked_at = now
    row.locked_by = worker_id
    row.attempts = row.attempts + 1
    session.flush()
    return row


def claim_next_email_job(
    session: Session, worker_id: str, *, stale_after_sec: int = 600
) -> EmailJobRow | None:
    return _claim_next(session, EmailJobRow, worker_id, stale_after_sec=stale_after_sec)


def claim_next_event_job(
    session: Session, worker_id: str, *, stale_after_sec: int = 600
) -> EventJobRow | None:
    return _claim_next(session, EventJobRow, worker_id, stale_after_sec=stale_after_sec)


def mark_job_done(session: Session, model, job_id: int) -> None:  # type: ignore[no-untyped-def]
    row = session.get(model, job_id)
    if row is None:
        return
    row.status = "done"
    row.locked_at = None
    row.locked_by = None


def mark_job_failed(
    session: Session,
    model,  # type: ignore[no-untyped-def]
    job_id: int,
    *,
    error: str,
) -> None:
    row = session.get(model, job_id)
    if row is None:
        return
    row.status = "failed"
    row.locked_at = None
    row.locked_by = None
    row.last_error = error[:2000]


def reschedule_job(
    session: Session,
    model,  # type: ignore[no-untyped-def]
    job_id: int,
    *,
    error: str,
    backoff_seconds: int,
) -> None:
    row = session.get(model, job_id)
    if row is None:
        return
    row.status = "queued"
    row.locked_at = None
    row.locked_by = None
    row.last_error = error[:2000]
    row.next_run_at = datetime.now(UTC) + timedelta(seconds=backoff_seconds)


def count_queue(
    session: Session,
    model,  # type: ignore[no-untyped-def]
    *,
    status: str | None = None,
    owner_chat_id: str | None = None,
) -> int:
    stmt = select(model.id)
    if status is not None:
        stmt = stmt.where(model.status == status)
    if owner_chat_id is not None:
        stmt = stmt.where(model.owner_chat_id == owner_chat_id)
    return len(list(session.execute(stmt).all()))
