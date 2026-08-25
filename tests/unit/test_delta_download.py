"""Tests for incremental download reuse (seed base hard-linking)."""

from __future__ import annotations

import os

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
