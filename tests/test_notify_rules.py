from __future__ import annotations

from email_intel.integrations.telegram import format_message, should_alert
from email_intel.models import Extraction, Importance, Meeting, ReplyPriority


def test_alert_on_critical():
    assert should_alert(Extraction(importance=Importance.CRITICAL)) is True


def test_alert_on_important():
    assert should_alert(Extraction(importance=Importance.IMPORTANT)) is True


def test_alert_on_deadline_even_if_normal():
    assert should_alert(Extraction(importance=Importance.NORMAL, deadline="2026-05-01")) is True


def test_alert_on_meeting_even_if_normal():
    assert should_alert(
        Extraction(importance=Importance.NORMAL, meeting=Meeting(exists=True, date="2026-05-01"))
    ) is True


def test_no_alert_on_plain_normal():
    assert should_alert(Extraction(importance=Importance.NORMAL)) is False


def test_no_alert_on_ignore():
    assert should_alert(Extraction(importance=Importance.IGNORE)) is False


def test_urgent_reply_alerts():
    assert (
        should_alert(
            Extraction(
                importance=Importance.NORMAL,
                reply_needed=True,
                reply_priority=ReplyPriority.URGENT,
            )
        )
        is True
    )


def test_normal_reply_does_not_alert():
    assert (
        should_alert(
            Extraction(
                importance=Importance.NORMAL,
                reply_needed=True,
                reply_priority=ReplyPriority.NORMAL,
            )
        )
        is False
    )


def test_format_message_contains_key_fields(email_factory):
    email = email_factory(subject="Interview tomorrow", sender="placement@iitd.ac.in")
    ex = Extraction(
        summary="Interview at 3 PM tomorrow",
        importance=Importance.IMPORTANT,
        deadline="2026-04-20",
        tasks=["Bring resume", "Prepare intro"],
        reply_needed=True,
        reply_priority=ReplyPriority.URGENT,
    )
    msg = format_message(email, ex)
    assert "[IMPORTANT]" in msg
    assert "placement@iitd.ac.in" in msg
    assert "Interview tomorrow" in msg
    assert "Interview at 3 PM tomorrow" in msg
    assert "2026-04-20" in msg
    assert "- Bring resume" in msg
    assert "urgent" in msg.lower()
