"""Idempotent SQLite schema migrations run at startup.

Kept intentionally small: SQLAlchemy's `create_all` builds fresh tables, but
it won't ALTER existing columns. Anything that mutates a pre-existing table
goes here.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text
from sqlalchemy.orm import sessionmaker

from email_intel.storage import repo
from email_intel.storage.db import session_scope
from email_intel.storage.schema import Base

log = logging.getLogger(__name__)


def _columns(engine: Engine, table: str) -> set[str]:
    insp = inspect(engine)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def _add_column(engine: Engine, table: str, ddl: str) -> None:
    """ALTER TABLE ADD COLUMN. Caller checks column absence first."""
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def run_migrations(engine: Engine, session_factory: sessionmaker) -> None:
    """Apply in-place schema changes for pre-scoping databases.

    Safe to call every startup. Each step checks state before acting.
    """
    # create_all is a no-op for existing tables; ensures new tables appear on
    # upgraded DBs (email_jobs, event_jobs).
    Base.metadata.create_all(engine)
    _ensure_owner_columns(engine)
    _backfill_owner(session_factory)


def _ensure_owner_columns(engine: Engine) -> None:
    accounts_cols = _columns(engine, "accounts")
    if accounts_cols and "owner_chat_id" not in accounts_cols:
        log.info("Migrating accounts: adding owner_chat_id column")
        _add_column(engine, "accounts", "owner_chat_id VARCHAR(32)")

    emails_cols = _columns(engine, "emails")
    if emails_cols and "owner_chat_id" not in emails_cols:
        log.info("Migrating emails: adding owner_chat_id column")
        _add_column(engine, "emails", "owner_chat_id VARCHAR(32)")
    if emails_cols and "body_text" not in emails_cols:
        log.info("Migrating emails: adding body_text column")
        _add_column(engine, "emails", "body_text TEXT")

    pending_cols = _columns(engine, "pending_events")
    if pending_cols and "owner_chat_id" not in pending_cols:
        log.info("Migrating pending_events: adding owner_chat_id column")
        _add_column(engine, "pending_events", "owner_chat_id VARCHAR(32)")


def _backfill_owner(session_factory: sessionmaker) -> None:
    """Assign the bot owner as the owner of any ownerless rows.

    Runs lazily: if there's no bot owner yet (nobody has /start'd), no-op.
    The first /start triggers a re-run check via runtime build path.
    """
    with session_scope(session_factory) as s:
        owner = _find_owner_chat_id(s)
        if not owner:
            return

        for table in ("accounts", "emails", "pending_events"):
            result = s.execute(
                text(
                    f"UPDATE {table} SET owner_chat_id = :owner "
                    "WHERE owner_chat_id IS NULL"
                ),
                {"owner": owner},
            )
            rowcount = getattr(result, "rowcount", 0) or 0
            if rowcount:
                log.info(
                    "Backfilled owner_chat_id=%s on %d %s row(s)",
                    owner,
                    rowcount,
                    table,
                )


def _find_owner_chat_id(session) -> str | None:  # type: ignore[no-untyped-def]
    from email_intel.storage.schema import BotUserRow
    from sqlalchemy import select

    stmt = select(BotUserRow.chat_id).where(BotUserRow.is_owner == True)  # noqa: E712
    row = session.execute(stmt).scalar_one_or_none()
    if row:
        return str(row)
    # No owner yet; pick the first authorized chat if any (handles edge case
    # where owner flag was never set).
    chat_ids = repo.list_authorized_chat_ids(session)
    return chat_ids[0] if chat_ids else None
