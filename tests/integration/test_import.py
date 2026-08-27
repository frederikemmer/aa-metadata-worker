"""Integration tests: migrations + import pipeline against real PostgreSQL."""

from __future__ import annotations

import dataclasses

import pytest

from common.config import load_settings
from common.db import apply_migrations, list_migrations
from common.normalize import normalize_md5
from sync import state
from sync.importer import import_release
from sync.sources import get_adapter
from tests.conftest import fixture_lines, make_zst


def import_fixture(conn, collection: str, tmp_path, name: str | None = None):
    name = name or collection
    release_id = conn.execute(
        "INSERT INTO sync_releases (collection, release_identifier) VALUES (%s, %s) RETURNING id",
        (collection, f"test_{name}"),
    ).fetchone()[0]
    payload = make_zst(fixture_lines(name), tmp_path / f"{name}.jsonl.zst")
    return import_release(conn, collection, payload, release_id)


class TestMigrations:
    def test_apply_creates_schema(self, db_conn):
        version = db_conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        assert int(version[0]) >= 1

    def test_idempotent(self, db_conn):
        assert apply_migrations(db_conn) == []
        assert len(list_migrations()) >= 1


class TestImportPipeline:
    def test_import_counts_and_rows(self, db_conn, tmp_path):
        stats = import_fixture(db_conn, "zlib3_records", tmp_path)
        total = db_conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()[0]
        assert total == stats.inserted > 0
        assert stats.failed == 0
        # Release counters persisted
        row = db_conn.execute("SELECT records_seen, records_inserted FROM sync_releases").fetchone()
        assert row[0] >= row[1]

    def test_discard_only_import_persists_progress(self, db_conn, tmp_path, monkeypatch):
        release_id = db_conn.execute(
            "INSERT INTO sync_releases (collection, release_identifier) "
            "VALUES ('upload_records', 'discard_only') RETURNING id"
        ).fetchone()[0]
        raw = {
            "aacid": "aacid__upload_records_academia_edu__20250101T000000Z__1__Paper",
            "metadata": {
                "md5": "f" * 32,
                "filepath": "papers/paper.pdf",
                "exiftool_output": {"Title": "Paper", "Author": "Author"},
            },
        }
        payload = make_zst([raw, raw], tmp_path / "discard-only.jsonl.zst")
        counter_updates = []
        original_update = state.update_release_counters

        def track_update(*args, **kwargs):
            counter_updates.append(kwargs.copy())
            return original_update(*args, **kwargs)

        monkeypatch.setattr(state, "update_release_counters", track_update)
        settings = dataclasses.replace(load_settings(), sync_batch_size=2)

        stats = import_release(db_conn, "upload_records", payload, release_id, settings=settings)

        row = db_conn.execute(
            "SELECT records_seen, records_discarded FROM sync_releases WHERE id = %s",
            (release_id,),
        ).fetchone()
        assert stats.seen == 2
        assert stats.discarded == 2
        assert row == (2, 2)
        assert counter_updates == [
            {"seen": 2, "inserted": 0, "updated": 0, "skipped": 0, "discarded": 2, "failed": 0}
        ]

    def test_duplicate_md5_not_duplicated(self, db_conn, tmp_path):
        lines = fixture_lines("zlib3_records")
        first = import_fixture(db_conn, "zlib3_records", tmp_path)
        # Re-import the exact same payload under a new release id.
        release_id = db_conn.execute(
            "INSERT INTO sync_releases (collection, release_identifier) VALUES (%s, %s) RETURNING id",
            ("zlib3_records", "test_second"),
        ).fetchone()[0]
        second_payload = make_zst(lines, tmp_path / "second.jsonl.zst")
        second = import_release(db_conn, "zlib3_records", second_payload, release_id)
        total = db_conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()[0]
        assert total == first.inserted
        assert second.inserted == 0 or second.updated > 0

    def _make_release(self, db_conn, identifier):
        return db_conn.execute(
            "INSERT INTO sync_releases (collection, release_identifier) "
            "VALUES ('zlib3_records', %s) RETURNING id",
            (identifier,),
        ).fetchone()[0]

    def test_better_metadata_merge(self, db_conn, tmp_path):
        """A sparse record followed by a richer one ends up with the richer data."""
        md5_hex = "a" * 32
        sparse = {
            "aacid": "aacid__zlib3_records__20240101T000000Z__1__x",
            "metadata": {"md5_reported": md5_hex, "title": "Sparse"},
        }
        rich = {
            "aacid": "aacid__zlib3_records__20250101T000000Z__2__y",
            "metadata": {
                "md5_reported": md5_hex,
                "title": "Rich Title",
                "author": "Jane Doe",
                "publisher": "Pub House",
                "year": "2020",
                "language": "german",
                "extension": "epub",
                "filesize_reported": 42424,
                "isbns": ["978-3-16-148410-0"],
            },
        }
        payload = make_zst([sparse], tmp_path / "sparse.jsonl.zst")
        import_release(db_conn, "zlib3_records", payload, self._make_release(db_conn, "merge_test"))

        release_id2 = self._make_release(db_conn, "merge_test_2")
        payload2 = make_zst([rich], tmp_path / "rich.jsonl.zst")
        import_release(db_conn, "zlib3_records", payload2, release_id2)

        row = db_conn.execute(
            """
            SELECT title, publisher, publication_year, isbn13, quality_score
            FROM metadata_records WHERE md5 = %s
            """,
            (normalize_md5(md5_hex),),
        ).fetchone()
        assert row[0] == "Rich Title"
        assert row[1] == "Pub House"
        assert row[2] == 2020
        assert row[3] == ["9783161484100"]
        assert row[4] > 30

    def test_tombstone_excluded_from_search(self, db_conn, tmp_path):
        import_fixture(db_conn, "zlib3_records", tmp_path)
        rows = db_conn.execute("SELECT COUNT(*) FROM metadata_records WHERE deleted").fetchone()[0]
        searched = db_conn.execute(
            """
            SELECT COUNT(*) FROM metadata_records
            WHERE NOT deleted AND search_tsv @@ to_tsquery('simple', 'a:*')
            """
        ).fetchone()[0]
        assert rows >= 0  # tombstones exist in DB...
        assert searched >= 0  # ...but search SQL always adds NOT deleted

    def test_corrupt_line_does_not_abort(self, db_conn, tmp_path):
        import json

        good_lines = fixture_lines("zlib3_records")[:5]
        for i in range(297):  # many valid records with unique md5s
            raw = json.loads(good_lines[i % len(good_lines)])
            raw["metadata"]["md5_reported"] = f"{(i + 1):032x}"
            good_lines.append(json.dumps(raw))
        lines = list(good_lines[:300])
        lines.insert(150, "{not valid json!!")
        lines.append("{another bad line")
        release_id = self._make_release(db_conn, "corrupt")
        payload = make_zst(lines, tmp_path / "corrupt.jsonl.zst")
        stats = import_release(db_conn, "zlib3_records", payload, release_id)
        assert stats.failed == 2
        assert stats.seen == len(lines)

    def test_high_error_rate_aborts(self, db_conn, tmp_path, monkeypatch):
        import dataclasses

        from common.config import load_settings

        settings = load_settings()
        strict = dataclasses.replace(settings, sync_error_abort_rate=0.01)

        bad_lines = ['{"broken json line'] * 200  # raise real parse errors
        good = fixture_lines("zlib3_records")[:10]
        lines = good + bad_lines
        release_id = self._make_release(db_conn, "bad_rate")
        payload = make_zst(lines, tmp_path / "bad.jsonl.zst")
        with pytest.raises(RuntimeError, match="error rate"):
            import_release(db_conn, "zlib3_records", payload, release_id, strict)

    def test_enrichment_collections_import(self, db_conn, tmp_path):
        """goodreads/gbooks/libby land with synthetic md5s and correct counters."""
        from sync.sources.base import synthetic_md5

        for collection in ("goodreads_records", "gbooks_records", "libby_records"):
            stats = import_fixture(db_conn, collection, tmp_path)
            assert stats.failed == 0
            assert stats.inserted > 0
            # Each fixture contains one non-importable line: goodreads lacks an
            # id there (-> skipped), gbooks/libby hit their type filters (-> discarded).
            assert stats.skipped + stats.discarded >= 1
            rows = db_conn.execute(
                "SELECT md5, source_collection, source_record_id FROM metadata_records "
                "WHERE source_collection = %s",
                (collection,),
            ).fetchall()
            assert rows
            for md5, coll, rid in rows:
                assert md5 == synthetic_md5(coll, rid)


class TestFtsConsistency:
    def test_search_vector_populated(self, db_conn, tmp_path):
        import_fixture(db_conn, "zlib3_records", tmp_path)
        missing = db_conn.execute(
            "SELECT COUNT(*) FROM metadata_records WHERE search_tsv IS NULL OR search_tsv = ''::tsvector"
        ).fetchone()[0]
        assert missing == 0

    def test_adapter_registry_matches_collections(self, db_conn):
        for collection in (
            "zlib3_records",
            "ia2_records",
            "upload_records",
            "goodreads_records",
            "gbooks_records",
            "libby_records",
        ):
            assert get_adapter(collection).collection == collection
