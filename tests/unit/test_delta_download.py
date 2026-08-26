"""Tests for incremental download reuse (seed base hard-linking)."""

from __future__ import annotations

import os

import pytest

import sync.torrent_client as torrent_client_module
from sync.torrent_client import TorrentClient


class TestSeedBaseLinking:
    def test_hardlink_places_seed_base(self, tmp_path):
        prev = tmp_path / "old.payload"
        prev.write_bytes(b"x" * 1000)
        target = tmp_path / "new.jsonl.seekable.zst"

        placed = TorrentClient._link_seed_base(prev, target)

        assert placed is True
        assert target.exists()
        assert target.stat().st_size == 1000
        # Hard link: same inode, not a copy.
        assert target.stat().st_ino == prev.stat().st_ino

    def test_noop_without_previous(self, tmp_path):
        target = tmp_path / "new.jsonl.seekable.zst"
        assert TorrentClient._link_seed_base(None, target) is False
        assert not target.exists()

    def test_noop_when_target_exists(self, tmp_path):
        prev = tmp_path / "old.payload"
        prev.write_bytes(b"prev")
        target = tmp_path / "new.jsonl.seekable.zst"
        target.write_bytes(b"existing")
        placed = TorrentClient._link_seed_base(prev, target)
        assert placed is False
        assert target.read_bytes() == b"existing"  # untouched

    def test_missing_prev_file_is_noop(self, tmp_path):
        target = tmp_path / "new.jsonl.seekable.zst"
        placed = TorrentClient._link_seed_base(tmp_path / "does-not-exist", target)
        assert placed is False


class TestTorrentFileFetch:
    """The .torrent fetch must survive flaky mirrors (502s) via retries."""

    def test_fetch_retries_until_success(self, monkeypatch):
        attempts = {"n": 0}

        class _Response:
            def __init__(self):
                self.content = b"torrent-bytes"

            def raise_for_status(self):
                pass

        def fake_get(url, timeout=None, follow_redirects=None):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise torrent_client_module.httpx.HTTPStatusError(
                    "Server error '502 Bad Gateway'",
                    request=torrent_client_module.httpx.Request("GET", url),
                    response=torrent_client_module.httpx.Response(502),
                )
            return _Response()

        monkeypatch.setattr(torrent_client_module.httpx, "get", fake_get)
        monkeypatch.setattr(torrent_client_module.time, "sleep", lambda _s: None)

        result = TorrentClient._fetch_torrent_bytes("https://mirror.example/x.torrent")

        assert result == b"torrent-bytes"
        assert attempts["n"] == 3

    def test_fetch_raises_after_exhausted_retries(self, monkeypatch):
        def fake_get(url, **_kwargs):
            raise torrent_client_module.httpx.ConnectError("boom")

        monkeypatch.setattr(torrent_client_module.httpx, "get", fake_get)
        monkeypatch.setattr(torrent_client_module.time, "sleep", lambda _s: None)

        with pytest.raises(RuntimeError, match="Could not fetch torrent file"):
            TorrentClient._fetch_torrent_bytes("https://mirror.example/x.torrent")


class TestPrevPayloadManagement:
    def test_prev_path_is_stable_per_collection(self, tmp_path):
        from sync.run import _prev_payload_path

        path_a1 = _prev_payload_path(tmp_path, "zlib3_records")
        path_a2 = _prev_payload_path(tmp_path, "zlib3_records")
        path_b = _prev_payload_path(tmp_path, "ia2_records")
        assert path_a1 == path_a2
        assert path_a1 != path_b
        assert path_a1.parent.exists()  # created on demand

    def test_replace_keeps_latest_payload_as_seed_base(self, tmp_path):
        """After a successful run the payload becomes the next seed base."""
        from sync.run import _prev_payload_path

        payload = tmp_path / "annas_archive_meta__aacid__zlib3_records__x.jsonl.seekable.zst"
        payload.write_bytes(b"payload-bytes")
        prev = _prev_payload_path(tmp_path, "zlib3_records")

        os.replace(payload, prev)

        assert not payload.exists()
        assert prev.read_bytes() == b"payload-bytes"
