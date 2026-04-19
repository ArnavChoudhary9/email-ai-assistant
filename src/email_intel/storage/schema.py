from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    tasks: Mapped[list["TaskRow"]] = relationship(back_populates="email", cascade="all,delete")
    notifications: Mapped[list["NotificationRow"]] = relationship(
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    email: Mapped[EmailRow] = relationship(back_populates="notifications")


class CalendarEventRow(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("emails.id", ondelete="CASCADE"))
    google_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
