"""End-to-end sync orchestration for all configured collections.

Flow:
  1. Discover releases, skip completed, check storage guard.
  2. Download torrents (optionally parallel via SYNC_MAX_DOWNLOADS).
  3. Stream-import each completed payload sequentially.

Parallel downloads speed up the sync when collections have few seeders –
the bottleneck is typically peer availability, not local bandwidth.  Each
download thread owns its own libtorrent session and DB connection.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from sync.torrent_client import PartialDownloadError, TorrentClient

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


# -- Parallel download worker ------------------------------------------------


def _download_one(
    collection: str,
    release_identifier: str,
    torrent_url: str,
    magnet_link: str,
    release_id: int,
    work_dir: Path,
    settings: Settings,
    listen_port: int = 6881,
    stall_at_99_s: int | None = None,
) -> tuple[str, Path | None, Exception | None, bool]:
    """Download a single collection in its own thread.

    Returns (collection, payload_path, error, is_partial).  `is_partial` is
    True when the download stalled but partial payload data exists and should
    be imported resiliently.  Each thread creates its own TorrentClient
    (libtorrent session) and DB connection for thread safety.
    """
    conn: psycopg.Connection | None = None
    client: TorrentClient | None = None
    try:
        conn = connect(settings)
        client = TorrentClient(
            work_dir,
            listen_port=listen_port,
            checking_grace_s=settings.sync_checking_grace_min * 60,
            stall_at_99_s=(
                stall_at_99_s
                if stall_at_99_s is not None
                else settings.sync_stall_at_99_min * 60
            ),
        )

        seed_base = (
            _prev_payload_path(work_dir, collection)
            if settings.sync_reuse_prev_payload
            else None
        )

        def on_progress(done: int, total: int, _rid: int = release_id) -> None:
            state.update_download_progress(conn, _rid, done, total)

        payload_path = client.download(
            release_identifier,
            torrent_url,
            magnet_link,
            on_progress=on_progress,
            seed_base=seed_base,
        )
        return collection, payload_path, None, False
    except PartialDownloadError as error:  # stalled but data on disk
        return collection, error.path, None, True
    except Exception as error:  # noqa: BLE001 - catch everything for worker
        return collection, None, error, False
    finally:
        if client is not None:
            client.close()
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass


# -- Import (shared by sequential and parallel paths) -------------------------


def _import_payload(
    conn: psycopg.Connection,
    collection: str,
    payload_path: Path,
    info: dict,
    work_dir: Path,
    settings: Settings,
    summary: SyncRunSummary,
    resilient: bool = False,
) -> None:
    """Import one downloaded payload and finalize its release row.

    Called as soon as an individual download finishes so that finished
    collections do not wait for slower/stalled torrents.

    `resilient` is set when the payload is a partial download (some pieces
    missing/corrupt).  In that case the importer skips the broken frames and
    imports every decompressible record instead of failing the whole release.
    """
    release_id = info["release_id"]
    release = info["release"]
    try:
        # Guard before import: DB will grow while payload exists.
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

        stats = import_release(
            conn, collection, payload_path, release_id, settings, resilient=resilient
        )
        validate_import(conn, release_id, stats)
        status_note = " (partial — some pieces missing)" if resilient else ""
        state.set_release_status(
            conn, release_id, "completed", f"imported{status_note}"
        )
        state.record_imported_release(conn, collection, release.identifier)
        state.update_download_progress(
            conn, release_id, payload_path.stat().st_size, payload_path.stat().st_size
        )

        if settings.sync_reuse_prev_payload:
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
        logger.exception("[%s] Import failed: %s", collection, error)
        summary.failed.append((collection, f"{error}"))
        delete_payload(work_dir / release.identifier)


# -- Sequential download (original behaviour) --------------------------------


def _download_sequential(
    conn: psycopg.Connection,
    to_download: dict[str, dict],
    work_dir: Path,
    settings: Settings,
    summary: SyncRunSummary,
    stall_at_99_s: int | None = None,
) -> None:
    """Download collections one-by-one, importing each right after it finishes."""
    client: TorrentClient | None = None
    try:
        for collection, info in to_download.items():
            release_id = info["release_id"]
            release = info["release"]
            try:
                state.set_release_status(conn, release_id, "downloading", reset_counters=True)
                if client is None:
                    client = TorrentClient(
                        work_dir,
                        checking_grace_s=settings.sync_checking_grace_min * 60,
                        stall_at_99_s=(
                            stall_at_99_s
                            if stall_at_99_s is not None
                            else settings.sync_stall_at_99_min * 60
                        ),
                    )

                seed_base = (
                    _prev_payload_path(work_dir, collection)
                    if settings.sync_reuse_prev_payload
                    else None
                )

                def on_progress(done: int, total: int, _rid: int = release_id) -> None:
                    state.update_download_progress(conn, _rid, done, total)

                payload_path = client.download(
                    release.identifier,
                    release.torrent_url,
                    release.magnet_link,
                    on_progress=on_progress,
                    seed_base=seed_base,
                )
                _import_payload(
                    conn, collection, payload_path, info, work_dir, settings, summary
                )
            except PartialDownloadError as error:
                logger.warning(
                    "[%s] Partial download; importing available data resiliently.",
                    collection,
                )
                _import_payload(
                    conn,
                    collection,
                    error.path,
                    info,
                    work_dir,
                    settings,
                    summary,
                    resilient=True,
                )
            except GracefulShutdown:
                raise
            except Exception as error:  # noqa: BLE001 - record failure, continue others
                conn.rollback()
                state.set_release_status(
                    conn, release_id, "failed", f"{type(error).__name__}: {error}"[:2000]
                )
                logger.exception("[%s] Release failed: %s", collection, error)
                summary.failed.append((collection, f"{error}"))
                delete_payload(work_dir / release.identifier)
    finally:
        if client is not None:
            client.close()


# -- Main orchestration -------------------------------------------------------


def run_sync(
    collections: list[str] | None = None,
    force: bool = False,
    settings: Settings | None = None,
    work_dir: Path | None = None,
    release_overrides: dict[str, str] | None = None,
) -> SyncRunSummary:
    """One incremental sync pass over the configured/newest releases.

    When ``sync_max_downloads > 1`` collections are downloaded in parallel
    (each in its own libtorrent session and DB connection).  Import is always
    sequential to keep DB write throughput predictable.

    `release_overrides` maps collection -> identifier suffix to pin a specific
    release instead of the newest one (e.g. bootstrapping from an older,
    better-seeded cumulative release; later syncs catch up via the shared
    byte-identical prefix).
    """
    settings = settings or load_settings()
    work_dir = work_dir or Path("/work/sync")
    wanted_collections = collections or settings.aa_collections
    summary = SyncRunSummary()

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

            # -- Phase 1: Discovery, skip completed, storage guard -----------

            to_download: dict[str, dict] = {}
            modes = state.get_collection_modes(conn)

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

                state.set_release_status(conn, release_id, "downloading", reset_counters=True)
                to_download[collection] = {
                    "release": release,
                    "release_id": release_id,
                }

            # -- Phase 2: Download (parallel or sequential) -------------------
            # Each finished download is imported immediately, so completed
            # collections never wait for slower or stalled torrents.

            max_dl = settings.sync_max_downloads

            # Effective >=99% stall timeout per collection: collections in
            # 'import' mode fall back to a resilient import much sooner so a
            # stuck download does not block freshness for minutes on end.
            import_stall = settings.sync_import_stall_min * 60

            def _stall_for(collection: str) -> int | None:
                mode = modes.get(collection)
                if mode is not None and mode.mode == "import":
                    return import_stall
                return None

            any_import_mode = any(
                modes.get(c) is not None and modes[c].mode == "import"
                for c in to_download
            )
            all_stall_s = import_stall if any_import_mode else None

            if max_dl > 1 and len(to_download) > 1:
                logger.info(
                    "Downloading %d collections in parallel (max_workers=%d).",
                    len(to_download),
                    max_dl,
                )
                futures = {}
                with ThreadPoolExecutor(max_workers=max_dl) as pool:
                    for idx, (collection, info) in enumerate(to_download.items()):
                        release = info["release"]
                        future = pool.submit(
                            _download_one,
                            collection,
                            release.identifier,
                            release.torrent_url,
                            release.magnet_link,
                            info["release_id"],
                            work_dir,
                            settings,
                            listen_port=6881 + idx,
                            stall_at_99_s=_stall_for(collection),
                        )
                        futures[future] = collection

                    for future in as_completed(futures):
                        collection = futures[future]
                        release_id = to_download[collection]["release_id"]
                        _coll, payload, error, is_partial = future.result()
                        if error is not None:
                            conn.rollback()
                            state.set_release_status(
                                conn,
                                release_id,
                                "failed",
                                f"{type(error).__name__}: {error}"[:2000],
                            )
                            logger.exception("[%s] Download failed: %s", collection, error)
                            summary.failed.append((collection, f"{error}"))
                            delete_payload(work_dir / to_download[collection]["release"].identifier)
                        else:
                            logger.info(
                                "[%s] Download %s; starting import.",
                                collection,
                                "partial (resilient)" if is_partial else "complete",
                            )
                            _import_payload(
                                conn,
                                collection,
                                payload,
                                to_download[collection],
                                work_dir,
                                settings,
                                summary,
                                resilient=is_partial,
                            )
            else:
                logger.info("Downloading %d collections sequentially.", len(to_download))
                _download_sequential(
                    conn, to_download, work_dir, settings, summary, stall_at_99_s=all_stall_s
                )
        finally:
            state.release_sync_lock(conn)
    finally:
        conn.close()

    return summary
