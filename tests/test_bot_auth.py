from __future__ import annotations

from email_intel.storage import repo
from email_intel.storage.db import session_scope


def test_first_user_becomes_owner(session_factory):
    with session_scope(session_factory) as s:
        row, is_new = repo.upsert_bot_user(
            s, "111", "alice", auto_authorize_if_first=True
        )
        assert is_new is True
        assert row.is_owner is True
        assert row.is_authorized is True


def test_second_user_is_not_auto_authorized(session_factory):
    with session_scope(session_factory) as s:
        repo.upsert_bot_user(s, "111", "alice", auto_authorize_if_first=True)
    with session_scope(session_factory) as s:
        row, is_new = repo.upsert_bot_user(
            s, "222", "bob", auto_authorize_if_first=True
        )
        assert is_new is True
        assert row.is_owner is False
        assert row.is_authorized is False


def test_authorize_flips_flag(session_factory):
    with session_scope(session_factory) as s:
        repo.upsert_bot_user(s, "111", "alice", auto_authorize_if_first=True)
        repo.upsert_bot_user(s, "222", "bob", auto_authorize_if_first=True)

    with session_scope(session_factory) as s:
        assert repo.authorize_bot_user(s, "222") is True

    with session_scope(session_factory) as s:
        row = repo.get_bot_user(s, "222")
        assert row is not None
        assert row.is_authorized is True


def test_authorized_chat_id_listing(session_factory):
    with session_scope(session_factory) as s:
        repo.upsert_bot_user(s, "111", "alice", auto_authorize_if_first=True)
        repo.upsert_bot_user(s, "222", "bob", auto_authorize_if_first=True)
        repo.authorize_bot_user(s, "222")

    with session_scope(session_factory) as s:
        ids = set(repo.list_authorized_chat_ids(s))
        assert ids == {"111", "222"}


def test_reregister_updates_username_not_authorization(session_factory):
    with session_scope(session_factory) as s:
        repo.upsert_bot_user(s, "111", "alice", auto_authorize_if_first=True)

    with session_scope(session_factory) as s:
        row, is_new = repo.upsert_bot_user(
            s, "111", "alice_new", auto_authorize_if_first=True
        )
        assert is_new is False
        assert row.telegram_username == "alice_new"
        assert row.is_owner is True
