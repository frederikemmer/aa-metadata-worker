"""Background, import-free analysis of retained upload_records payloads."""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import psycopg

from common.config import Settings, load_settings
from common.db import apply_migrations, connect
from sync import state
from sync.importer import iter_jsonl
from sync.sources.uploads import subcollection_of

logger = logging.getLogger(__name__)
_stop_event = threading.Event()


def request_stop() -> None:
    _stop_event.set()


def reset_stop() -> None:
    _stop_event.clear()


def analyze_payload(
    payload_path: Path,
    filter_names: set[str],
    on_progress=None,
) -> tuple[dict[str, int], int]:
    """Count matching raw records without invoking adapters or DB imports."""
    counts = {name: 0 for name in filter_names}
    scanned = 0
    latest_bytes = 0
    last_report = 0.0

    def report(done_bytes: int) -> None:
        nonlocal latest_bytes, last_report
        latest_bytes = done_bytes
        now = time.monotonic()
        if on_progress is not None and now - last_report >= 5.0:
            last_report = now
            on_progress(done_bytes, scanned)

    for line in iter_jsonl(payload_path, on_bytes=report):
        if _stop_event.is_set():
            raise InterruptedError("Filter analysis stopped during shutdown.")
        scanned += 1
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = subcollection_of(raw.get("aacid"))
        if name in counts:
            counts[name] += 1

    if on_progress is not None:
        on_progress(max(latest_bytes, payload_path.stat().st_size), scanned)
    return counts, scanned


def process_filter_analysis_job(
    conn: psycopg.Connection, job: dict, work_dir: Path
) -> None:
    job_id = int(job["id"])
    if job["collection"] != "upload_records":
        raise ValueError("Only upload_records supports subcollection analysis.")
    payload_path = work_dir / ".prev" / "upload_records.payload"
    if not payload_path.is_file():
        raise FileNotFoundError("The retained upload_records payload is no longer available.")
    mode = state.get_collection_mode(conn, "upload_records")
    if mode.last_imported_identifier != job["release_identifier"]:
        raise RuntimeError("The retained payload belongs to a different release.")

    snapshot = {
        str(name): bool(blocked)
        for name, blocked in dict(job["filters_snapshot"]).items()
    }
    logger.info(
        "Filter analysis job %d starting for %s (%d filters).",
        job_id,
        job["release_identifier"],
        len(snapshot),
    )

    def on_progress(done_bytes: int, records_scanned: int) -> None:
        state.update_filter_analysis_progress(
            conn, job_id, done_bytes, records_scanned
        )

    counts, scanned = analyze_payload(
        payload_path, set(snapshot), on_progress=on_progress
    )
    state.complete_filter_analysis(
        conn,
        job_id,
        int(job["release_id"]),
        snapshot,
        counts,
        payload_path.stat().st_size,
        scanned,
    )
    logger.info("Filter analysis job %d completed: scanned=%d.", job_id, scanned)


def run_filter_analysis_worker_forever(
    settings: Settings | None = None,
    work_dir: Path | None = None,
) -> None:  # pragma: no cover - long-running loop
    settings = settings or load_settings()
    work_dir = work_dir or Path("/work/sync")
    reset_stop()
    conn = connect(settings)
    try:
        apply_migrations(conn)
        conn.execute(
            "UPDATE filter_analysis_jobs SET status = 'pending' WHERE status = 'running'"
        )
        while not _stop_event.is_set():
            job = state.claim_filter_analysis_job(conn)
            if job is None:
                _stop_event.wait(5.0)
                continue
            try:
                process_filter_analysis_job(conn, job, work_dir)
            except InterruptedError:
                conn.execute(
                    "UPDATE filter_analysis_jobs SET status = 'pending' WHERE id = %s",
                    (job["id"],),
                )
                return
            except Exception as error:  # noqa: BLE001 - persist job failure
                logger.exception("Filter analysis job %s failed: %s", job["id"], error)
                state.fail_filter_analysis(
                    conn, int(job["id"]), f"{type(error).__name__}: {error}"
                )
    finally:
        conn.close()
