"""Pending scheduling events: propose -> Telegram prompt -> approve/reject -> create.

Fingerprint-based dedup across emails: multiple reminder emails for the same
interview collapse to a single pending row (and therefore a single calendar
event, a single prompt).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from email_intel.storage import repo
from email_intel.storage.schema import PendingEventRow

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")
# Strip prefixes our own builder adds ("Meeting:", "Deadline:") plus the
# common reminder/forward tokens that otherwise make equivalent reminder
# emails fingerprint differently.
_PREFIX_RE = re.compile(
    r"^(?:meeting|deadline|reminder|re|fwd|fw)\s*[:|\-–—]\s*",
    re.IGNORECASE,
)


def _strip_prefixes(title: str) -> str:
    t = title or ""
    for _ in range(4):  # handle "Fwd: Re: Reminder: Interview"
        new = _PREFIX_RE.sub("", t).strip()
        if new == t:
            break
        t = new
    return t


def compute_fingerprint(title: str, start_iso: str) -> str:
    """Stable hash of normalized title + start DATE (not time).

    Date-only so reminders that drift by a few minutes collapse. Prefixes
    like "Meeting:", "Reminder:", "Re:" are stripped so that the reminder
    emails about the same event produce the same fingerprint.
    """
    stripped = _strip_prefixes(title)
    norm = _WS.sub(" ", _PUNCT.sub(" ", stripped.lower())).strip()
    date_part = (start_iso or "")[:10]
    return hashlib.sha256(f"{norm}|{date_part}".encode()).hexdigest()


def propose(
    session: Session,
    *,
    email_id: int,
    owner_chat_id: str | None,
    title: str,
    start_iso: str,
    end_iso: str,
    timezone_name: str,
    event_body: dict[str, Any],
) -> tuple[PendingEventRow, bool]:
    """Insert a pending-event row for this proposed calendar event.

    Dedup is scoped per owner_chat_id — two different users seeing the same
    event in their own inboxes each get their own prompt.
    """
    fp = compute_fingerprint(title, start_iso)
    existing = repo.find_pending_by_fingerprint(session, fp, owner_chat_id=owner_chat_id)
    if existing is not None:
        log.info(
            "Pending event dedup hit: fingerprint=%s existing_id=%s status=%s title=%r owner=%s",
            fp,
            existing.id,
            existing.status,
            title,
            owner_chat_id,
        )
        return existing, False

    row = repo.insert_pending_event(
        session=session,
        email_id=email_id,
        owner_chat_id=owner_chat_id,
        fingerprint=fp,
        title=title,
        start_iso=start_iso,
        end_iso=end_iso,
        timezone_name=timezone_name,
        event_body_json=json.dumps(event_body, ensure_ascii=False),
    )
    log.info(
        "Pending event proposed: id=%s fingerprint=%s title=%r start=%s",
        row.id,
        fp,
        title,
        start_iso,
    )
    return row, True


def event_body(row: PendingEventRow) -> dict[str, Any]:
    return json.loads(row.event_body_json)
