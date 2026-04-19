from __future__ import annotations

import pytest

from email_intel.storage import repo
from email_intel.storage.db import session_scope


ALICE = "1001"
BOB = "2002"


def _insert(session, *, name, owner):
    return repo.insert_account(
        session,
        name=name,
        owner_chat_id=owner,
        host="imap.example.com",
        port=993,
        use_ssl=True,
        email=f"{name}@example.com",
        password_encrypted="<encrypted>",
    )


def test_list_scopes_by_owner(session_factory):
    with session_scope(session_factory) as s:
        _insert(s, name="alice-main", owner=ALICE)
        _insert(s, name="alice-work", owner=ALICE)
        _insert(s, name="bob-main", owner=BOB)

    with session_scope(session_factory) as s:
        alice_accts = {a.name for a in repo.list_accounts(s, owner_chat_id=ALICE)}
        bob_accts = {a.name for a in repo.list_accounts(s, owner_chat_id=BOB)}
    assert alice_accts == {"alice-main", "alice-work"}
    assert bob_accts == {"bob-main"}


def test_get_account_respects_owner(session_factory):
    with session_scope(session_factory) as s:
        _insert(s, name="shared", owner=ALICE)

    with session_scope(session_factory) as s:
        assert repo.get_account_by_name(s, "shared", owner_chat_id=ALICE) is not None
        # Bob can't see Alice's account even though he knows the name.
        assert repo.get_account_by_name(s, "shared", owner_chat_id=BOB) is None


def test_delete_respects_owner(session_factory):
    with session_scope(session_factory) as s:
        _insert(s, name="important", owner=ALICE)

    with session_scope(session_factory) as s:
        # Bob tries to delete Alice's account — no-op.
        assert repo.delete_account(s, "important", owner_chat_id=BOB) is False

    with session_scope(session_factory) as s:
        assert repo.get_account_by_name(s, "important", owner_chat_id=ALICE) is not None

    with session_scope(session_factory) as s:
        assert repo.delete_account(s, "important", owner_chat_id=ALICE) is True


def test_same_name_allowed_across_owners(session_factory):
    """Two different users can each have an account called "main"."""
    with session_scope(session_factory) as s:
        _insert(s, name="main", owner=ALICE)
        _insert(s, name="main", owner=BOB)

    with session_scope(session_factory) as s:
        a = repo.get_account_by_name(s, "main", owner_chat_id=ALICE)
        b = repo.get_account_by_name(s, "main", owner_chat_id=BOB)
        assert a is not None and b is not None
        assert a.id != b.id


def test_same_name_same_owner_rejected(session_factory):
    """But the same user can't have two accounts with the same name."""
    with session_scope(session_factory) as s:
        _insert(s, name="dup", owner=ALICE)

    # Use a fresh session so the flush error propagates out cleanly.
    with pytest.raises(Exception):
        with session_scope(session_factory) as s:
            _insert(s, name="dup", owner=ALICE)


def test_delete_accounts_for_chat_cascades(session_factory):
    with session_scope(session_factory) as s:
        _insert(s, name="one", owner=BOB)
        _insert(s, name="two", owner=BOB)
        _insert(s, name="not-bobs", owner=ALICE)

    with session_scope(session_factory) as s:
        deleted = repo.delete_accounts_for_chat(s, BOB)
        assert deleted == 2

    with session_scope(session_factory) as s:
        assert repo.list_accounts(s, owner_chat_id=BOB) == []
        # Alice's account is untouched.
        assert len(repo.list_accounts(s, owner_chat_id=ALICE)) == 1


def test_pending_fingerprint_scoped_by_owner(session_factory):
    """The same event fingerprint can exist for two different users."""
    # Simulate: both Alice and Bob receive the same interview notification.
    fp = "abc123deadbeef"

    def _insert_pending(session, owner, email_id):
        return repo.insert_pending_event(
            session,
            email_id=email_id,
            owner_chat_id=owner,
            fingerprint=fp,
            title="Interview",
            start_iso="2026-04-20T15:00:00+05:30",
            end_iso="2026-04-20T16:00:00+05:30",
            timezone_name="Asia/Kolkata",
            event_body_json="{}",
        )

    # Need email rows first (FK). Build two minimal ones.
    from email_intel.storage.schema import EmailRow

    with session_scope(session_factory) as s:
        e1 = EmailRow(
            provider="imap",
            account_name="a",
            owner_chat_id=ALICE,
            message_id="a-msg",
            sender="x",
            subject="Interview",
            received_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            raw_hash="h1",
        )
        e2 = EmailRow(
            provider="imap",
            account_name="b",
            owner_chat_id=BOB,
            message_id="b-msg",
            sender="x",
            subject="Interview",
            received_at=__import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ),
            raw_hash="h2",
        )
        s.add_all([e1, e2])
        s.flush()
        _insert_pending(s, ALICE, e1.id)
        _insert_pending(s, BOB, e2.id)

    with session_scope(session_factory) as s:
        alice_pending = repo.list_pending(s, owner_chat_id=ALICE)
        bob_pending = repo.list_pending(s, owner_chat_id=BOB)
        assert len(alice_pending) == 1
        assert len(bob_pending) == 1
        # Scoped fingerprint lookups return the right row for each user.
        a_row = repo.find_pending_by_fingerprint(s, fp, owner_chat_id=ALICE)
        b_row = repo.find_pending_by_fingerprint(s, fp, owner_chat_id=BOB)
        assert a_row is not None and b_row is not None
        assert a_row.id != b_row.id
