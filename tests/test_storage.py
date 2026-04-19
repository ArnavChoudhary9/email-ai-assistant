from __future__ import annotations

from email_intel.models import Extraction, Importance
from email_intel.storage import repo
from email_intel.storage.db import session_scope


def test_insert_and_dedup(session_factory, email_factory):
    email = email_factory()
    with session_scope(session_factory) as s:
        assert repo.is_seen(s, email.provider, email.message_id, email.raw_hash) is False
        repo.insert_email(s, email)

    with session_scope(session_factory) as s:
        assert repo.is_seen(s, email.provider, email.message_id, email.raw_hash) is True


def test_save_extraction_marks_processed(session_factory, email_factory):
    email = email_factory()
    with session_scope(session_factory) as s:
        row = repo.insert_email(s, email)
        assert row.processed is False

        ex = Extraction(
            summary="A summary",
            importance=Importance.IMPORTANT,
            tasks=["Reply to Alice", "Book a room"],
            deadline="2026-04-25",
        )
        repo.save_extraction(s, row, ex)
        row_id = row.id

    with session_scope(session_factory) as s:
        from email_intel.storage.schema import EmailRow, TaskRow

        row = s.get(EmailRow, row_id)
        assert row is not None
        assert row.processed is True
        assert row.importance == "important"
        assert row.summary == "A summary"

        tasks = s.query(TaskRow).filter_by(email_id=row_id).all()
        assert {t.title for t in tasks} == {"Reply to Alice", "Book a room"}
        assert all(t.due_date == "2026-04-25" for t in tasks)


def test_telegram_idempotency(session_factory, email_factory):
    email = email_factory()
    with session_scope(session_factory) as s:
        row = repo.insert_email(s, email)
        row_id = row.id

    with session_scope(session_factory) as s:
        assert repo.telegram_already_sent(s, row_id) is False
        repo.mark_telegram_sent(s, row_id)

    with session_scope(session_factory) as s:
        assert repo.telegram_already_sent(s, row_id) is True
        # Second mark is a no-op — still true, still only one row.
        repo.mark_telegram_sent(s, row_id)

    with session_scope(session_factory) as s:
        from email_intel.storage.schema import NotificationRow

        rows = s.query(NotificationRow).filter_by(email_id=row_id).all()
        assert len(rows) == 1
