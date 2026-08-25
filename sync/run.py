"""End-to-end sync orchestration for all configured collections.

Flow per collection/release:
  discover -> ensure sync_releases row -> skip completed -> storage guard ->
  download torrent (reusing the previous payload as seed base so only changed
  pieces are transferred) -> stream-import -> validate -> mark completed ->
  keep payload as next seed base -> cleanup.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from common.config import Settings, load_settings
from common.db import apply_migrations, connect
from sync import state
from sync.discovery import fetch_manifest, find_release, latest_releases
from sync.importer import (
    GracefulShutdown,
    delete_payload,
    import_release,
    validate_import,
)
from sync.storage_guard import GIB, evaluate_storage
from sync.torrent_client import TorrentClient

logger = logging.getLogger(__name__)


@dataclass
class SyncRunSummary:
    started_at: float = time.time()
    processed: list[tuple[str, str]] = field(default_factory=list)
    skipped_completed: list[str] = field(default_factory=list)
    blocked: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return time.time() - self.started_at


def _prev_payload_path(work_dir: Path, collection: str) -> Path:
    """Path where the latest successfully imported payload of a collection is kept."""
    prev_dir = work_dir / ".prev"
    prev_dir.mkdir(parents=True, exist_ok=True)
    return prev_dir / f"{collection}.payload"


def _guard_or_block(
    conn: psycopg.Connection,
    work_dir: Path,
    settings: Settings,
    additional_bytes: float,
) -> bool:
    db_size = state.database_size_bytes(conn)
    decision = evaluate_storage(
        str(work_dir),
        db_size,
        additional_bytes,
        settings.storage_warn_gib,
        settings.storage_stop_gib,
    )
    logger.info("%s", decision.message)
    return decision.allowed


def run_sync(
    collections: list[str] | None = None,
    force: bool = False,
    settings: Settings | None = None,
    work_dir: Path | None = None,
    release_overrides: dict[str, str] | None = None,
) -> SyncRunSummary:
    """One incremental sync pass over the configured/newest releases.

    `release_overrides` maps collection -> identifier suffix to pin a specific
    release instead of the newest one (e.g. bootstrapping from an older,
    better-seeded cumulative release; later syncs catch up via the shared
    byte-identical prefix).
    """
    settings = settings or load_settings()
    work_dir = work_dir or Path("/work/sync")
    wanted_collections = collections or settings.aa_collections
    summary = SyncRunSummary()
    client: TorrentClient | None = None  # set early so finally-blocks stay safe

    conn = connect(settings)
    try:
        apply_migrations(conn)
        if not state.acquire_sync_lock(conn):
            raise state.SyncLockBusy("Another sync process holds the advisory lock; refusing parallel run.")
        try:
            manifest = fetch_manifest(settings.aa_mirror_base_url)
            releases = latest_releases(manifest, wanted_collections)
            for override_collection, override_suffix in (release_overrides or {}).items():
                if override_collection not in wanted_collections:
                    continue
                releases[override_collection] = find_release(
                    manifest, override_collection, override_suffix
                )

            missing = set(wanted_collections) - set(releases.keys())
            for collection in sorted(missing):
                logger.warning("No metadata release found for collection '%s'.", collection)

            for collection in wanted_collections:
                release = releases.get(collection)
                if release is None:
                    continue

                release_id = state.ensure_release(
                    conn,
                    collection,
                    release.identifier,
                    release.btih,
                    release.torrent_url,
                    release.data_size_bytes,
                )
                current = state.find_release(conn, release_id)
                assert current is not None

                if not force and current["status"] == "completed":
                    logger.info(
                        "[%s] %s already completed; skipping.",
                        collection,
                        release.identifier[-40:],
                    )
                    summary.skipped_completed.append(collection)
                    continue

                # Storage guard BEFORE download (payload size + 5% slack).
                needed = release.data_size_bytes * 1.05
                if not _guard_or_block(conn, work_dir, settings, needed):
                    state.set_release_status(
                        conn,
                        release_id,
                        "blocked_storage",
                        f"Storage guard blocked download ({needed / GIB:.1f} GiB needed).",
                    )
                    summary.blocked.append((collection, release.identifier))
                    continue

                try:
                    state.set_release_status(conn, release_id, "downloading", reset_counters=True)
                    if client is None:
                        client = TorrentClient(work_dir)

                    seed_base = (
                        _prev_payload_path(work_dir, collection) if settings.sync_reuse_prev_payload else None
                    )

                    def on_progress(done: int, total: int, _rid=release_id) -> None:
                        state.update_download_progress(conn, _rid, done, total)

                    payload_path = client.download(
                        release.identifier,
                        release.torrent_url,
                        release.magnet_link,
                        on_progress=on_progress,
                        seed_base=seed_base,
                    )

                    # Guard again before import: DB will grow while payload exists.
                    payload_bytes = payload_path.stat().st_size
                    db_size_now = state.database_size_bytes(conn)
                    decision = evaluate_storage(
                        str(work_dir),
                        db_size_now,
                        payload_bytes * 1.1,  # DB growth estimate while payload on disk
                        settings.storage_warn_gib,
                        settings.storage_stop_gib,
                    )
                    logger.info("%s", decision.message)

                    stats = import_release(conn, collection, payload_path, release_id, settings)
                    validate_import(conn, release_id, stats)
                    state.set_release_status(conn, release_id, "completed")
                    state.update_download_progress(
                        conn, release_id, payload_path.stat().st_size, payload_path.stat().st_size
                    )

                    if settings.sync_reuse_prev_payload:
                        # Keep as seed base for the next incremental release.
                        os.replace(payload_path, _prev_payload_path(work_dir, collection))
                        logger.info("Payload kept as future seed base (%s).", collection)
                    else:
                        delete_payload(payload_path)

                    discarded_note = f" discarded={stats.discarded}" if stats.discarded else ""
                    logger.info(
                        "[%s] %s done: seen=%d inserted=%d updated=%d skipped=%d%s "
                        "failed=%d in %.0fs; db_size=%.2f GiB",
                        collection,
                        release.identifier[:70],
                        stats.seen,
                        stats.inserted,
                        stats.updated,
                        stats.skipped,
                        discarded_note,
                        stats.failed,
                        time.time() - summary.started_at,
                        state.database_size_bytes(conn) / GIB,
                    )
                    summary.processed.append((collection, release.identifier))
                except GracefulShutdown:
                    state.set_release_status(
                        conn, release_id, "discovered", "Interrupted by shutdown (resumable)."
                    )
                    raise
                except Exception as error:  # noqa: BLE001 - record failure, continue others
                    conn.rollback()
                    state.set_release_status(
                        conn, release_id, "failed", f"{type(error).__name__}: {error}"[:2000]
                    )
                    logger.exception("[%s] Release failed: %s", collection, error)
                    summary.failed.append((collection, f"{error}"))
                    # Remove the temporary payload/link; the previous seed base stays intact.
                    delete_payload(work_dir / release.identifier)
        finally:
            state.release_sync_lock(conn)
            if client is not None:
                client.close()
    finally:
        conn.close()

    return summary
