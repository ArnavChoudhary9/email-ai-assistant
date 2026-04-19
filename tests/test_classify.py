from __future__ import annotations

from email_intel.models import Importance
from email_intel.pipeline.classify import classify


def test_high_priority_keyword_triggers_llm(email_factory):
    email = email_factory(subject="Interview schedule", body="Your interview is at 3 PM.")
    c = classify(email, email.body_text)
    assert c.should_call_llm is True
    assert c.importance_guess == Importance.IMPORTANT
    assert "interview" in c.matched_keywords


def test_urgent_keyword(email_factory):
    email = email_factory(subject="URGENT: action required", body="please respond")
    c = classify(email, email.body_text)
    assert c.should_call_llm is True
    assert c.importance_guess == Importance.IMPORTANT


def test_promo_with_unsubscribe_header_is_ignored(email_factory):
    email = email_factory(
        subject="50% off this weekend",
        sender="newsletter@brand.com",
        body="Limited time offer. Unsubscribe here.",
        headers={"list-unsubscribe": "<mailto:u@brand.com>"},
    )
    c = classify(email, email.body_text)
    assert c.should_call_llm is False
    assert c.importance_guess == Importance.IGNORE


def test_ambiguous_defers_to_llm(email_factory):
    email = email_factory(subject="Hi", body="Just checking in")
    c = classify(email, email.body_text)
    assert c.should_call_llm is True
    assert c.importance_guess == Importance.NORMAL


def test_high_priority_beats_promo_signals(email_factory):
    # An email from a newsletter sender that still mentions a deadline should
    # not be silently ignored.
    email = email_factory(
        subject="Reminder: submission deadline tomorrow",
        sender="notifications@school.edu",
        body="Your assignment deadline is approaching.",
        headers={"list-unsubscribe": "<mailto:u@school.edu>"},
    )
    c = classify(email, email.body_text)
    assert c.should_call_llm is True
    assert c.importance_guess == Importance.IMPORTANT
