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


def _find_bot_owner(session: Session) -> str | None:
    from sqlalchemy import select

    from email_intel.storage.schema import BotUserRow

    stmt = select(BotUserRow.chat_id).where(BotUserRow.is_owner == True)  # noqa: E712
    row = session.execute(stmt).scalar_one_or_none()
    return str(row) if row else None


def seed_from_yaml_if_empty(
    session_factory: sessionmaker[Session],
    cipher: FernetCipher,
    yaml_path: Path,
) -> int:
    """If the accounts table is empty, import rows from accounts.yaml.

    Requires a bot owner to exist (since every account needs one). If there's
    no owner yet, logs a hint and returns 0; next cycle will retry.

    Returns number of rows inserted.
    """
    with session_scope(session_factory) as s:
        if repo.list_accounts(s, enabled_only=False):
            return 0
        yaml_accounts = load_accounts_from_yaml(yaml_path)
        if not yaml_accounts:
            return 0

        owner = _find_bot_owner(s)
        if not owner:
            log.info(
                "accounts.yaml found but no bot owner yet; /start the Telegram bot to seed"
            )
            return 0

        for acc in yaml_accounts:
            repo.insert_account(
                s,
                name=acc.name,
                owner_chat_id=owner,
                host=acc.host,
                port=acc.port,
                use_ssl=acc.use_ssl,
                email=acc.email,
                password_encrypted=cipher.encrypt(acc.password.get_secret_value()),
                folder=acc.folder,
                initial_lookback_days=acc.initial_lookback_days,
            )
        log.info(
            "Seeded %d account(s) from %s into DB (owner=%s)",
            len(yaml_accounts),
            yaml_path,
            owner,
        )
        return len(yaml_accounts)


def load_runtime_accounts(
    session_factory: sessionmaker[Session],
    cipher: FernetCipher,
) -> list[tuple[IMAPAccount, str]]:
    """Pull enabled accounts from DB, decrypt passwords.

    Returns a list of (account, owner_chat_id) pairs. Accounts without an
    owner are skipped with a warning — they shouldn't exist post-migration.
    """
    with session_scope(session_factory) as s:
        rows = repo.list_accounts(s, enabled_only=True)
        out: list[tuple[IMAPAccount, str]] = []
        for row in rows:
            if not row.owner_chat_id:
                log.warning(
                    "Account %s has no owner_chat_id; skipping (run migration?)",
                    row.name,
                )
                continue
            try:
                pwd = cipher.decrypt(row.password_encrypted)
            except Exception:
                log.exception("Failed to decrypt password for account %s; skipping", row.name)
                continue
            acc = IMAPAccount(
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
            out.append((acc, row.owner_chat_id))
        return out
