from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy.orm import Session, sessionmaker

from email_intel.models import Email
from email_intel.providers.base import BaseEmailProvider
from email_intel.storage import repo
from email_intel.storage.db import session_scope

log = logging.getLogger(__name__)


def fetch_unseen(
    provider: BaseEmailProvider,
    session_factory: sessionmaker[Session],
) -> Iterator[Email]:
    """Yield emails the provider has surfaced that the local db has not yet seen."""
    seen = 0
    new = 0
    for email in provider.fetch_new():
        seen += 1
        with session_scope(session_factory) as s:
            if repo.is_seen(s, email.provider, email.message_id, email.raw_hash):
                continue
        new += 1
        yield email
    log.info(
        "fetch_unseen: account=%s scanned=%d new=%d",
        provider.account_name,
        seen,
        new,
    )
