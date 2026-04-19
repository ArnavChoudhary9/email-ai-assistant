from __future__ import annotations

import logging
import signal
import sys
from types import FrameType

from apscheduler.schedulers.blocking import BlockingScheduler  # type: ignore[import-untyped]

from email_intel.app import run_cycle
from email_intel.config import get_settings
from email_intel.logging_setup import setup_logging

log = logging.getLogger(__name__)


def run_forever() -> int:
    settings = get_settings()
    setup_logging(settings)

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        _safe_cycle,
        trigger="interval",
        minutes=settings.poll_interval_minutes,
        id="poll_cycle",
        next_run_time=None,  # run immediately via the explicit call below
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(signum: int, _frame: FrameType | None) -> None:
        log.info("Signal %s received; shutting down scheduler", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    log.info("Scheduler starting: interval=%dm", settings.poll_interval_minutes)
    # Run one cycle immediately so startup isn't silent.
    _safe_cycle()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    log.info("Scheduler stopped")
    return 0


def _safe_cycle() -> None:
    try:
        run_cycle()
    except Exception:
        log.exception("run_cycle raised; next tick will retry")


if __name__ == "__main__":
    sys.exit(run_forever())
