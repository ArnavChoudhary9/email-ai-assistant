from __future__ import annotations

from email_intel.models import Classification, Email, Importance

# PRD §7 keyword buckets. Tuned for university / internship / academic contexts.
HIGH_PRIORITY_KEYWORDS = frozenset(
    {
        "placement",
        "interview",
        "deadline",
        "exam",
        "professor",
        "payment due",
        "urgent",
        "action required",
        "immediate",
        "asap",
        "submission",
        "assignment due",
        "fee due",
        "viva",
        "defense",
        "response required",
        "please reply",
        "final reminder",
    }
)

PROMO_KEYWORDS = frozenset(
    {
        "unsubscribe",
        "newsletter",
        "you are receiving this",
        "promotional",
        "% off",
        "limited time offer",
        "exclusive deal",
        "sale ends",
        "sponsored",
    }
)

PROMO_SENDER_FRAGMENTS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "newsletter",
        "mailer",
        "marketing",
        "notifications@",
        "updates@",
    }
)


def classify(email: Email, body: str) -> Classification:
    """Cheap heuristic gate — decides whether an email is worth LLM attention."""
    haystack = f"{email.subject}\n{body}".lower()
    sender_lower = email.sender.lower()

    matched_high = [k for k in HIGH_PRIORITY_KEYWORDS if k in haystack]
    if matched_high:
        return Classification(
            importance_guess=Importance.IMPORTANT,
            should_call_llm=True,
            matched_keywords=matched_high,
            reason="matched high-priority keyword",
        )

    # Promo signals: List-Unsubscribe header, sender fragment, or body keyword.
    has_unsub_header = "list-unsubscribe" in email.headers
    promo_sender = any(f in sender_lower for f in PROMO_SENDER_FRAGMENTS)
    matched_promo = [k for k in PROMO_KEYWORDS if k in haystack]

    if has_unsub_header and (promo_sender or matched_promo):
        return Classification(
            importance_guess=Importance.IGNORE,
            should_call_llm=False,
            matched_keywords=matched_promo,
            reason="promotional signals (List-Unsubscribe + sender/keyword)",
        )

    if matched_promo and promo_sender:
        return Classification(
            importance_guess=Importance.IGNORE,
            should_call_llm=False,
            matched_keywords=matched_promo,
            reason="promotional sender + body keywords",
        )

    # Ambiguous — let the LLM decide.
    return Classification(
        importance_guess=Importance.NORMAL,
        should_call_llm=True,
        matched_keywords=[],
        reason="ambiguous — defer to LLM",
    )
