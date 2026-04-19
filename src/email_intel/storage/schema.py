from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class EmailRow(Base):
    __tablename__ = "emails"
    __table_args__ = (UniqueConstraint("provider", "message_id", name="uq_provider_msgid"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    account_name: Mapped[str] = mapped_column(String(64))
    message_id: Mapped[str] = mapped_column(String(512), index=True)
    sender: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(1024))
    received_at: Mapped[datetime] = mapped_column(DateTime)
    raw_hash: Mapped[str] = mapped_column(String(64), index=True)

    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    importance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    tasks: Mapped[list[TaskRow]] = relationship(back_populates="email", cascade="all,delete")
    notifications: Mapped[list[NotificationRow]] = relationship(
        back_populates="email", cascade="all,delete"
    )


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(1024))
    due_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")

    email: Mapped[EmailRow] = relationship(back_populates="tasks")


class NotificationRow(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    telegram_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    slack_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    email: Mapped[EmailRow] = relationship(back_populates="notifications")


class CalendarEventRow(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    google_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class AccountRow(Base):
    """Email account managed by the Telegram bot. Passwords stored encrypted."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(16), default="imap")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=993)
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[str] = mapped_column(String(255))
    password_encrypted: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(String(64), default="INBOX")
    initial_lookback_days: Mapped[int] = mapped_column(Integer, default=3)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class BotUserRow(Base):
    """Authorized Telegram chats. First /start becomes owner automatically."""

    __tablename__ = "bot_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(32), unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_authorized: Mapped[bool] = mapped_column(Boolean, default=False)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PendingEventRow(Base):
    """Proposed calendar event waiting for user approval via Telegram.

    Cross-email dedup is via `fingerprint` (hash of normalized title + start date).
    Repeated reminder emails about the same event collapse into one pending row.
    """

    __tablename__ = "pending_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512))
    start_iso: Mapped[str] = mapped_column(String(64))
    end_iso: Mapped[str] = mapped_column(String(64))
    timezone_name: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    event_body_json: Mapped[str] = mapped_column(Text)
    # pending | approved | rejected | created | expired | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    prompt_chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    google_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
