"""Sync tests: discovery, state machine, locking, storage guard, run_sync flows."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from common.config import load_settings
from sync import state
from sync.discovery import find_release, latest_releases, parse_release
from sync.run import run_sync
from sync.storage_guard import evaluate_storage
from tests.conftest import make_zst

_RELEASE_IDENTIFIER = (
    "annas_archive_meta__aacid__zlib3_records__20240101T000000Z--20250101T000000Z.jsonl.seekable.zst"
)


def _entry(name: str, **overrides):
    base = {
        "url": f"https://mirror.example/{name}",
        "display_name": name,
        "is_metadata": True,
        "obsolete": False,
        "btih": f"btih-{hash(name) & 0xFFFF}",
        "magnet_link": f"magnet:?dn={name}",
        "data_size": 1_000_000,
        "embargo": True,
    }
    base.update(overrides)
    return base


class TestDiscovery:
    def test_parse_release(self):
        info = parse_release(
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent"
            )
        )
        assert info is not None
        assert info.collection == "zlib3_records"
        assert info.identifier.endswith("jsonl.seekable.zst")
        assert info.data_size_bytes == 1_000_000

    def test_obsolete_and_non_metadata_filtered(self):
        obsolete = _entry(
            "annas_archive_meta__aacid__zlib3_records__"
            "20240101T000000Z--20240201T000000Z.jsonl.seekable.zst.torrent",
            obsolete=True,
        )
        assert parse_release(obsolete) is None
        data_torrent = _entry(
            "annas_archive_data__aacid__upload_files_x__20240404T231822Z--20260404T231823Z.torrent",
            is_metadata=False,
        )
        assert parse_release(data_torrent) is None

    def test_latest_release_selected(self):
        manifest = [
            _entry(
                "annas_archive_meta__aacid__ia2_records__20240126T065114Z--20251011T032821Z.jsonl.seekable.zst.torrent"
            ),
            _entry(
                "annas_archive_meta__aacid__ia2_records__20240126T065114Z--20260626T041035Z.jsonl.seekable.zst.torrent"
            ),
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent"
            ),
        ]
        best = latest_releases(manifest, ["ia2_records", "zlib3_records"])
        assert best["ia2_records"].identifier.endswith("--20260626T041035Z.jsonl.seekable.zst")
        assert set(best.keys()) == {"ia2_records", "zlib3_records"}

    def test_find_release_by_suffix(self):
        manifest = [
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260706T193143Z.jsonl.seekable.zst.torrent"
            ),
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent"
            ),
        ]
        found = find_release(manifest, "zlib3_records", "--20260706T193143Z.jsonl.seekable.zst")
        assert found.identifier.endswith("20240809T171652Z--20260706T193143Z.jsonl.seekable.zst")

    def test_find_release_allows_obsolete_pin(self):
        """Explicit overrides may pin obsolete releases (better-seeded escape hatch)."""
        manifest = [
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260706T193143Z.jsonl.seekable.zst.torrent",
                obsolete=True,
            ),
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent"
            ),
        ]
        found = find_release(manifest, "zlib3_records", "--20260706T193143Z.jsonl.seekable.zst")
        assert found.identifier.endswith("--20260706T193143Z.jsonl.seekable.zst")
        # latest_releases stays strict: obsolete entries never win automatically.
        best = latest_releases(manifest, ["zlib3_records"])
        assert best["zlib3_records"].identifier.endswith("--20260821T041731Z.jsonl.seekable.zst")

    def test_find_release_rejects_unknown_and_ambiguous(self):
        manifest = [
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260706T193143Z.jsonl.seekable.zst.torrent"
            ),
            _entry(
                "annas_archive_meta__aacid__zlib3_records__20240809T171652Z--20260821T041731Z.jsonl.seekable.zst.torrent"
            ),
        ]
        with pytest.raises(ValueError, match="No release found"):
            find_release(manifest, "zlib3_records", "--19990101T000000Z")
        # Shared base prefix matches both -> ambiguous.
        with pytest.raises(ValueError, match="Ambiguous"):
            find_release(manifest, "zlib3_records", "jsonl.seekable.zst")


class TestStorageGuard:
    def test_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sync.storage_guard._disk_free_bytes", lambda p: 500 * 1024**3)
        decision = evaluate_storage(
            str(tmp_path),
            db_size_bytes=10 * 1024**3,
            additional_bytes_needed=20 * 1024**3,
            warn_gib=300,
            stop_gib=400,
        )
        assert decision.allowed and decision.level == "ok"

    def test_warn(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sync.storage_guard._disk_free_bytes", lambda p: 500 * 1024**3)
        decision = evaluate_storage(str(tmp_path), 350 * 1024**3, 10 * 1024**3, 300, 400)
        assert decision.allowed and decision.level == "warn"

    def test_stop_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sync.storage_guard._disk_free_bytes", lambda p: 500 * 1024**3)
        decision = evaluate_storage(str(tmp_path), 395 * 1024**3, 10 * 1024**3, 300, 400)
        assert not decision.allowed and decision.level == "stop"

    def test_insufficient_free_disk_blocks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sync.storage_guard._disk_free_bytes", lambda p: 1024**3)
        decision = evaluate_storage(str(tmp_path), 10 * 1024**3, 50 * 1024**3, 300, 400)
        assert not decision.allowed


class TestStateAndLocking:
    def test_ensure_release_idempotent(self, db_conn):
        id1 = state.ensure_release(db_conn, "zlib3_records", "rel-a", "btih1", "url", 100)
        id2 = state.ensure_release(db_conn, "zlib3_records", "rel-a", "btih1b", "url2", 100)
        assert id1 == id2
        count = db_conn.execute("SELECT COUNT(*) FROM sync_releases").fetchone()[0]
        assert count == 1

    def test_status_transitions(self, db_conn):
        rid = state.ensure_release(db_conn, "ia2_records", "rel-b", None, None, None)
        state.set_release_status(db_conn, rid, "downloading", reset_counters=True)
        state.update_release_counters(db_conn, rid, seen=10, inserted=7)
        state.update_release_counters(db_conn, rid, seen=5, inserted=4)
        row = db_conn.execute(
            "SELECT status, records_seen, records_inserted FROM sync_releases WHERE id=%s", (rid,)
        ).fetchone()
        assert row[0] == "downloading" and row[1] == 15 and row[2] == 11
        state.set_release_status(db_conn, rid, "importing")
        import_started = db_conn.execute(
            "SELECT import_started_at FROM sync_releases WHERE id=%s", (rid,)
        ).fetchone()[0]
        assert import_started is not None
        state.set_release_status(db_conn, rid, "completed")
        assert state.completed_release_identifiers(db_conn, "ia2_records") == {"rel-b"}

    def test_parallel_sync_blocked_by_advisory_lock(self, db_conn, postgres_docker):
        assert state.acquire_sync_lock(db_conn)
        conn2 = psycopg.connect(postgres_docker, autocommit=True)
        try:
            assert not state.acquire_sync_lock(conn2)
        finally:
            state.release_sync_lock(db_conn)
            conn2.close()
        # After release, another connection may take it.
        conn2 = psycopg.connect(postgres_docker, autocommit=True)
        try:
            assert state.acquire_sync_lock(conn2)
            state.release_sync_lock(conn2)
        finally:
            conn2.close()


class FakeTorrentClient:
    """Stands in for libtorrent: materializes a small payload immediately."""

    payloads: dict[str, list[str]] = {}

    def __init__(self, download_dir, **_kwargs):
        self.download_dir = Path(download_dir)

    def download(self, release_identifier, torrent_url, magnet_link="", on_progress=None, seed_base=None):
        lines = self.payloads[release_identifier]
        target = self.download_dir / release_identifier
        make_zst(lines, target.with_suffix(""))  # writes .zst path
        # make_zst appended .zst already via suffix param; rename if needed:
        final = self.download_dir / release_identifier
        if not final.exists() and target.with_suffix("").exists():
            target.with_suffix("").rename(final)
        return final

    def close(self):
        pass


class TestRunSyncFlows:
    def _fake_manifest(self, monkeypatch):
        names = [
            "annas_archive_meta__aacid__zlib3_records__20240101T000000Z--20250101T000000Z.jsonl.seekable.zst.torrent",
        ]
        manifest = [_entry(n, data_size=1000, url=f"https://mirror.example/{n}") for n in names]
        monkeypatch.setattr("sync.run.fetch_manifest", lambda base_url: manifest)
        monkeypatch.setattr("sync.run.TorrentClient", FakeTorrentClient)

    def test_discover_import_complete_then_skip(self, db_conn, tmp_path, monkeypatch):
        from sync.importer import iter_jsonl  # noqa: F401 - ensure module import works

        lines = [
            '{"aacid":"aacid__zlib3_records__20250101T000000Z__9__Z","metadata":'
            '{"md5_reported":"' + "1" * 32 + '","title":"Fake Book","extension":"epub"}}'
        ]
        FakeTorrentClient.payloads = {
            _RELEASE_IDENTIFIER: lines,
        }
        self._fake_manifest(monkeypatch)
        settings = load_settings()

        summary1 = run_sync(collections=["zlib3_records"], force=False, settings=settings, work_dir=tmp_path)
        assert summary1.processed and summary1.failed == []
        total = db_conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()[0]
        assert total == 1
        # payload deleted after success
        assert not list(tmp_path.glob("*.zst"))

        # Second run: release known -> skipped, no re-import.
        summary2 = run_sync(collections=["zlib3_records"], force=False, settings=settings, work_dir=tmp_path)
        assert summary2.skipped_completed == ["zlib3_records"]
        assert summary2.processed == []

    def test_release_override_pins_specific_release(self, db_conn, tmp_path, monkeypatch):
        names = [
            "annas_archive_meta__aacid__zlib3_records__20240101T000000Z--20250101T000000Z.jsonl.seekable.zst.torrent",
            "annas_archive_meta__aacid__zlib3_records__20240101T000000Z--20250201T000000Z.jsonl.seekable.zst.torrent",
        ]
        manifest = [_entry(n, data_size=1000, url=f"https://mirror.example/{n}") for n in names]
        monkeypatch.setattr("sync.run.fetch_manifest", lambda base_url: manifest)
        monkeypatch.setattr("sync.run.TorrentClient", FakeTorrentClient)
        lines = [
            '{"aacid":"aacid__zlib3_records__20250101T000000Z__9__Z","metadata":'
            '{"md5_reported":"' + "1" * 32 + '","title":"Older Book","extension":"epub"}}'
        ]
        # Only the pinned (older) release has a payload; if the newest were
        # chosen instead, the fake client would raise KeyError and fail the run.
        FakeTorrentClient.payloads = {_RELEASE_IDENTIFIER: lines}
        settings = load_settings()

        summary = run_sync(
            collections=["zlib3_records"],
            force=False,
            settings=settings,
            work_dir=tmp_path,
            release_overrides={"zlib3_records": "--20250101T000000Z.jsonl.seekable.zst"},
        )
        assert summary.processed == [("zlib3_records", _RELEASE_IDENTIFIER)]
        assert summary.failed == []
        total = db_conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()[0]
        assert total == 1

    def test_release_override_unknown_suffix_fails_fast(self, db_conn, tmp_path, monkeypatch):
        self._fake_manifest(monkeypatch)
        settings = load_settings()
        with pytest.raises(ValueError, match="No release found"):
            run_sync(
                collections=["zlib3_records"],
                force=False,
                settings=settings,
                work_dir=tmp_path,
                release_overrides={"zlib3_records": "--19990101T000000Z"},
            )

    def test_failed_import_marks_failed_and_retry_works(self, db_conn, tmp_path, monkeypatch):
        lines = ['{"broken at all'] * 300 + [
            '{"aacid":"aacid__zlib3_records__20250101T000000Z__9__Z","metadata":'
            '{"md5_reported":"' + "2" * 32 + '","title":"Retry Book"}}'
        ]
        identifier = (
            "annas_archive_meta__aacid__zlib3_records__20240101T000000Z--20250101T000000Z.jsonl.seekable.zst"
        )
        FakeTorrentClient.payloads = {identifier: lines}
        self._fake_manifest(monkeypatch)
        settings = load_settings()
        strict = __import__("dataclasses").replace(settings, sync_error_abort_rate=0.01)

        summary = run_sync(collections=["zlib3_records"], force=False, settings=strict, work_dir=tmp_path)
        assert summary.failed
        row = db_conn.execute("SELECT status FROM sync_releases").fetchone()
        assert row[0] == "failed"

        # Fix the payload (retry scenario): now valid content only.
        FakeTorrentClient.payloads = {identifier: lines[-1:]}
        summary2 = run_sync(collections=["zlib3_records"], force=False, settings=settings, work_dir=tmp_path)
        assert summary2.processed
        row = db_conn.execute("SELECT status FROM sync_releases").fetchone()
        assert row[0] == "completed"

    def test_parallel_run_refused(self, db_conn, tmp_path, monkeypatch):
        self._fake_manifest(monkeypatch)
        assert state.acquire_sync_lock(db_conn)
        try:
            with pytest.raises(state.SyncLockBusy):
                run_sync(
                    collections=["zlib3_records"], force=False, settings=load_settings(), work_dir=tmp_path
                )
        finally:
            state.release_sync_lock(db_conn)

    def test_storage_block_marks_blocked_storage(self, db_conn, tmp_path, monkeypatch):
        self._fake_manifest(monkeypatch)
        settings = load_settings()
        tight = __import__("dataclasses").replace(settings, storage_warn_gib=0, storage_stop_gib=0)
        # stop threshold 0 -> any projected size blocked
        summary = run_sync(collections=["zlib3_records"], force=False, settings=tight, work_dir=tmp_path)
        assert summary.blocked
        row = db_conn.execute("SELECT status FROM sync_releases").fetchone()
        assert row[0] == "blocked_storage"
