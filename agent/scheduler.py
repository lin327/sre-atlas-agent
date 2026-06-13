"""Periodic collection scheduler for SRE Atlas Agent.

Wraps the ``schedule`` library to run a full collect-dedup-generate cycle
at a configurable interval (default 6 hours).
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from typing import Callable

import schedule

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_INTERVAL_HOURS: int = 6


class SchedulerError(Exception):
    """Raised when the scheduler encounters an unrecoverable error."""


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class CollectionScheduler:
    """Run a collection callback on a fixed interval.

    Parameters
    ----------
    collection_fn : Callable[[], None]
        The function invoked each cycle.  It is expected to perform the
        full collect → dedup → generate → output pipeline.
    interval_hours : int
        How often to run the cycle.
    """

    def __init__(
        self,
        collection_fn: Callable[[], None],
        interval_hours: int = DEFAULT_INTERVAL_HOURS,
    ) -> None:
        if interval_hours < 1:
            raise SchedulerError("Interval must be at least 1 hour.")
        self._collection_fn = collection_fn
        self._interval_hours = interval_hours
        self._running = False

    # ------------------------------------------------------------------
    # Single cycle
    # ------------------------------------------------------------------

    def run_collection_cycle(self) -> None:
        """Execute one full collection cycle with logging and timing."""
        start = datetime.now(timezone.utc)
        logger.info("=== Collection cycle started at %s ===", start.isoformat())

        try:
            self._collection_fn()
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            logger.info(
                "=== Collection cycle finished in %.1fs ===", elapsed
            )
        except Exception:
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            logger.exception(
                "=== Collection cycle FAILED after %.1fs ===", elapsed
            )
            # Do not re-raise — the scheduler keeps running.

    # ------------------------------------------------------------------
    # Continuous mode
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the periodic scheduler.  Blocks until interrupted."""
        logger.info(
            "Scheduler started — running every %d hour(s).",
            self._interval_hours,
        )

        # Run immediately on startup, then schedule recurring.
        self.run_collection_cycle()
        schedule.every(self._interval_hours).hours.do(self.run_collection_cycle)

        self._running = True

        # Graceful shutdown on SIGINT / SIGTERM.
        def _stop(signum: int, frame) -> None:  # noqa: ANN001
            logger.info("Received signal %s — shutting down.", signum)
            self._running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        while self._running:
            schedule.run_pending()
            time.sleep(30)

        logger.info("Scheduler stopped.")

    def stop(self) -> None:
        """Request a graceful stop from another thread."""
        self._running = False


# ---------------------------------------------------------------------------
# Standalone test entry-point
# ---------------------------------------------------------------------------


def _demo_cycle() -> None:
    """Placeholder cycle used when running the module directly."""
    logger.info("[demo] Collection cycle would run here.")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    logger.info("Running scheduler in standalone demo mode.")
    sched = CollectionScheduler(collection_fn=_demo_cycle, interval_hours=1)
    sched.start()
