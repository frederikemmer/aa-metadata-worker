"""Scheduled sync worker: sleeps until SYNC_SCHEDULE (HH:MM, TZ) each day.

The worker never runs a bootstrap implicitly. It only performs incremental
`run_sync` passes; releases already marked completed are skipped.

Dashboard control (DB-driven, cross-container): the worker polls the
sync_commands queue every _COMMAND_POLL_S seconds while waiting:
  - run_now: starts a sync pass immediately
  - pause:   sets paused flag; interrupts an in-flight run gracefully and
             blocks scheduled/manual starts until resumed
  - resume:  clears the paused flag
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from common.config import load_settings

logger = logging.getLogger(__name__)

_COMMAND_POLL_S = 10.0

_stop_event = threading.Event()


def request_stop(*_args) -> None:
    _stop_event.set()
    # Also flag an in-flight import so it can finish its batch and stop cleanly.
    try:
        from sync.importer import request_shutdown

        request_shutdown("worker-stop", None)
    except Exception:  # noqa: BLE001 - never fail on shutdown plumbing
        pass


def reset_stop() -> None:
    _stop_event.clear()


def _seconds_until(schedule_hhmm: str, tz_name: str) -> float:
    try:
        hour, minute = (int(part) for part in schedule_hhmm.split(":", 1))
    except ValueError as error:
        raise ValueError(f"Invalid SYNC_SCHEDULE '{schedule_hhmm}', expected HH:MM") from error
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid SYNC_SCHEDULE '{schedule_hhmm}', expected HH:MM")
    now = datetime.now(ZoneInfo(tz_name))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def next_scheduled_run(schedule_hhmm: str, tz_name: str) -> str:
    """ISO timestamp of the next scheduled run (for API/dashboard display)."""
    seconds = _seconds_until(schedule_hhmm, tz_name)
    return (datetime.now(ZoneInfo(tz_name)) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def wait_until_next_run(seconds: float, poll_interval_s: float = 5.0) -> bool:
    """Wait up to `seconds`; returns True if a stop was requested."""
    waited = 0.0
    while waited < seconds:
        if _stop_event.wait(timeout=min(poll_interval_s, seconds - waited)):
            return True
        waited += poll_interval_s
    return False


class CommandPoller:
    """Reads dashboard control commands + paused flag from PostgreSQL."""

    def __init__(self) -> None:
        self._conn: psycopg.Connection | None = None
        self.run_requested = False
        self.paused = False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:  # pragma: no cover - best effort
                pass
            self._conn = None

    def poll(self, settings) -> None:  # noqa: ANN001 - Settings type avoids cycle
        if self._conn is None:
            from common.db import connect

            self._conn = connect(settings)
        try:
            from sync import state

            for _command_id, command in state.pop_pending_commands(self._conn):
                logger.info("Control command received: %s", command)
                if command == "run_now":
                    self.run_requested = True
                elif command == "pause":
                    state.set_paused(self._conn, True)
                    self.paused = True
                    if not _stop_event.is_set():
                        # Interrupt an in-flight run gracefully (resumable).
                        from sync.importer import request_shutdown

                        request_shutdown("dashboard-pause", None)
                elif command == "resume":
                    state.set_paused(self._conn, False)
                    self.paused = False
            self.paused = state.is_paused(self._conn)
        except psycopg.Error as error:
            logger.warning("Control poll failed (%s); will reconnect.", error)
            self.close()

    def clear_pause_interrupt(self) -> None:
        """Reset the importer shutdown flag after a pause-interrupted run."""
        try:
            from sync.importer import install_signal_handlers

            install_signal_handlers()
        except Exception:  # noqa: BLE001 - never fail on plumbing
            pass


def run_worker_forever() -> None:  # pragma: no cover - long-running loop
    from sync.importer import GracefulShutdown, install_signal_handlers
    from sync.run import run_sync
    from sync.state import SyncLockBusy

    settings = load_settings()
    reset_stop()
    install_signal_handlers()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)

    poller = CommandPoller()
    logger.info(
        "Sync worker starting: collections=%s schedule=%s %s",
        settings.aa_collections,
        settings.sync_schedule,
        settings.tz,
    )

    try:
        while not _stop_event.is_set():
            wait_s = _seconds_until(settings.sync_schedule, settings.tz)
            logger.info("Next sync in %.0f minutes.", wait_s / 60)

            due_in = wait_s
            while due_in > 0 and not _stop_event.is_set():
                if _stop_event.wait(timeout=min(_COMMAND_POLL_S, due_in)):
                    break
                due_in -= min(_COMMAND_POLL_S, due_in)
                poller.poll(settings)
                if poller.run_requested or poller.paused:
                    break

            if _stop_event.is_set():
                logger.info("Worker received shutdown signal; exiting.")
                return

            poller.poll(settings)
            paused_now = poller.paused
            triggered = "Dashboard" if poller.run_requested else "Zeitplan"
            poller.run_requested = False

            if paused_now:
                if triggered == "Dashboard":
                    logger.info("Sync requested via dashboard, but worker is paused; ignoring.")
                else:
                    logger.info("Paused via dashboard; skipping scheduled sync.")
                continue

            if not settings.sync_enabled:
                logger.info("SYNC_ENABLED=false; skipping scheduled sync.")
                continue

            logger.info("Starting sync (%s).", triggered)
            try:
                summary = run_sync(settings=settings)
                logger.info(
                    "Sync finished: processed=%d skipped=%d blocked=%d failed=%d (%.0fs)",
                    len(summary.processed),
                    len(summary.skipped_completed),
                    len(summary.blocked),
                    len(summary.failed),
                    summary.duration_s,
                )
            except SyncLockBusy as error:
                logger.warning("%s", error)
            except GracefulShutdown:
                if _stop_event.is_set():
                    logger.info("Worker shutting down gracefully.")
                    return
                poller.clear_pause_interrupt()
                logger.info("Sync paused via dashboard; release stays resumable.")
            except Exception:  # noqa: BLE001 - worker must survive any failure
                logger.exception("Scheduled sync failed; will retry next schedule.")
    finally:
        poller.close()
        logger.info("Worker stopped.")
