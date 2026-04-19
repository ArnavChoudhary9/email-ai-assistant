"""Durable queue behavior: atomic claim, stale-lease recovery, retry backoff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from email_intel.storage import repo
from email_intel.storage.db import session_scope
from email_intel.storage.schema import EmailJobRow, EmailRow, EventJobRow


def _seed_email(session, owner="1001", message_id="m-1") -> int:
    row = EmailRow(
        provider="imap",
        account_name="a",
        owner_chat_id=owner,
        message_id=message_id,
        sender="s",
        subject="subj",
        received_at=datetime.now(timezone.utc),
        raw_hash=f"h-{message_id}",
    )
    session.add(row)
    session.flush()
    return row.id


def test_enqueue_and_claim(session_factory):
    with session_scope(session_factory) as s:
        email_id = _seed_email(s)
        job = repo.enqueue_email_job(s, email_id=email_id, owner_chat_id="1001")
        job_id = job.id

    with session_scope(session_factory) as s:
        claimed = repo.claim_next_email_job(s, "worker-A")
        assert claimed is not None
        assert claimed.id == job_id
        assert claimed.status == "processing"
        assert claimed.locked_by == "worker-A"
        assert claimed.attempts == 1

    # A second claim finds nothing — the job is locked.
    with session_scope(session_factory) as s:
        assert repo.claim_next_email_job(s, "worker-B") is None


def test_stale_lease_recovery(session_factory):
    with session_scope(session_factory) as s:
        email_id = _seed_email(s)
        job = repo.enqueue_email_job(s, email_id=email_id, owner_chat_id="1001")
        job_id = job.id

    # Claim by worker-A.
    with session_scope(session_factory) as s:
        repo.claim_next_email_job(s, "worker-A")

    # Backdate locked_at so the lease looks stale.
    with session_scope(session_factory) as s:
        row = s.get(EmailJobRow, job_id)
        assert row is not None
        row.locked_at = datetime.now(timezone.utc) - timedelta(hours=2)

    # worker-B reclaims.
    with session_scope(session_factory) as s:
        reclaimed = repo.claim_next_email_job(s, "worker-B", stale_after_sec=60)
        assert reclaimed is not None
        assert reclaimed.id == job_id
        assert reclaimed.locked_by == "worker-B"
        assert reclaimed.attempts == 2  # attempt counter advanced on re-claim


def test_mark_done_releases_slot(session_factory):
    with session_scope(session_factory) as s:
        email_id = _seed_email(s)
        job = repo.enqueue_email_job(s, email_id=email_id, owner_chat_id="1001")
        job_id = job.id

    with session_scope(session_factory) as s:
        repo.claim_next_email_job(s, "worker-A")
    with session_scope(session_factory) as s:
        repo.mark_job_done(s, EmailJobRow, job_id)

    with session_scope(session_factory) as s:
        row = s.get(EmailJobRow, job_id)
        assert row is not None
        assert row.status == "done"
        assert row.locked_at is None


def test_reschedule_uses_backoff(session_factory):
    with session_scope(session_factory) as s:
        email_id = _seed_email(s)
        job = repo.enqueue_email_job(s, email_id=email_id, owner_chat_id="1001")
        job_id = job.id

    with session_scope(session_factory) as s:
        repo.claim_next_email_job(s, "worker-A")

    with session_scope(session_factory) as s:
        repo.reschedule_job(
            s, EmailJobRow, job_id, error="transient", backoff_seconds=120
        )

    with session_scope(session_factory) as s:
        row = s.get(EmailJobRow, job_id)
        assert row is not None
        assert row.status == "queued"
        assert row.last_error == "transient"
        # next_run_at is in the future → claim_next should NOT pick it yet.
        # SQLite returns naive datetimes; compare to a naive UTC reference.
        assert row.next_run_at > datetime.now(timezone.utc).replace(tzinfo=None)

    with session_scope(session_factory) as s:
        assert repo.claim_next_email_job(s, "worker-A") is None


def test_mark_failed_terminal(session_factory):
    with session_scope(session_factory) as s:
        email_id = _seed_email(s)
        job = repo.enqueue_email_job(s, email_id=email_id, owner_chat_id="1001")
        job_id = job.id

    with session_scope(session_factory) as s:
        repo.claim_next_email_job(s, "worker-A")
    with session_scope(session_factory) as s:
        repo.mark_job_failed(s, EmailJobRow, job_id, error="bad JSON")

    with session_scope(session_factory) as s:
        # A failed job is NOT picked up even by claim.
        assert repo.claim_next_email_job(s, "worker-B") is None


def test_event_queue_separate_from_email_queue(session_factory):
    """Claiming an email job must not touch event jobs and vice versa."""
    with session_scope(session_factory) as s:
        email_id = _seed_email(s)
        repo.enqueue_email_job(s, email_id=email_id, owner_chat_id="1001")

        from email_intel.storage.schema import PendingEventRow

        pending = PendingEventRow(
            email_id=email_id,
            owner_chat_id="1001",
            fingerprint="fp-1",
            title="t",
            start_iso="2026-04-20T15:00:00+05:30",
            end_iso="2026-04-20T16:00:00+05:30",
            timezone_name="Asia/Kolkata",
            event_body_json="{}",
        )
        s.add(pending)
        s.flush()
        repo.enqueue_event_job(
            s, pending_event_id=pending.id, owner_chat_id="1001"
        )

    with session_scope(session_factory) as s:
        e1 = repo.claim_next_email_job(s, "w-a")
        assert e1 is not None
    with session_scope(session_factory) as s:
        e2 = repo.claim_next_event_job(s, "w-a")
        assert e2 is not None
        # Second event claim is empty (only one in the queue).
    with session_scope(session_factory) as s:
        assert repo.claim_next_event_job(s, "w-b") is None


def test_count_queue(session_factory):
    with session_scope(session_factory) as s:
        e1 = _seed_email(s, message_id="m-1")
        e2 = _seed_email(s, message_id="m-2")
        repo.enqueue_email_job(s, email_id=e1, owner_chat_id="1001")
        repo.enqueue_email_job(s, email_id=e2, owner_chat_id="2002")

    with session_scope(session_factory) as s:
        assert repo.count_queue(s, EmailJobRow, status="queued") == 2
        assert (
            repo.count_queue(s, EmailJobRow, status="queued", owner_chat_id="1001")
            == 1
        )


def test_enqueue_email_idempotent_on_seen(session_factory, email_factory):
    """enqueue_email de-dupes via is_seen."""
    from email_intel.pipeline import worker

    email = email_factory(subject="hello", message_id="same")

    job1 = worker.enqueue_email(session_factory, email, "1001")
    job2 = worker.enqueue_email(session_factory, email, "1001")

    assert job1 is not None
    assert job2 is None  # second call sees the existing email, skips

    with session_scope(session_factory) as s:
        assert repo.count_queue(s, EmailJobRow) == 1
