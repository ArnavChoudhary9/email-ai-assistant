"""Interactive Telegram bot: accounts CRUD + scheduling approval.

Runs in a dedicated background thread so it coexists with the sync
APScheduler poller. Outgoing prompts from the pipeline side are bridged into
the bot's asyncio loop via `asyncio.run_coroutine_threadsafe`.

Commands:
  /start         register this chat (first caller becomes owner)
  /help          command list
  /status        recent cycle stats + account health
  /accounts      list configured IMAP accounts
  /add_account   conversation: host -> port -> SSL -> email -> password -> folder
  /remove_account <name>
  /test_account <name>
  /pending       list events awaiting approval
  /cancel        abort the current conversation

Callback queries (from inline keyboards):
  approve:<pending_id>
  reject:<pending_id>
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from email_intel.config import IMAPAccount, Settings
from email_intel.integrations.google_calendar import GoogleCalendarClient
from email_intel.providers.imap import IMAPProvider
from email_intel.security import FernetCipher
from email_intel.storage import repo
from email_intel.storage.db import session_scope
from email_intel.storage.schema import PendingEventRow

log = logging.getLogger(__name__)

# ConversationHandler state ids for /add_account
ADD_HOST, ADD_PORT, ADD_SSL, ADD_EMAIL, ADD_PASSWORD, ADD_FOLDER, ADD_NAME = range(7)


@dataclass
class BotContext:
    """Dependencies threaded through every handler via app.bot_data."""

    settings: Settings
    session_factory: sessionmaker[Session]
    cipher: FernetCipher
    build_calendar: Callable[[], GoogleCalendarClient | None]


def _ctx(context: ContextTypes.DEFAULT_TYPE) -> BotContext:
    return context.application.bot_data["ctx"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


def _caller_chat_id(update: Update) -> str:
    return str(update.effective_chat.id) if update.effective_chat else ""


async def _require_auth(update: Update, ctx: BotContext) -> str | None:
    """Enforce authorization and return the caller's chat_id (string).

    Returns None if the caller isn't authorized; the reply to that effect is
    sent here so callers can just `if not chat_id: return`.
    """
    chat_id = _caller_chat_id(update)
    if not chat_id:
        return None
    with session_scope(ctx.session_factory) as s:
        row = repo.get_bot_user(s, chat_id)
        if row is None or not row.is_authorized:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "This chat isn't authorized. Ask the owner to /authorize "
                    f"{chat_id}, or the owner can send /start first."
                )
            return None
    return chat_id


# ---------------------------------------------------------------------------
# /start — auto chat-id capture
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not update.effective_message:
        return

    chat_id = str(chat.id)
    username = user.username if user else None

    with session_scope(ctx.session_factory) as s:
        row, is_new = repo.upsert_bot_user(
            s, chat_id, username, auto_authorize_if_first=True
        )
        if is_new and row.is_owner:
            msg = (
                "Welcome! You are the owner of this bot.\n"
                f"Chat ID captured: {chat_id}\n\n"
                "Try /help to see what I can do."
            )
        elif is_new:
            msg = (
                f"Registered chat {chat_id}. Awaiting owner approval.\n"
                "The owner can run /authorize to grant access."
            )
        elif row.is_authorized:
            msg = f"Already registered. Chat ID: {chat_id}. /help for commands."
        else:
            msg = "Still awaiting owner approval."

    await update.effective_message.reply_text(msg)


async def cmd_authorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    if not update.effective_message:
        return

    # Only owner can authorize new chats.
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    with session_scope(ctx.session_factory) as s:
        caller = repo.get_bot_user(s, chat_id)
        if caller is None or not caller.is_owner:
            await update.effective_message.reply_text("Only the owner can authorize chats.")
            return

        if not context.args:
            await update.effective_message.reply_text("Usage: /authorize <chat_id>")
            return
        target = context.args[0]
        ok = repo.authorize_bot_user(s, target)

    if ok:
        await update.effective_message.reply_text(f"Authorized chat {target}.")
    else:
        await update.effective_message.reply_text(f"No such chat: {target}.")


# ---------------------------------------------------------------------------
# /help and /status
# ---------------------------------------------------------------------------


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Commands:\n"
        "/accounts — list your email accounts\n"
        "/add_account — add a new IMAP account\n"
        "/remove_account <name>\n"
        "/test_account <name>\n"
        "/pending — your events waiting for approval\n"
        "/status — your health\n"
        "/revoke <chat_id> — (owner only) de-authorize a chat and delete their accounts\n"
        "/cancel — abort the current conversation"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return
    with session_scope(ctx.session_factory) as s:
        accounts = repo.list_accounts(s, enabled_only=False, owner_chat_id=caller)
        pending_rows = repo.list_pending(s, owner_chat_id=caller)
    healthy = sum(1 for a in accounts if a.enabled and not a.last_error)
    await update.effective_message.reply_text(
        f"Your accounts: {healthy}/{len(accounts)} healthy\n"
        f"Your pending events: {len(pending_rows)}\n"
        f"Timezone: {ctx.settings.app_timezone}"
    )


# ---------------------------------------------------------------------------
# /accounts, /remove_account, /test_account
# ---------------------------------------------------------------------------


async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return
    with session_scope(ctx.session_factory) as s:
        accounts = repo.list_accounts(s, enabled_only=False, owner_chat_id=caller)
    if not accounts:
        await update.effective_message.reply_text(
            "No accounts yet. Use /add_account to add one."
        )
        return
    lines = ["Your accounts:"]
    for a in accounts:
        status = "✓" if a.enabled and not a.last_error else ("✗" if a.last_error else "•")
        last_ok = a.last_success_at.isoformat() if a.last_success_at else "never"
        lines.append(f"{status} {a.name} — {a.email}@{a.host} (last ok: {last_ok})")
        if a.last_error:
            lines.append(f"   last error: {a.last_error[:120]}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_remove_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /remove_account <name>")
        return
    name = context.args[0]
    with session_scope(ctx.session_factory) as s:
        ok = repo.delete_account(s, name, owner_chat_id=caller)
    await update.effective_message.reply_text(
        f"Removed account {name}." if ok else f"No such account: {name}."
    )


async def cmd_test_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /test_account <name>")
        return
    name = context.args[0]

    with session_scope(ctx.session_factory) as s:
        row = repo.get_account_by_name(s, name, owner_chat_id=caller)
        if row is None:
            await update.effective_message.reply_text(f"No such account: {name}.")
            return
        try:
            pwd = ctx.cipher.decrypt(row.password_encrypted)
        except Exception as e:
            await update.effective_message.reply_text(f"Password decrypt failed: {e}")
            return
        acc = IMAPAccount(
            name=row.name,
            type="imap",
            host=row.host,
            port=row.port,
            use_ssl=row.use_ssl,
            email=row.email,
            password=SecretStr(pwd),
            folder=row.folder,
            initial_lookback_days=row.initial_lookback_days,
        )

    def _probe() -> str:
        provider = IMAPProvider(acc)
        with provider._connect():
            return "ok"

    try:
        await asyncio.to_thread(_probe)
    except Exception as e:
        with session_scope(ctx.session_factory) as s:
            repo.mark_account_error(s, name, str(e), owner_chat_id=caller)
        await update.effective_message.reply_text(f"Login failed: {e}")
        return

    with session_scope(ctx.session_factory) as s:
        repo.mark_account_success(s, name, owner_chat_id=caller)
    await update.effective_message.reply_text(f"Login to {name} succeeded.")


# ---------------------------------------------------------------------------
# /add_account — ConversationHandler
# ---------------------------------------------------------------------------


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return ConversationHandler.END
    context.user_data.clear()  # type: ignore[union-attr]
    context.user_data["owner_chat_id"] = caller  # type: ignore[index]
    await update.effective_message.reply_text(
        "Adding a new IMAP account. Send /cancel any time.\n\n"
        "IMAP host? (e.g. mailstore.iitd.ac.in or imap.gmail.com)"
    )
    return ADD_HOST


async def add_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADD_HOST
    context.user_data["host"] = update.message.text.strip()  # type: ignore[index]
    await update.message.reply_text("Port? (default 993)")
    return ADD_PORT


async def add_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADD_PORT
    text = update.message.text.strip()
    try:
        port = int(text) if text else 993
    except ValueError:
        await update.message.reply_text("Not a number. Try again.")
        return ADD_PORT
    context.user_data["port"] = port  # type: ignore[index]
    await update.message.reply_text("SSL? (yes/no, default yes)")
    return ADD_SSL


async def add_ssl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADD_SSL
    t = update.message.text.strip().lower()
    context.user_data["use_ssl"] = t not in ("n", "no", "false", "0")  # type: ignore[index]
    await update.message.reply_text("Email / username?")
    return ADD_EMAIL


async def add_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADD_EMAIL
    context.user_data["email"] = update.message.text.strip()  # type: ignore[index]
    await update.message.reply_text(
        "Password? (Stored encrypted. You can delete this message after — "
        "Telegram keeps it in your chat history.)"
    )
    return ADD_PASSWORD


async def add_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADD_PASSWORD
    context.user_data["password"] = update.message.text  # type: ignore[index]
    await update.message.reply_text("Folder? (default INBOX)")
    return ADD_FOLDER


async def add_folder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ADD_FOLDER
    text = update.message.text.strip() or "INBOX"
    context.user_data["folder"] = text  # type: ignore[index]
    await update.message.reply_text("Short name for this account? (e.g. personal, work)")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ctx = _ctx(context)
    if not update.message or not update.message.text:
        return ADD_NAME
    name = update.message.text.strip()
    data = context.user_data  # type: ignore[assignment]
    if not data:
        return ConversationHandler.END
    owner = data.get("owner_chat_id")
    if not owner:
        await update.message.reply_text("Session expired. Start over with /add_account.")
        return ConversationHandler.END

    with session_scope(ctx.session_factory) as s:
        if repo.get_account_by_name(s, name, owner_chat_id=owner) is not None:
            await update.message.reply_text(
                f"You already have an account named {name!r}. Pick a different name."
            )
            return ADD_NAME
        repo.insert_account(
            s,
            name=name,
            owner_chat_id=owner,
            host=data["host"],
            port=data["port"],
            use_ssl=data["use_ssl"],
            email=data["email"],
            password_encrypted=ctx.cipher.encrypt(data["password"]),
            folder=data["folder"],
        )

    await update.message.reply_text(
        f"Saved account {name}. Try /test_account {name} to verify login."
    )
    context.user_data.clear()  # type: ignore[union-attr]
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_message:
        await update.effective_message.reply_text("Cancelled.")
    context.user_data.clear()  # type: ignore[union-attr]
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /pending and callback queries
# ---------------------------------------------------------------------------


def _keyboard_for_pending(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{pending_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{pending_id}"),
            ]
        ]
    )


def _format_prompt(row: PendingEventRow) -> str:
    return (
        "Schedule this event?\n\n"
        f"Title: {row.title}\n"
        f"Start: {row.start_iso}\n"
        f"End:   {row.end_iso}\n"
        f"Timezone: {row.timezone_name}"
    )


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    caller = await _require_auth(update, ctx)
    if not caller or not update.effective_message:
        return
    with session_scope(ctx.session_factory) as s:
        rows = repo.list_pending(s, owner_chat_id=caller)
        snapshot = [
            {
                "id": r.id,
                "title": r.title,
                "start_iso": r.start_iso,
                "end_iso": r.end_iso,
                "timezone_name": r.timezone_name,
            }
            for r in rows
        ]

    if not snapshot:
        await update.effective_message.reply_text("No pending events.")
        return

    for item in snapshot:
        kb = _keyboard_for_pending(item["id"])
        text = (
            "Schedule this event?\n\n"
            f"Title: {item['title']}\n"
            f"Start: {item['start_iso']}\n"
            f"End:   {item['end_iso']}\n"
            f"Timezone: {item['timezone_name']}"
        )
        await update.effective_message.reply_text(text, reply_markup=kb)


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: de-authorize a chat and hard-delete all their accounts."""
    ctx = _ctx(context)
    if not update.effective_message:
        return
    chat_id = _caller_chat_id(update)
    if not chat_id:
        return

    with session_scope(ctx.session_factory) as s:
        caller = repo.get_bot_user(s, chat_id)
        if caller is None or not caller.is_owner:
            await update.effective_message.reply_text("Only the owner can revoke chats.")
            return
        if not context.args:
            await update.effective_message.reply_text("Usage: /revoke <chat_id>")
            return
        target = context.args[0]
        if target == chat_id:
            await update.effective_message.reply_text(
                "Refusing to revoke yourself — you're the owner."
            )
            return
        target_row = repo.get_bot_user(s, target)
        if target_row is None:
            await update.effective_message.reply_text(f"No such chat: {target}.")
            return
        deleted = repo.delete_accounts_for_chat(s, target)
        target_row.is_authorized = False

    await update.effective_message.reply_text(
        f"Revoked chat {target}. Deleted {deleted} account(s)."
    )
    try:
        await context.bot.send_message(
            chat_id=target,
            text="Your access has been revoked and your accounts were deleted.",
        )
    except Exception:
        log.debug("Couldn't notify revoked chat %s (it may have blocked the bot)", target)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx = _ctx(context)
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()

    try:
        action, pid_str = query.data.split(":", 1)
        pending_id = int(pid_str)
    except ValueError:
        return

    # Ownership check: only the user who owns this pending event can decide.
    caller_chat_id = (
        str(update.effective_chat.id) if update.effective_chat else ""
    )
    with session_scope(ctx.session_factory) as s:
        row = repo.get_pending(s, pending_id)
        if row is None:
            await query.edit_message_text("This prompt is stale — pending event not found.")
            return
        if row.owner_chat_id and row.owner_chat_id != caller_chat_id:
            # Silent refusal via toast — don't reveal another user's data.
            await query.answer("Not your prompt.", show_alert=True)
            return

    if action == "approve":
        await _approve(ctx, query, pending_id)
    elif action == "reject":
        await _reject(ctx, query, pending_id)


