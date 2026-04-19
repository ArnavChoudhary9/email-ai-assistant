from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import sessionmaker

from email_intel.models import Email
from email_intel.storage.db import make_engine, make_session_factory


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Short-circuit tenacity's backoff waits so retry tests run fast."""
    import tenacity.nap

    monkeypatch.setattr(tenacity.nap, "sleep", lambda _s: None)


@pytest.fixture
def session_factory() -> sessionmaker:
    engine = make_engine(":memory:")
    return make_session_factory(engine)


def make_email(
    *,
    subject: str = "Test subject",
    sender: str = "alice@example.com",
    body: str = "Hello body",
    body_html: str | None = None,
    headers: dict[str, str] | None = None,
    message_id: str | None = None,
    received_at: datetime | None = None,
    account_name: str = "test",
) -> Email:
    raw_hash = hashlib.sha256(f"{subject}|{sender}|{body}".encode()).hexdigest()
    return Email(
        provider="imap",
        account_name=account_name,
        message_id=message_id or raw_hash,
        sender=sender,
        subject=subject,
        received_at=received_at or datetime.now(UTC),
        body_text=body,
        body_html=body_html,
        headers=headers or {},
        raw_hash=raw_hash,
    )


@pytest.fixture
def email_factory():
    return make_email
