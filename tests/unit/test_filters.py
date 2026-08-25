"""Unit tests for the book-only filters (uploads subcollections, ia2 mediatype)."""

import json
from pathlib import Path

from common.config import load_settings
from sync.sources import get_adapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestSubcollectionExtraction:
    def test_with_subcollection(self):
        from sync.sources.uploads import subcollection_of

        assert (
            subcollection_of("aacid__upload_records_aaaaarg__20240627T210539Z__4871860__Hurqe")
            == "aaaaarg"
        )

    def test_plain_upload_records(self):
        from sync.sources.uploads import subcollection_of

        assert (
            subcollection_of("aacid__upload_records__20240627T210539Z__4871860__Hurqe") is None
        )


class TestUploadFilters:
    adapter = get_adapter("upload_records")

    def test_blocked_subcollection_discarded(self):
        raw = {
            "aacid": "aacid__upload_records_academia_edu__20250101T000000Z__1__Abc",
            "metadata": {
                "md5": "a" * 32,
                "filepath": "papers/some-paper.pdf",
                "exiftool_output": {"Title": "A Paper", "Author": "Someone"},
            },
        }
        result = self.adapter.parse(raw)
        assert len(result) == 1
        assert result[0].discarded
        assert result[0].discard_reason == "blocked_subcollection:academia_edu"

    def test_missing_title_author_discarded(self):
        # aaaaarg fixture records carry exiftool data; craft one without Title.
        raw = {
            "aacid": "aacid__upload_records_shuge__20250101T000000Z__2__Def",
            "metadata": {
                "md5": "b" * 32,
                "filepath": "books/unknown-file.epub",
                "filesize": 1234,
                "file_type": "epub",
            },
        }
        result = self.adapter.parse(raw)
        assert result[0].discarded
        assert result[0].discard_reason == "missing_title_or_author"

    def test_good_record_passes_gate(self):
        raw = {
            "aacid": "aacid__upload_records_shuge__20250101T000000Z__3__Ghi",
            "metadata": {
                "md5": "c" * 32,
                "filepath": "books/good-book.epub",
                "filesize": 4321,
                "file_type": "epub",
                "exiftool_output": {"Title": "Ein Buch", "Author": "Autor Name"},
            },
        }
        result = self.adapter.parse(raw)
        record = result[0]
        assert not record.discarded
        assert record.title == "Ein Buch"
        assert record.authors == ["Autor Name"]

    def test_gate_can_be_disabled(self):
        import dataclasses

        relaxed = dataclasses.replace(load_settings(), upload_require_title_author=False)
        adapter = type(self.adapter)(relaxed)
        raw = {
            "aacid": "aacid__upload_records_shuge__20250101T000000Z__4__Jkl",
            "metadata": {"md5": "d" * 32, "filepath": "x/y.epub"},
        }
        result = adapter.parse(raw)
        assert not result[0].discarded


class TestIa2MediatypeFilter:
    adapter = get_adapter("ia2_records")

    @staticmethod
    def _raw(mediatype: str | None) -> dict:
        meta = {
            "ia_id": "some_item",
            "metadata_json": {
                "metadata": {
                    "identifier": "some_item",
                    "title": "T",
                    "creator": "A",
                    "isbn": "9783161484100",
                    "language": "eng",
                },
                "aa_shorter_files": [{"name": "f.pdf", "size": "10", "md5": "e" * 32}],
            },
        }
        if mediatype is not None:
            meta["metadata_json"]["metadata"]["mediatype"] = mediatype
        return {"aacid": "aacid__ia2_records__20250101T000000Z__5__Mno", "metadata": meta}

    def test_audio_item_discarded(self):
        result = self.adapter.parse(self._raw("audio"))
        assert result and result[0].discarded
        assert result[0].discard_reason == "ia_mediatype:audio"

    def test_texts_item_imported(self):
        result = self.adapter.parse(self._raw("texts"))
        assert len(result) == 1
        assert not result[0].discarded
        assert result[0].title == "T"

    def test_missing_mediatype_defaults_to_keep(self):
        result = self.adapter.parse(self._raw(None))
        assert not result[0].discarded

    def test_filter_can_be_disabled(self):
        import dataclasses

        relaxed = dataclasses.replace(load_settings(), ia_require_texts=False)
        adapter = type(self.adapter)(relaxed)
        result = adapter.parse(self._raw("audio"))
        assert not result[0].discarded


class TestFixturesStillParse:
    """The realistic official fixtures must still produce non-discarded records."""

    def test_zlib_fixture_unaffected(self):
        zlib_adapter = get_adapter("zlib3_records")
        for raw in load_fixture("zlib3_records"):
            for record in zlib_adapter.parse(raw):
                assert not record.discarded  # zlib has no book filter