async def _reject(ctx: BotContext, query: Any, pending_id: int) -> None:
    with session_scope(ctx.session_factory) as s:
        repo.mark_pending_status(s, pending_id, "rejected")
    await query.edit_message_text(f"{query.message.text}\n\n❌ Rejected.")


async def _approve(ctx: BotContext, query: Any, pending_id: int) -> None:
    """Queue the GCal insert. A worker picks it up on the next scheduler tick."""
    with session_scope(ctx.session_factory) as s:
        row = repo.get_pending(s, pending_id)
        if row is None:
            await query.edit_message_text("This prompt is stale — pending event not found.")
            return
        if row.status != "pending":
            await query.edit_message_text(
                f"Already {row.status}. No action taken."
            )
            return
        # Mark approved and enqueue. The worker owns the GCal insert, so the
        # bot thread stays responsive and transient errors retry.
        row.status = "approved"
        repo.enqueue_event_job(
            s, pending_event_id=pending_id, owner_chat_id=row.owner_chat_id
        )

    await query.edit_message_text(
        f"{query.message.text}\n\n⏳ Queued — creating in the background."
    )


# ---------------------------------------------------------------------------
# Application builder + thread runner
# ---------------------------------------------------------------------------


def _build_application(bot_ctx: BotContext) -> Application:
    token = bot_ctx.settings.telegram_bot_token.get_secret_value()
    app = ApplicationBuilder().token(token).build()
    app.bot_data["ctx"] = bot_ctx

    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add_account", add_start)],
        states={
            ADD_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_host)],
            ADD_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_port)],
            ADD_SSL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_ssl)],
            ADD_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_email)],
            ADD_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_password)],
            ADD_FOLDER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_folder)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
        },
        fallbacks=[CommandHandler("cancel", add_cancel)],
        name="add_account",
        persistent=False,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("authorize", cmd_authorize))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("remove_account", cmd_remove_account))
    app.add_handler(CommandHandler("test_account", cmd_test_account))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(add_conv)
    app.add_handler(CallbackQueryHandler(on_callback))
    return app


