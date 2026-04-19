from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Importance(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    NORMAL = "normal"
    IGNORE = "ignore"


class ReplyPriority(StrEnum):
    NONE = ""
    NORMAL = "normal"
    URGENT = "urgent"


class Attachment(BaseModel):
    filename: str
    mime_type: str
    size_bytes: int


class Email(BaseModel):
    """Normalized representation of a fetched email. Providers emit these."""

    provider: str
    account_name: str
    message_id: str
    sender: str
    subject: str
    received_at: datetime
    body_text: str
    body_html: str | None = None
    folder: str = "INBOX"
    labels: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    attachments: list[Attachment] = Field(default_factory=list)
    raw_hash: str


class Classification(BaseModel):
    """Output of the cheap heuristic gate (PRD §7)."""

    importance_guess: Importance
    should_call_llm: bool
    matched_keywords: list[str] = Field(default_factory=list)
    reason: str = ""


class Meeting(BaseModel):
    exists: bool = False
    date: str = ""
    time: str = ""
    location: str = ""


class CalendarEvent(BaseModel):
    title: str
    start: str = ""
    end: str = ""
    description: str = ""


class Extraction(BaseModel):
    """Structured LLM output per PRD §3.3."""

    summary: str = ""
    importance: Importance = Importance.NORMAL
    action_required: bool = False
    deadline: str = ""
    meeting: Meeting = Field(default_factory=Meeting)
    tasks: list[str] = Field(default_factory=list)
    calendar_events: list[CalendarEvent] = Field(default_factory=list)
    reply_needed: bool = False
    reply_priority: ReplyPriority = ReplyPriority.NONE
