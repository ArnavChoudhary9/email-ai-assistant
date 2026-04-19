"""Account resolution: DB -> runtime IMAPAccount objects, with optional YAML seed."""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from email_intel.config import IMAPAccount, load_accounts_from_yaml
from email_intel.security import FernetCipher
from email_intel.storage import repo
from email_intel.storage.db import session_scope

log = logging.getLogger(__name__)


def seed_from_yaml_if_empty(
    session_factory: sessionmaker[Session],
    cipher: FernetCipher,
    yaml_path: Path,
) -> int:
    """If the accounts table is empty, import rows from accounts.yaml.

    Returns number of rows inserted. Idempotent: becomes a no-op once the
    table has any row.
    """
    with session_scope(session_factory) as s:
        if repo.list_accounts(s, enabled_only=False):
            return 0
        yaml_accounts = load_accounts_from_yaml(yaml_path)
        if not yaml_accounts:
            return 0
        for acc in yaml_accounts:
            repo.insert_account(
                s,
                name=acc.name,
                host=acc.host,
                port=acc.port,
                use_ssl=acc.use_ssl,
                email=acc.email,
                password_encrypted=cipher.encrypt(acc.password.get_secret_value()),
                folder=acc.folder,
                initial_lookback_days=acc.initial_lookback_days,
            )
        log.info("Seeded %d account(s) from %s into DB", len(yaml_accounts), yaml_path)
        return len(yaml_accounts)


def load_runtime_accounts(
    session_factory: sessionmaker[Session],
    cipher: FernetCipher,
) -> list[IMAPAccount]:
    """Pull enabled accounts from DB, decrypt passwords, return IMAPAccount objs."""
    with session_scope(session_factory) as s:
        rows = repo.list_accounts(s, enabled_only=True)
        out: list[IMAPAccount] = []
        for row in rows:
            try:
                pwd = cipher.decrypt(row.password_encrypted)
            except Exception:
                log.exception("Failed to decrypt password for account %s; skipping", row.name)
                continue
            out.append(
                IMAPAccount(
                    name=row.name,
                    type="imap",
                    host=row.host,
                    port=row.port,
                    use_ssl=row.use_ssl,
                    email=row.email,
                    password=SecretStr(pwd),
                    folder=row.folder,
                    initial_lookback_days=row.initial_lookback_days,
                )
            )
        return out