class BotRunner:
    """Runs a python-telegram-bot Application in a dedicated thread + loop.

    The public API (start/stop/send_pending_prompt) is safe to call from the
    sync pipeline thread. Outgoing prompts are bridged into the bot's event
    loop via asyncio.run_coroutine_threadsafe.
    """

    def __init__(self, bot_ctx: BotContext) -> None:
        self._bot_ctx = bot_ctx
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: Application | None = None
        self._ready = threading.Event()
        self._stop_signal: asyncio.Event | None = None

    def start(self, *, ready_timeout: float = 30.0) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=ready_timeout):
            raise RuntimeError("Telegram bot failed to start in time")
        log.info("Telegram bot thread is up")

    def stop(self, *, timeout: float = 10.0) -> None:
        if self._loop is None or self._stop_signal is None:
            return
        self._loop.call_soon_threadsafe(self._stop_signal.set)
        if self._thread:
            self._thread.join(timeout=timeout)

    def send_pending_prompt(
        self,
        chat_id: str,
        row: PendingEventRow,
        *,
        timeout: float = 30.0,
    ) -> str | None:
        """Send a pending-event prompt to `chat_id`. Returns the sent message_id.

        Called from the sync pipeline thread. Blocks on the bot's event loop.
        """
        if self._loop is None or self._app is None:
            return None
        text = _format_prompt(row)
        kb = _keyboard_for_pending(row.id)
        coro = self._app.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            msg = fut.result(timeout=timeout)
        except Exception:
            log.exception("Failed to send pending prompt to %s", chat_id)
            return None
        return str(msg.message_id)

    def edit_pending_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        *,
        timeout: float = 30.0,
    ) -> bool:
        """Edit a previously-sent prompt from the worker thread.

        Used by drain_events to update "⏳ Queued…" to "✅ Created …" once the
        GCal insert succeeds. Best-effort — returns False if we can't reach
        the message (deleted, too old, bot blocked).
        """
        if self._loop is None or self._app is None:
            return False
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return False
        coro = self._app.bot.edit_message_text(
            chat_id=chat_id, message_id=mid, text=text
        )
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            fut.result(timeout=timeout)
        except Exception:
            log.debug("Couldn't edit message %s in chat %s", message_id, chat_id)
            return False
        return True

    # internal --------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._async_main())
        except Exception:
            log.exception("Telegram bot thread crashed")
        finally:
            if self._loop is not None:
                try:
                    self._loop.close()
                except Exception:
                    pass
            self._ready.set()  # unblock start() caller even on failure

    async def _async_main(self) -> None:
        self._app = _build_application(self._bot_ctx)
        self._stop_signal = asyncio.Event()
        await self._app.initialize()
        await self._app.start()
        assert self._app.updater is not None
        await self._app.updater.start_polling(drop_pending_updates=True)
        self._ready.set()
        try:
            await self._stop_signal.wait()
        finally:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception:
                log.exception("Error during bot shutdown")


