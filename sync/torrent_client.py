"""BitTorrent download of AAC metadata files using an embedded libtorrent client.

libtorrent is imported lazily so environments without the native library
(e.g. unit-test runs on macOS) can still import everything else.

Incremental updates ("nur das Neue herunterladen"): AAC cumulative releases
share a byte-identical compressed prefix with their predecessor (t2sz blocks
are deterministic and records are append-only). Before adding the new torrent,
the previous payload is hard-linked under the new filename; libtorrent's hash
check then verifies every piece, keeps everything that matches the shared
prefix and downloads only the remaining pieces. Correctness never depends on
this optimization: mismatching pieces are simply re-downloaded.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class TorrentDownloadError(RuntimeError):
    pass


class TorrentClient:
    """Downloads one .jsonl.seekable.zst file into `download_dir` at a time."""

    def __init__(self, download_dir: str | Path):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        try:
            import libtorrent as lt  # noqa: PLC0415 - lazy by design
        except ImportError as error:  # pragma: no cover - platform dependent
            raise RuntimeError(
                "libtorrent is not available; install the 'libtorrent' package "
                "or use an image with it baked in (see Dockerfile)."
            ) from error
        self._lt = lt
        params: dict = {
            "listen_interfaces": "0.0.0.0:6881",
            "alert_mask": lt.alert.category_t.status_notification,
            "active_downloads": 2,
            "active_seeds": 2,
            "connections_limit": 200,
        }
        self._session = lt.session(params)

        # Enable DHT, LSD and PEX for better peer discovery — critical when
        # the remaining pieces are rare (e.g.99.45% stall on zlib3).
        settings = self._session.get_settings()
        settings["enable_dht"] = True
        settings["enable_lsd"] = True
        settings["enable_natpmp"] = True
        settings["enable_upnp"] = True
        settings["anonymous_mode"] = False
        self._session.apply_settings(settings)
        self._session.add_dht_router("router.bittorrent.com", 6881)
        self._session.add_dht_router("dht.transmissionbt.com", 6881)
        self._session.add_dht_router("router.utorrent.com", 6881)
        self._session.add_dht_router("dht.libtorrent.org", 25401)
        logger.info("libtorrent session initialized with DHT/PEX enabled")

    @staticmethod
    def _fetch_torrent_bytes(torrent_url: str, timeout_s: float = 60.0, retries: int = 3) -> bytes:
        """Fetch the .torrent file with retries — mirrors are flaky (502s)."""
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = httpx.get(torrent_url, timeout=timeout_s, follow_redirects=True)
                response.raise_for_status()
                return response.content
            except Exception as error:  # noqa: BLE001 - retry any transport/HTTP failure
                last_error = error
                logger.warning("Torrent file fetch failed (attempt %d/%d): %s", attempt, retries, error)
                time.sleep(min(2**attempt, 30))
        raise RuntimeError(f"Could not fetch torrent file from {torrent_url}: {last_error}")

    @staticmethod
    def _link_seed_base(seed_base: Path | None, target: Path) -> bool:
        """Hard-link the previous payload under the new filename (zero copy).

        Falls back to a real copy if hard links are unsupported. Returns whether
        a seed base was placed.
        """
        if seed_base is None or not seed_base.exists() or target.exists():
            return False
        try:
            os.link(seed_base, target)
            logger.info(
                "Seeding new download from previous payload (%s) - only changed pieces will be downloaded.",
                seed_base.name[:50],
            )
            return True
        except OSError as error:
            logger.warning("Hard link failed (%s); falling back to full download.", error)
            try:
                import shutil

                shutil.copy2(seed_base, target)
                return True
            except OSError:
                return False

    def download(
        self,
        release_identifier: str,
        torrent_url: str,
        magnet_link: str = "",
        progress_every_s: float = 30.0,
        on_progress=None,
        seed_base: Path | None = None,
        stall_timeout_s: float = 900.0,
    ) -> Path:
        """Download the release payload; returns the local file path.

        `on_progress(done_bytes, total_bytes)` is invoked roughly once per second
        so callers can persist live progress.
        """
        lt = self._lt
        target_path = self.download_dir / release_identifier
        if target_path.exists():
            logger.info("Payload already present, re-checking via libtorrent: %s", target_path)
        elif not self._link_seed_base(seed_base, target_path):
            pass  # genuine full download

        info_params = {}
        if torrent_url:
            try:
                torrent_bytes = self._fetch_torrent_bytes(torrent_url)
                info_params["ti"] = lt.torrent_info(lt.bdecode(torrent_bytes))
            except Exception as error:  # noqa: BLE001 - fall back to magnet below
                if not magnet_link:
                    raise TorrentDownloadError(
                        f"Could not fetch torrent file {torrent_url} and no magnet link available"
                    ) from error
                logger.warning("Torrent URL failed after retries (%s); falling back to magnet link.", error)
                info_params["url"] = magnet_link
        elif magnet_link:
            info_params["url"] = magnet_link
        else:
            raise TorrentDownloadError("Neither torrent URL nor magnet link provided")

        info_params["save_path"] = str(self.download_dir)
        handle = self._session.add_torrent(info_params)
        handle.set_flags(lt.torrent_flags.auto_managed)

        last_log = 0.0
        last_progress_change = time.monotonic()
        last_total_done = -1
        last_callback = 0.0

        while True:
            status = handle.status()
            total_done = int(status.total_done)

            now = time.monotonic()
            # While checking existing data, verification progress counts as activity.
            checking = (
                str(status.state)
                in (
                    "CheckingFiles",
                    "CheckingResumeData",
                )
                or "checking" in str(status.state).lower()
            )

            if total_done != last_total_done:
                last_total_done = total_done
                last_progress_change = time.monotonic()
            elif checking:
                last_progress_change = time.monotonic()

            if on_progress is not None and now - last_callback >= 1.0:
                last_callback = now
                try:
                    on_progress(total_done, int(status.total_wanted))
                except Exception:  # noqa: BLE001 - progress reporting must never kill downloads
                    pass

            if now - last_log >= progress_every_s:
                last_log = now
                logger.info(
                    "Torrent %s: %.1f%% (peers=%d, down=%.0f KiB/s)",
                    release_identifier[:60],
                    status.progress * 100,
                    status.num_peers,
                    status.download_payload_rate / 1024,
                )

            if status.is_seeding or status.progress >= 1.0:
                break

            if handle.is_paused():
                handle.unset_flags(lt.torrent_flags.paused)

            if time.monotonic() - last_progress_change > stall_timeout_s:
                # Default remove flags keep the files on disk (libtorrent 2.x
                # dropped the old delete_files keyword argument).
                self._session.remove_torrent(handle)
                raise TorrentDownloadError(
                    f"Torrent stalled without progress for {stall_timeout_s:.0f}s: {release_identifier}"
                )
            time.sleep(1.0)

        # Give libtorrent a moment to finalize/rename files.
        for _ in range(30):
            if target_path.exists() and target_path.stat().st_size > 0:
                break
            time.sleep(1.0)
        # Keep the payload on disk (default remove flags); caller decides
        # whether it becomes the next seed base or is deleted.
        self._session.remove_torrent(handle)

        if not target_path.exists():
            raise TorrentDownloadError(f"Download finished but payload not found at {target_path}")
        logger.info("Download complete: %s (%d bytes)", target_path, target_path.stat().st_size)
        return target_path

    def close(self) -> None:
        self._session.pause()
        # libtorrent 2.x removed session.abort(); graceful shutdown relies on
        # pause() + garbage collection of the session object.
        abort = getattr(self._session, "abort", None)
        if callable(abort):
            abort()
