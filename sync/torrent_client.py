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
import shutil
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# DHT bootstrap routers for peer discovery.
_DHT_ROUTERS = [
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881),
    ("router.utorrent.com", 6881),
    ("dht.libtorrent.org", 25401),
    ("dht.aelitis.com", 6881),
    ("dht.anon.p0ps.org", 6881),
]


class TorrentDownloadError(RuntimeError):
    pass


def _adaptive_stall_timeout(progress: float, base: float = 900.0) -> float:
    """Return a stall timeout that scales up as the download nears completion.

    At 99%+ the remaining pieces may be rare and DHT/peer discovery needs time
    to locate specific seeders.  However 3600s was too aggressive — 15 min is
    enough to decide the torrent is hopeless and fall back to the magnet link.
    """
    if progress >= 0.99:
        return max(base, 900.0)   # 15 min — then try magnet link
    if progress >= 0.95:
        return max(base, 1800.0)  # 30 min — pieces may still be findable
    if progress >= 0.90:
        return max(base, 1200.0)  # 20 min
    return base


class TorrentClient:
    """Downloads one .jsonl.seekable.zst file into `download_dir` at a time."""

    def __init__(self, download_dir: str | Path, *, listen_port: int = 6881):
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
            "listen_interfaces": f"0.0.0.0:{listen_port}",
            "alert_mask": lt.alert.category_t.status_notification,
            "active_downloads": 4,
            "active_seeds": 4,
            "connections_limit": 500,
            "unchoke_slots_limit": 100,
        }
        self._session = lt.session(params)

        # Enable DHT, LSD and PEX for better peer discovery — critical when
        # the remaining pieces are rare (e.g. 99.5% stall on zlib3).
        settings = self._session.get_settings()
        settings["enable_dht"] = True
        settings["enable_lsd"] = True
        settings["enable_natpmp"] = True
        settings["enable_upnp"] = True
        settings["anonymous_mode"] = False
        settings["request_timeout"] = 30
        settings["peer_timeout"] = 60
        settings["torrent_connect_boost"] = 50
        self._session.apply_settings(settings)
        for host, port in _DHT_ROUTERS:
            self._session.add_dht_router(host, port)
        logger.info("libtorrent session initialized (DHT/PEX, %d connections max)", 500)

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
                shutil.copy2(seed_base, target)
                return True
            except OSError:
                return False

    def _download_with_handle(
        self,
        handle,
        release_identifier: str,
        target_path: Path,
        on_progress,
        progress_every_s: float,
    ) -> None:
        """Core download loop with adaptive stall detection and peer re-announce."""
        lt = self._lt
        last_log = 0.0
        last_progress_change = time.monotonic()
        last_total_done = -1
        last_callback = 0.0
        last_reannounce = 0.0
        announce_interval = 300  # re-announce every 5 min while stalled

        while True:
            status = handle.status()
            total_done = int(status.total_done)
            progress = status.progress

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
                last_progress_change = now
            elif checking:
                last_progress_change = now

            if on_progress is not None and now - last_callback >= 1.0:
                last_callback = now
                try:
                    on_progress(total_done, int(status.total_wanted))
                except Exception:  # noqa: BLE001 - progress reporting must never kill downloads
                    pass

            if now - last_log >= progress_every_s:
                last_log = now
                logger.info(
                    "Torrent %s: %.1f%% (peers=%d, seeds=%d, down=%.0f KiB/s)",
                    release_identifier[:60],
                    progress * 100,
                    status.num_peers,
                    status.num_seeds,
                    status.download_payload_rate / 1024,
                )

            if status.is_seeding or progress >= 1.0:
                break

            if handle.is_paused():
                handle.unset_flags(lt.torrent_flags.paused)

            # Adaptive stall timeout: wait longer near completion.
            stall_limit = _adaptive_stall_timeout(progress)
            stalled_s = now - last_progress_change

            if stalled_s > stall_limit:
                self._session.remove_torrent(handle)
                raise TorrentDownloadError(
                    f"Torrent stalled without progress for {stall_limit:.0f}s "
                    f"(progress={progress*100:.1f}%): {release_identifier}"
                )

            # Periodically force re-announce when stalled to find new peers.
            # At 99%+ re-announce every 2 min to discover rare-piece seeders.
            reannounce_gap = 120 if progress >= 0.99 else announce_interval
            if stalled_s > reannounce_gap and now - last_reannounce > reannounce_gap:
                last_reannounce = now
                try:
                    handle.force_reannounce()
                    logger.info(
                        "[%s] Force re-announce (stalled %.0fs, peers=%d, progress=%.1f%%)",
                        release_identifier[:40],
                        stalled_s,
                        status.num_peers,
                        progress * 100,
                    )
                except Exception:  # noqa: BLE001
                    pass

            time.sleep(1.0)

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

        Strategy: try the torrent file first. If that stalls (e.g. at 99.5%),
        remove the handle and retry with the magnet link which uses DHT
        directly and may discover different peers.
        """
        lt = self._lt
        target_path = self.download_dir / release_identifier
        if target_path.exists():
            logger.info("Payload already present, re-checking via libtorrent: %s", target_path)
        elif not self._link_seed_base(seed_base, target_path):
            pass  # genuine full download

        # --- Attempt 1: torrent file ----------------------------------------
        handle = None
        last_error: Exception | None = None

        if torrent_url:
            try:
                torrent_bytes = self._fetch_torrent_bytes(torrent_url)
                info = lt.torrent_info(lt.bdecode(torrent_bytes))
                info_params = {
                    "ti": info,
                    "save_path": str(self.download_dir),
                }
                handle = self._session.add_torrent(info_params)
                handle.set_flags(lt.torrent_flags.auto_managed)
                self._download_with_handle(
                    handle, release_identifier, target_path, on_progress, progress_every_s
                )
                # Success — skip to finalization below.
            except TorrentDownloadError as error:
                last_error = error
                logger.warning(
                    "[%s] Torrent-file download failed: %s", release_identifier[:40], error
                )
                # Remove the stalled handle (keep files on disk for resume).
                try:
                    self._session.remove_torrent(handle)
                except Exception:  # noqa: BLE001
                    pass
                handle = None
            except Exception as error:  # noqa: BLE001
                last_error = error
                logger.warning(
                    "[%s] Torrent-file setup failed: %s", release_identifier[:40], error
                )
                try:
                    self._session.remove_torrent(handle)
                except Exception:  # noqa: BLE001
                    pass
                handle = None

        # --- Attempt 2: magnet link fallback ---------------------------------
        if handle is None and magnet_link:
            logger.info("[%s] Retrying with magnet link…", release_identifier[:40])
            try:
                info_params = {
                    "url": magnet_link,
                    "save_path": str(self.download_dir),
                }
                handle = self._session.add_torrent(info_params)
                handle.set_flags(lt.torrent_flags.auto_managed)
                self._download_with_handle(
                    handle, release_identifier, target_path, on_progress, progress_every_s
                )
            except TorrentDownloadError as error:
                try:
                    self._session.remove_torrent(handle)
                except Exception:  # noqa: BLE001
                    pass
                # If we already had a partial file and this is a retry, the
                # previous error is more informative.
                raise error from last_error
            except Exception as error:  # noqa: BLE001
                try:
                    self._session.remove_torrent(handle)
                except Exception:  # noqa: BLE001
                    pass
                raise TorrentDownloadError(str(error)) from error
        elif handle is None:
            # No magnet link available — raise the original error.
            if last_error is not None:
                raise last_error
            raise TorrentDownloadError("Neither torrent URL nor magnet link provided")

        # --- Finalization ----------------------------------------------------
        # Give libtorrent a moment to flush its write cache to disk before we
        # remove the handle.  Without this, remove_torrent() can drop pieces
        # that haven't been flushed yet — causing a 99.9% "completed" download
        # to suddenly lose data.
        for _wait in range(60):
            if target_path.exists() and target_path.stat().st_size > 0:
                break
            time.sleep(1.0)
        # Flush any remaining write-cache entries (best-effort, non-blocking).
        try:
            self._session.wait_for_alert(-1)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
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