def build_bot_runner(
    settings: Settings,
    session_factory: sessionmaker[Session],
    cipher: FernetCipher,
    build_calendar: Any,
) -> BotRunner:
    ctx = BotContext(
        settings=settings,
        session_factory=session_factory,
        cipher=cipher,
        build_calendar=build_calendar,
    )
    return BotRunner(ctx)


def seed_owner_from_env_if_any(
    session_factory: sessionmaker[Session],
    telegram_chat_id: str,
) -> None:
    """If TELEGRAM_CHAT_ID is set and bot_users is empty, seed it as owner.

    Keeps backward compatibility: existing users with .env-configured chat_id
    don't need to /start the bot before it can send outgoing notifications.
    """
    if not telegram_chat_id:
        return
    with session_scope(session_factory) as s:
        if repo.count_bot_users(s) > 0:
            return
        existing = repo.get_bot_user(s, telegram_chat_id)
        if existing is not None:
            return
        s.add_all([])  # no-op, just to touch session
        from email_intel.storage.schema import BotUserRow

        s.add(
            BotUserRow(
                chat_id=telegram_chat_id,
                telegram_username=None,
                is_authorized=True,
                is_owner=True,
            )
        )
        log.info("Seeded owner chat_id=%s from TELEGRAM_CHAT_ID env", telegram_chat_id)


__all__ = [
    "BotContext",
    "BotRunner",
    "build_bot_runner",
    "seed_owner_from_env_if_any",
]
