"""Queue drain loops for email_jobs and event_jobs.

Two durable queues live in SQLite. The scheduler tick calls `drain_emails`
and `drain_events` in sequence. Each claim is atomic (status flip +
locked_at under a single transaction), so a crashed worker's jobs get
re-claimed after their lease expires.

Design choices:
  - Serial draining keeps SQLite writes simple (no multi-writer contention).
  - Email jobs do the heavy lifting: classify -> extract -> propose events ->
    notify. The LLM call is outside the DB transaction (as before).
  - Event jobs do the GCal insert off the bot thread. On success, we update
    the pending row and edit the original Telegram prompt (via the bot
    runner bridge). On failure we back off and retry.
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from email_intel.integrations.openrouter import OpenRouterClient
from email_intel.integrations.telegram import TelegramNotifier
from email_intel.models import Email
from email_intel.pipeline import calendar as calendar_stage
from email_intel.pipeline import notify as notify_stage
from email_intel.pipeline.classify import classify
from email_intel.pipeline.parse import clean_body
from email_intel.pipeline.summarize import extract as llm_extract
from email_intel.providers.base import BaseEmailProvider
from email_intel.storage import repo
from email_intel.storage.db import session_scope
from email_intel.storage.schema import (
    EmailJobRow,
    EmailRow,
    EventJobRow,
    PendingEventRow,
)

log = logging.getLogger(__name__)

# How long a claimed job can stay "processing" before another worker steals it.
LEASE_SECONDS = 600
# Retry backoff for transient errors, capped.
BACKOFF_STEPS_SECONDS = (30, 120, 600, 1800)
MAX_ATTEMPTS = len(BACKOFF_STEPS_SECONDS) + 1


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _backoff_for(attempt: int) -> int:
    """Attempt is 1-based (first retry is attempts=1 after initial failure)."""
    idx = min(attempt - 1, len(BACKOFF_STEPS_SECONDS) - 1)
    return BACKOFF_STEPS_SECONDS[max(0, idx)]


# --- Enqueue API (called by fetch stage / bot callback) -------------------


def enqueue_email(
    session_factory: sessionmaker[Session],
    email: Email,
    owner_chat_id: str,
) -> int | None:
    """Insert the email row and enqueue a job. Idempotent on is_seen.

    Returns the job id, or None if this email was already seen.
    """
    with session_scope(session_factory) as s:
        if repo.is_seen(s, email.provider, email.message_id, email.raw_hash):
            return None
        email_row = repo.insert_email(s, email, owner_chat_id=owner_chat_id)
        job = repo.enqueue_email_job(
            s, email_id=email_row.id, owner_chat_id=owner_chat_id
        )
        return job.id


def fetch_and_enqueue(
    provider: BaseEmailProvider,
    session_factory: sessionmaker[Session],
    owner_chat_id: str,
) -> int:
    """Pull new emails from the provider into the queue. Returns enqueued count."""
    count = 0
    for email in provider.fetch_new():
        if enqueue_email(session_factory, email, owner_chat_id) is not None:
            count += 1
    log.info(
        "fetch_and_enqueue: account=%s enqueued=%d",
        provider.account_name,
        count,
    )
    return count


# --- Email queue drain ----------------------------------------------------


class BotPromptSender(Protocol):
    def send_pending_prompt(
        self, chat_id: str, row: Any, *, timeout: float = 30.0
    ) -> str | None: ...


def drain_emails(
    *,
    session_factory: sessionmaker[Session],
    llm: OpenRouterClient,
    telegram: TelegramNotifier,
    bot_runner: BotPromptSender | None,
    primary_model: str,
    fallback_model: str | None,
    app_timezone: str,
    max_jobs: int = 50,
) -> dict[str, int]:
    """Claim and process email jobs one at a time. Returns per-status stats."""
    stats = {"processed": 0, "skipped_ignore": 0, "failed": 0, "retried": 0}
    worker_id = _worker_id()
    for _ in range(max_jobs):
        claim_info = _claim_one_email_job(session_factory, worker_id)
        if claim_info is None:
            break
        job_id, email_id, owner_chat_id, attempts = claim_info
        try:
            outcome = _process_email_job(
                session_factory=session_factory,
                job_id=job_id,
                email_id=email_id,
                owner_chat_id=owner_chat_id,
                llm=llm,
                telegram=telegram,
                bot_runner=bot_runner,
                primary_model=primary_model,
                fallback_model=fallback_model,
                app_timezone=app_timezone,
            )
        except Exception as e:
            log.exception("Email job %d failed on attempt %d", job_id, attempts)
            _finalize_failure(
                session_factory,
                EmailJobRow,
                job_id,
                attempts,
                error=str(e),
                stats=stats,
            )
            continue

        if outcome == "skipped_ignore":
            stats["skipped_ignore"] += 1
        else:
            stats["processed"] += 1
        with session_scope(session_factory) as s:
            repo.mark_job_done(s, EmailJobRow, job_id)
    return stats


def _claim_one_email_job(
    session_factory: sessionmaker[Session], worker_id: str
) -> tuple[int, int, str | None, int] | None:
    with session_scope(session_factory) as s:
        row = repo.claim_next_email_job(s, worker_id, stale_after_sec=LEASE_SECONDS)
        if row is None:
            return None
        return row.id, row.email_id, row.owner_chat_id, row.attempts


def _process_email_job(
    *,
    session_factory: sessionmaker[Session],
    job_id: int,
    email_id: int,
    owner_chat_id: str | None,
    llm: OpenRouterClient,
    telegram: TelegramNotifier,
    bot_runner: BotPromptSender | None,
    primary_model: str,
    fallback_model: str | None,
    app_timezone: str,
) -> str:
    """Run the pipeline for a single queued email. Returns a stats key."""
    # Reconstitute the Email object from the DB row.
    email = _email_from_row(session_factory, email_id)
    if email is None:
        log.warning("email_id=%s missing; marking job %s done", email_id, job_id)
        return "processed"

    body = clean_body(email)
    cls = classify(email, body)

    with session_scope(session_factory) as s:
        email_row = s.get(EmailRow, email_id)
        if email_row is None:
            return "processed"
        if not cls.should_call_llm:
            email_row.processed = True
            email_row.importance = cls.importance_guess.value
            email_row.summary = f"(heuristic-skipped: {cls.reason})"
            return "skipped_ignore"

    # LLM call outside the transaction.
    extraction = llm_extract(
        client=llm,
        email=email,
        body=body,
        primary_model=primary_model,
        fallback_model=fallback_model,
        app_timezone=app_timezone,
    )

    pending_snapshot: list[dict[str, Any]] = []

    with session_scope(session_factory) as s:
        email_row = s.get(EmailRow, email_id)
        if email_row is None:
            return "processed"
        repo.save_extraction(s, email_row, extraction)

        new_pending = calendar_stage.propose_events_for_email(
            session=s,
            email_row=email_row,
            email=email,
            extraction=extraction,
            owner_chat_id=owner_chat_id,
            app_timezone=app_timezone,
        )
        for p in new_pending:
            pending_snapshot.append(
                {
                    "id": p.id,
                    "title": p.title,
                    "start_iso": p.start_iso,
                    "end_iso": p.end_iso,
                    "timezone_name": p.timezone_name,
                }
            )

        notify_stage.maybe_send_telegram(
            s,
            telegram,
            email_row,
            email,
            extraction,
            owner_chat_id=owner_chat_id,
            pending_events=len(new_pending),
        )

    # Send per-event approval prompts to the owner (if bot is up).
    if bot_runner is not None and owner_chat_id and pending_snapshot:
        for snap in pending_snapshot:
            message_id = bot_runner.send_pending_prompt(
                owner_chat_id,
                _PromptPayload(**snap),  # type: ignore[arg-type]
            )
            if message_id:
                with session_scope(session_factory) as s:
                    repo.update_pending_prompt(
                        s, int(snap["id"]), owner_chat_id, message_id
                    )

    return "processed"


def _email_from_row(
    session_factory: sessionmaker[Session], email_id: int
) -> Email | None:
    """Rebuild a pipeline Email object from a stored EmailRow.

    body_text is persisted on EmailRow at fetch time so the worker can resume
    after a crash without re-fetching from IMAP.
    """
    from email_intel.models import Email as EmailModel

    with session_scope(session_factory) as s:
        row = s.get(EmailRow, email_id)
        if row is None:
            return None
        return EmailModel(
            provider=row.provider,
            account_name=row.account_name,
            message_id=row.message_id,
            sender=row.sender,
            subject=row.subject,
            received_at=row.received_at,
            body_text=row.body_text or "",
            body_html=None,
            folder="INBOX",
            labels=[],
            headers={},
            attachments=[],
            raw_hash=row.raw_hash,
        )


class _PromptPayload:
    """Minimal shape satisfying BotRunner.send_pending_prompt."""

    def __init__(
        self,
        id: int,  # noqa: A002
        title: str,
        start_iso: str,
        end_iso: str,
        timezone_name: str,
    ) -> None:
        self.id = id
        self.title = title
        self.start_iso = start_iso
        self.end_iso = end_iso
        self.timezone_name = timezone_name


# --- Event queue drain ----------------------------------------------------


class CalendarClientLike(Protocol):
    def insert_event(self, body: dict[str, Any]) -> dict[str, Any]: ...


class BotEditor(Protocol):
    def edit_pending_message(
        self, chat_id: str, message_id: str, text: str, *, timeout: float = 30.0
    ) -> bool: ...


def drain_events(
    *,
    session_factory: sessionmaker[Session],
    calendar: CalendarClientLike | None,
    bot_runner: BotEditor | None,
    max_jobs: int = 20,
) -> dict[str, int]:
    stats = {"created": 0, "failed": 0, "retried": 0, "skipped": 0}
    worker_id = _worker_id()

    for _ in range(max_jobs):
        claim = _claim_one_event_job(session_factory, worker_id)
        if claim is None:
            break
        job_id, pending_id, attempts = claim

        if calendar is None:
            with session_scope(session_factory) as s:
                repo.mark_job_failed(
                    s, EventJobRow, job_id, error="Google Calendar not configured"
                )
                repo.mark_pending_status(
                    s, pending_id, "failed", error="Google Calendar not configured"
                )
            stats["skipped"] += 1
            continue

        from email_intel.pipeline import pending as pending_mod

        # Load event body + message coords.
        with session_scope(session_factory) as s:
            pending = s.get(PendingEventRow, pending_id)
            if pending is None:
                repo.mark_job_done(s, EventJobRow, job_id)
                stats["skipped"] += 1
                continue
            body = pending_mod.event_body(pending)
            title = pending.title
            email_id = pending.email_id
            fingerprint = pending.fingerprint
            prompt_chat_id = pending.prompt_chat_id
            prompt_message_id = pending.prompt_message_id

        try:
            result = calendar.insert_event(body)
        except Exception as e:
            log.exception("GCal insert failed for pending_id=%s", pending_id)
            _finalize_failure(
                session_factory,
                EventJobRow,
                job_id,
                attempts,
                error=str(e),
                stats=stats,
            )
            continue

        event_id = result.get("id") if isinstance(result, dict) else None
        with session_scope(session_factory) as s:
            repo.mark_pending_status(
                s, pending_id, "created", google_event_id=event_id
            )
            repo.record_calendar_event(
                s, email_id=email_id, google_event_id=event_id, fingerprint=fingerprint
            )
            repo.mark_job_done(s, EventJobRow, job_id)

        if bot_runner is not None and prompt_chat_id and prompt_message_id:
            bot_runner.edit_pending_message(
                prompt_chat_id,
                prompt_message_id,
                f"✅ Created: {title} (id={event_id})",
            )
        stats["created"] += 1

    return stats


def _claim_one_event_job(
    session_factory: sessionmaker[Session], worker_id: str
) -> tuple[int, int, int] | None:
    with session_scope(session_factory) as s:
        row = repo.claim_next_event_job(s, worker_id, stale_after_sec=LEASE_SECONDS)
        if row is None:
            return None
        return row.id, row.pending_event_id, row.attempts


# --- Shared failure handling ---------------------------------------------


def _finalize_failure(
    session_factory: sessionmaker[Session],
    model: Any,
    job_id: int,
    attempts: int,
    *,
    error: str,
    stats: dict[str, int],
) -> None:
    if attempts >= MAX_ATTEMPTS:
        with session_scope(session_factory) as s:
            repo.mark_job_failed(s, model, job_id, error=error)
        stats["failed"] += 1
        return
    backoff = _backoff_for(attempts)
    with session_scope(session_factory) as s:
        repo.reschedule_job(s, model, job_id, error=error, backoff_seconds=backoff)
    stats["retried"] += 1


