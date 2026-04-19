from __future__ import annotations

from email_intel.models import Extraction, Meeting
from email_intel.pipeline import calendar as calendar_stage
from email_intel.pipeline import pending
from email_intel.storage import repo
from email_intel.storage.db import session_scope


def test_fingerprint_stable_across_title_variants():
    a = pending.compute_fingerprint("Meeting: Interview", "2026-04-20T15:00:00+05:30")
    b = pending.compute_fingerprint("meeting: interview!!", "2026-04-20T15:00:00+05:30")
    assert a == b


def test_fingerprint_differs_by_date():
    a = pending.compute_fingerprint("Interview", "2026-04-20T15:00:00+05:30")
    b = pending.compute_fingerprint("Interview", "2026-04-21T15:00:00+05:30")
    assert a != b


def test_two_emails_same_event_produce_one_pending(session_factory, email_factory):
    extraction = Extraction(
        meeting=Meeting(exists=True, date="2026-04-20", time="15:00", location="IITD")
    )

    # Two distinct emails (different message_ids) describing the same event —
    # the common case when a sender blasts multiple reminder emails.
    email1 = email_factory(subject="Interview tomorrow", message_id="msg-1")
    email2 = email_factory(subject="Reminder: Interview tomorrow", message_id="msg-2")

    def _process(email):
        with session_scope(session_factory) as s:
            row = repo.insert_email(s, email)
            return calendar_stage.propose_events_for_email(
                session=s,
                email_row=row,
                email=email,
                extraction=extraction,
                app_timezone="Asia/Kolkata",
            )

    new1 = _process(email1)
    new2 = _process(email2)

    assert len(new1) == 1  # first email creates one pending row
    assert len(new2) == 0  # second email dedupes — no new rows

    with session_scope(session_factory) as s:
        all_pending = repo.list_pending(s)
        assert len(all_pending) == 1
