"""Migration: legacy DB with ownerless rows gets backfilled to the bot owner."""

from __future__ import annotations

from sqlalchemy import text

from email_intel.storage import repo
from email_intel.storage.db import make_engine, make_session_factory, session_scope
from email_intel.storage.migrate import run_migrations


def test_backfill_to_owner():
    engine = make_engine(":memory:")
    sf = make_session_factory(engine)

    # Set up a pre-scope-era row: insert an account with a NULL owner_chat_id
    # (the column exists because schema is fresh, but the app used to leave
    # it NULL). Also set up a bot owner.
    with session_scope(sf) as s:
        repo.upsert_bot_user(s, "999", "alice", auto_authorize_if_first=True)
        s.execute(
            text(
                "INSERT INTO accounts (name, type, host, port, use_ssl, email, "
                "password_encrypted, folder, initial_lookback_days, enabled, created_at) "
                "VALUES ('legacy', 'imap', 'host', 993, 1, 'l@x.com', 'enc', 'INBOX', 3, 1, "
                "CURRENT_TIMESTAMP)"
            )
        )

    run_migrations(engine, sf)

    with session_scope(sf) as s:
        row = repo.get_account_by_name(s, "legacy", owner_chat_id="999")
        assert row is not None
        assert row.owner_chat_id == "999"


def test_backfill_noop_without_owner():
    engine = make_engine(":memory:")
    sf = make_session_factory(engine)

    with session_scope(sf) as s:
        s.execute(
            text(
                "INSERT INTO accounts (name, type, host, port, use_ssl, email, "
                "password_encrypted, folder, initial_lookback_days, enabled, created_at) "
                "VALUES ('legacy', 'imap', 'host', 993, 1, 'l@x.com', 'enc', 'INBOX', 3, 1, "
                "CURRENT_TIMESTAMP)"
            )
        )

    # No bot owner → migration leaves owner_chat_id NULL; app layer filters
    # these out with a warning.
    run_migrations(engine, sf)

    with session_scope(sf) as s:
        # With no scope, we can see the row.
        rows = repo.list_accounts(s)
        assert len(rows) == 1
        assert rows[0].owner_chat_id is None
