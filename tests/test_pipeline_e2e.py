"""End-to-end pipeline via the queue: enqueue -> drain -> verify side effects."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import respx

from email_intel.integrations.openrouter import OPENROUTER_URL, OpenRouterClient
from email_intel.integrations.telegram import TelegramNotifier
from email_intel.pipeline import worker
from email_intel.storage import repo
from email_intel.storage.db import session_scope
from email_intel.storage.schema import EmailJobRow

OWNER = "111"


def _llm_ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _drain(session_factory, llm, telegram):
    return worker.drain_emails(
        session_factory=session_factory,
        llm=llm,
        telegram=telegram,
        bot_runner=None,
        primary_model="primary",
        fallback_model="fallback",
        app_timezone="Asia/Kolkata",
    )


@respx.mock
def test_important_email_drains_and_alerts(session_factory, email_factory):
    email = email_factory(
        subject="Urgent reply please",
        sender="manager@iitd.ac.in",
        body="Please reply ASAP — no meeting required.",
    )
    respx.post(OPENROUTER_URL).mock(
        return_value=_llm_ok(
            '{"summary":"Reply needed urgently","importance":"important",'
            '"action_required":true,"deadline":"",'
            '"meeting":{"exists":false,"date":"","time":"","location":""},'
            '"tasks":["Reply to manager"],"calendar_events":[],'
            '"reply_needed":true,"reply_priority":"urgent"}'
        )
    )

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.send.return_value = True

    job_id = worker.enqueue_email(session_factory, email, OWNER)
    assert job_id is not None

    with OpenRouterClient("sk-test") as llm:
        stats = _drain(session_factory, llm, telegram)

    assert stats["processed"] == 1
    telegram.send.assert_called_once()
    chat, msg = telegram.send.call_args.args
    assert chat == OWNER
    assert "[IMPORTANT]" in msg
    assert "Reply to manager" in msg

    with session_scope(session_factory) as s:
        # Job is done.
        assert repo.count_queue(s, EmailJobRow, status="done") == 1
        assert repo.count_queue(s, EmailJobRow, status="queued") == 0


@respx.mock
def test_meeting_email_creates_pending_via_queue(session_factory, email_factory):
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
    telegram.send.return_value = True

    worker.enqueue_email(session_factory, email, OWNER)
    with OpenRouterClient("sk-test") as llm:
        _drain(session_factory, llm, telegram)

    with session_scope(session_factory) as s:
        pending = repo.list_pending(s, owner_chat_id=OWNER)
    assert len(pending) == 1
    row = pending[0]
    assert row.title.startswith("Meeting:")
    assert row.start_iso.startswith("2026-04-21T15:00")
    assert "+05:30" in row.start_iso
    assert row.owner_chat_id == OWNER


@respx.mock
def test_reminder_emails_dedup_via_queue(session_factory, email_factory):
    """Two reminder emails about the same event → one pending row."""

    def _llm() -> httpx.Response:
        return _llm_ok(
            '{"summary":"Interview at 3 PM","importance":"important",'
            '"action_required":true,"deadline":"",'
            '"meeting":{"exists":true,"date":"2026-04-21","time":"15:00","location":"Room 204"},'
            '"tasks":[],"calendar_events":[],'
            '"reply_needed":false,"reply_priority":""}'
        )

    respx.post(OPENROUTER_URL).mock(side_effect=[_llm(), _llm()])

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.send.return_value = True

    worker.enqueue_email(
        session_factory,
        email_factory(subject="Interview invitation", message_id="msg-a"),
        OWNER,
    )
    worker.enqueue_email(
        session_factory,
        email_factory(subject="Reminder: Interview invitation", message_id="msg-b"),
        OWNER,
    )

    with OpenRouterClient("sk-test") as llm:
        stats = _drain(session_factory, llm, telegram)

    assert stats["processed"] == 2
    with session_scope(session_factory) as s:
        assert len(repo.list_pending(s, owner_chat_id=OWNER)) == 1


@respx.mock
def test_promo_email_skipped(session_factory, email_factory):
    email = email_factory(
        subject="50% off this weekend",
        sender="newsletter@brand.com",
        body="Limited time offer. Click to unsubscribe.",
        headers={"list-unsubscribe": "<mailto:u@brand.com>"},
    )
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(500))

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.send.return_value = True

    worker.enqueue_email(session_factory, email, OWNER)
    with OpenRouterClient("sk-test") as llm:
        stats = _drain(session_factory, llm, telegram)

    assert stats["skipped_ignore"] == 1
    assert stats["processed"] == 0
    telegram.send.assert_not_called()
    assert route.called is False


@respx.mock
def test_llm_failure_reschedules(session_factory, email_factory):
    """Transient LLM failure → job goes back to queued with backoff."""
    email = email_factory(subject="Urgent meeting", body="Tomorrow at 3 PM")
    respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(500))

    telegram = MagicMock(spec=TelegramNotifier)
    telegram.send.return_value = True

    job_id = worker.enqueue_email(session_factory, email, OWNER)
    assert job_id is not None

    with OpenRouterClient("sk-test") as llm:
        stats = _drain(session_factory, llm, telegram)

    assert stats["retried"] == 1
    with session_scope(session_factory) as s:
        row = s.get(EmailJobRow, job_id)
        assert row is not None
        assert row.status == "queued"
        assert row.last_error
        assert row.attempts >= 1
