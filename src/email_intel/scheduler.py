from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]

from email_intel.app import run_cycle
from email_intel.config import get_settings
from email_intel.integrations.telegram_bot import (
    BotRunner,
    build_bot_runner,
    seed_owner_from_env_if_any,
)
from email_intel.logging_setup import setup_logging
from email_intel.runtime import RuntimeContext, build_runtime

log = logging.getLogger(__name__)


def run_forever() -> int:
    settings = get_settings()
    setup_logging(settings)

    runtime = build_runtime(settings)
    seed_owner_from_env_if_any(runtime.session_factory, settings.telegram_chat_id)

    bot: BotRunner | None = None
    try:
        bot = build_bot_runner(
            settings,
            runtime.session_factory,
            runtime.cipher,
            runtime.build_calendar,
        )
        bot.start()
        runtime.bot_runner = bot
    except Exception:
        log.exception("Telegram bot failed to start; continuing without interactive bot")
        bot = None

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        lambda: _safe_cycle(runtime),
        trigger="interval",
        minutes=settings.poll_interval_minutes,
        id="poll_cycle",
        next_run_time=None,
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        log.info("Signal %s received; shutting down", signum)
        scheduler.shutdown(wait=False)
        if bot is not None:
            bot.stop()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    log.info("Scheduler starting: interval=%dm", settings.poll_interval_minutes)
    _safe_cycle(runtime)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if bot is not None:
            bot.stop()

    log.info("Scheduler stopped")
    return 0


def _safe_cycle(runtime: RuntimeContext) -> None:
    try:
        run_cycle(runtime=runtime)
    except Exception:
        log.exception("run_cycle raised; next tick will retry")


if __name__ == "__main__":
    sys.exit(run_forever())
