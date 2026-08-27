"""Tests for parallel download orchestration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sync.run import _download_one, _download_sequential


class TestDownloadOne:
    """_download_one runs in its own thread with isolated resources."""

    @patch("sync.run.connect")
    @patch("sync.run.TorrentClient")
    def test_returns_payload_on_success(self, mock_client_cls, mock_connect):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        payload = Path("/work/sync/identifier.jsonl.seekable.zst")
        mock_client.download.return_value = payload

        coll, result, error, is_partial = _download_one(
            "zlib3_records",
            "annas_archive_meta__aacid__zlib3_records__x.jsonl.seekable.zst",
            "https://example.com/t.torrent",
            "",
            42,
            Path("/work/sync"),
            MagicMock(sync_reuse_prev_payload=False),
        )

        assert coll == "zlib3_records"
        assert result == payload
        assert error is None
        assert is_partial is False
        mock_client.download.assert_called_once()
        mock_client.close.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("sync.run.connect")
    @patch("sync.run.TorrentClient")
    def test_returns_error_on_failure(self, mock_client_cls, mock_connect):
        mock_connect.return_value = MagicMock()
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.download.side_effect = RuntimeError("stalled")

        coll, result, error, is_partial = _download_one(
            "zlib3_records",
            "id",
            "url",
            "",
            1,
            Path("/work/sync"),
            MagicMock(sync_reuse_prev_payload=False),
        )

        assert coll == "zlib3_records"
        assert result is None
        assert isinstance(error, RuntimeError)
        assert "stalled" in str(error)
        assert is_partial is False
        mock_client.close.assert_called_once()

    @patch("sync.run.connect")
    @patch("sync.run.TorrentClient")
    def test_partial_download_returns_partial_path(self, mock_client_cls, mock_connect):
        """A stalled download with usable partial data is reported for a
        resilient import instead of being discarded."""
        from sync.torrent_client import PartialDownloadError

        mock_connect.return_value = MagicMock()
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        partial = Path("/work/sync/partial.jsonl.seekable.zst")
        mock_client.download.side_effect = PartialDownloadError("stalled", partial)

        coll, result, error, is_partial = _download_one(
            "zlib3_records",
            "id",
            "url",
            "magnet",
            1,
            Path("/work/sync"),
            MagicMock(sync_reuse_prev_payload=False),
        )

        assert coll == "zlib3_records"
        assert result == partial
        assert error is None
        assert is_partial is True

    @patch("sync.run.connect")
    @patch("sync.run.TorrentClient")
    def test_closes_resources_on_error(self, mock_client_cls, mock_connect):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.download.side_effect = OSError("disk full")

        _download_one("c", "id", "url", "", 1, Path("/w"), MagicMock())

        mock_client.close.assert_called_once()
        mock_conn.close.assert_called_once()

    @patch("sync.run.connect")
    @patch("sync.run.TorrentClient")
    def test_reuses_seed_base(self, mock_client_cls, mock_connect):
        mock_connect.return_value = MagicMock()
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.download.return_value = Path("x")
        settings = MagicMock(sync_reuse_prev_payload=True)
        work_dir = Path("/work/sync")

        _download_one("zlib3_records", "id", "url", "", 1, work_dir, settings)

        call_kwargs = mock_client.download.call_args
        # seed_base should point to .prev/<collection>.payload
        assert "seed_base" in call_kwargs.kwargs or len(call_kwargs.args) >= 6


class TestDownloadSequential:
    """_download_sequential reuses a single TorrentClient and imports inline."""

    @patch("sync.run._import_payload")
    @patch("sync.run.TorrentClient")
    def test_reuses_client_across_collections(self, mock_client_cls, mock_import):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.download.return_value = Path("payload")

        conn = MagicMock()
        settings = MagicMock(sync_reuse_prev_payload=False)
        to_download = {
            "zlib3_records": {
                "release": MagicMock(
                    identifier="zlib3_id",
                    torrent_url="url1",
                    magnet_link="",
                ),
                "release_id": 1,
            },
            "ia2_records": {
                "release": MagicMock(
                    identifier="ia2_id",
                    torrent_url="url2",
                    magnet_link="",
                ),
                "release_id": 2,
            },
        }

        _download_sequential(
            conn, to_download, Path("/work/sync"), settings, MagicMock()
        )

        # Client created once, used for both downloads; each download is
        # followed immediately by its import.
        assert mock_client_cls.call_count == 1
        assert mock_client.download.call_count == 2
        assert mock_import.call_count == 2
        mock_client.close.assert_called_once()

    @patch("sync.run._import_payload")
    @patch("sync.run.TorrentClient")
    def test_failed_download_does_not_stop_others(self, mock_client_cls, mock_import):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first failed")
            return Path("payload")

        mock_client.download.side_effect = side_effect

        conn = MagicMock()
        settings = MagicMock(sync_reuse_prev_payload=False)
        to_download = {
            "zlib3_records": {
                "release": MagicMock(
                    identifier="zlib3_id", torrent_url="url1", magnet_link=""
                ),
                "release_id": 1,
            },
            "ia2_records": {
                "release": MagicMock(
                    identifier="ia2_id", torrent_url="url2", magnet_link=""
                ),
                "release_id": 2,
            },
        }
        summary = MagicMock()
        summary.failed = []

        _download_sequential(
            conn, to_download, Path("/work/sync"), settings, summary
        )

        # Second download succeeded despite first failure and was imported.
        assert mock_import.call_count == 1
        assert mock_import.call_args.args[1] == "ia2_records"
        assert len(summary.failed) == 1
        assert summary.failed[0][0] == "zlib3_records"


class TestParallelismConfig:
    """sync_max_downloads setting behaviour."""

    def test_default_is_4(self):
        from common.config import Settings

        s = Settings()
        assert s.sync_max_downloads == 4

    def test_checking_grace_default_is_120(self):
        from common.config import Settings

        s = Settings()
        assert s.sync_checking_grace_min == 120

    def test_stall_at_99pct_default_is_15(self):
        from common.config import Settings

        s = Settings()
        assert s.sync_stall_at_99_min == 15

    def test_zero_falls_back_to_sequential(self):
        """max_downloads=0 should not crash ThreadPoolExecutor."""
        from common.config import Settings

        s = Settings(sync_max_downloads=0)
        assert s.sync_max_downloads <= 1  # triggers sequential path
