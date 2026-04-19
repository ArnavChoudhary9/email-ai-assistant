from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime

from email_intel.models import Email


class BaseEmailProvider(ABC):
    """Abstract provider. Concrete impls: IMAP (MVP), Gmail API, Roundcube (future)."""

    account_name: str
    provider_type: str

    @abstractmethod
    def fetch_new(self, since: datetime | None = None) -> Iterator[Email]:
        """Yield emails received since `since`. Providers must not mark messages read.

        Dedup is the caller's responsibility via the local db.
        """
