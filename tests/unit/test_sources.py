"""Source adapter tests against realistic fixtures from official AA samples."""

import dataclasses
import json
from pathlib import Path

import pytest

from common.config import load_settings
from sync.sources import get_adapter, known_collections
from sync.sources.base import aacid_timestamp

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_fixture(name: str) -> list[dict]:
    lines = (FIXTURES / f"{name}.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


class TestRegistry:
    def test_known_collections(self):
        assert known_collections() == ["ia2_records", "upload_records", "zlib3_records"]

    def test_unknown_raises(self):
        with pytest.raises(KeyError):
            get_adapter("nexusstc_records")


class TestAacidTimestamp:
    def test_parse(self):
        ts = aacid_timestamp("aacid__zlib3_records__20230808T014342Z__123__abc")
        assert ts is not None and ts.year == 2023 and ts.tzinfo is not None


class TestZlib3Adapter:
    adapter = get_adapter("zlib3_records")

    def test_parses_all_fixture_lines_with_md5(self):
        raws = load_fixture("zlib3_records")
        parsed = [r for raw in raws for r in self.adapter.parse(raw)]
        # Every fixture line has md5_reported except none; all should parse.
        assert len(parsed) >= len(raws) - 1  # allow at most one md5-less line
        assert all(len(r.md5) == 16 for r in parsed)

    def test_fields(self):
        raw = load_fixture("zlib3_records")[0]
        rec = self.adapter.parse(raw)[0]
        meta = raw["metadata"]
        assert rec.title == meta["title"]
        assert rec.extension == meta["extension"]
        assert rec.filesize == int(meta["filesize_reported"])
        assert rec.source_record_id == str(meta["zlibrary_id"])

    def test_removed_record_flagged(self):
        removed = [
            r
            for raw in load_fixture("zlib3_records")
            for r in self.adapter.parse(raw)
            if raw["metadata"].get("removed")
        ]
        if removed:  # fixture contains at least one
            assert all(r.deleted and r.removed_reason for r in removed)

    def test_asin_isbn_not_indexed(self):
        first = self.adapter.parse(load_fixture("zlib3_records")[0])[0]
        for isbn in first.isbn13 + first.isbn10:
            assert not isbn.startswith("B")

    def test_ipfs_cid_extracted(self):
        records = [r for raw in load_fixture("zlib3_records") for r in self.adapter.parse(raw)]
        ipfs = [r.ipfs_cid for r in records if r.ipfs_cid]
        assert ipfs, "fixture should contain at least one annabookinfo.ipfs_cid"


class TestIa2Adapter:
    adapter = get_adapter("ia2_records")

    def test_one_record_per_file(self):
        raws = load_fixture("ia2_records")
        total = sum(len(self.adapter.parse(raw)) for raw in raws)
        assert total > len(raws), "items with multiple files yield multiple records"

    def test_inherits_item_metadata(self):
        raw = load_fixture("ia2_records")[0]
        record = self.adapter.parse(raw)[0]
        ia_meta = raw["metadata"]["metadata_json"]["metadata"]
        assert record.title == str(ia_meta["title"]).strip()
        assert record.languages == ["eng"]
        assert record.oclc == [str(ia_meta["oclc-id"])]
        assert any(oid.startswith("OL") for oid in record.openlibrary_ids)

    def test_list_shaped_fields_do_not_crash(self):
        """IA metadata fields are sometimes lists; parse must not raise (prod 876/42928)."""
        base = load_fixture("ia2_records")[0]
        ia_meta = base["metadata"]["metadata_json"]["metadata"]
        ia_meta.update(
            title=["A List Title", "Second Entry"],
            publisher=["Listed Publisher"],
            creator=["First Author", "Second Author"],
            date="1999",
            language=["eng"],
        )
        records = [
            r
            for r in self.adapter.parse(base)
            if not r.discarded and not r.deleted
        ]
        assert records, "usable files expected in fixture item"
        for record in records:
            assert record.title == "A List Title"
            assert record.publisher == "Listed Publisher"
            assert "First Author" in record.authors
            assert "Second Author" in record.authors
            assert record.languages == ["eng"]

    def test_list_mediatype_still_filtered(self):
        """mediatype as list must not bypass the texts-only filter."""
        raw = load_fixture("ia2_records")[0]
        ia_meta = raw["metadata"]["metadata_json"]["metadata"]
        ia_meta["mediatype"] = ["audio"]
        parsed = self.adapter.parse(raw)
        assert len(parsed) == 1
        assert parsed[0].discarded
        assert parsed[0].discard_reason == "ia_mediatype:audio"


def _relaxed_uploads_adapter():
    """Adapter with book-quality gate disabled (for structural tests)."""
    return type(get_adapter("upload_records"))(
        dataclasses.replace(load_settings(), upload_require_title_author=False)
    )


class TestUploadsAdapter:
    adapter = get_adapter("upload_records")

    def _relaxed(self):
        return type(self.adapter)(
            dataclasses.replace(load_settings(), upload_require_title_author=False)
        )

    def test_parses_fixture(self):
        raws = load_fixture("upload_records")
        relaxed = self._relaxed()
        parsed = [r for raw in raws for r in relaxed.parse(raw) if not r.discarded]
        assert len(parsed) >= len(raws) - 1
        assert all(r.md5 for r in parsed)

    def test_deleted_as_duplicate_tombstone(self):
        raws = load_fixture("upload_records")
        target = next(raw for raw in raws if raw["metadata"].get("deleted_as_duplicate"))
        # Ensure it passes the quality gate so we test the tombstone path itself.
        exif = target["metadata"].setdefault("exiftool_output", {})
        exif.setdefault("Title", "Some Title")
        exif.setdefault("Author", "Some Author")
        record = self.adapter.parse(target)[0]
        assert not record.discarded
        assert record.deleted and record.removed_reason == "deleted_as_duplicate"

    def test_title_fallback_from_filepath(self):
        raws = load_fixture("upload_records")
        relaxed = self._relaxed()
        no_exif = next(
            raw
            for raw in raws
            if not (raw["metadata"].get("exiftool_output") or {}).get("Title")
        )
        record = relaxed.parse(no_exif)[0]
        assert record.title, "filepath fallback must produce non-empty title"

    def test_quality_gate_discards_metadata_free_records(self):
        raws = load_fixture("upload_records")
        candidate = None
        for raw in raws:
            exif = raw["metadata"].get("exiftool_output") or {}
            if not (exif.get("Title") and exif.get("Author")):
                candidate = raw
                break
        assert candidate is not None, "fixture should contain a metadata-free record"
        result = self.adapter.parse(candidate)[0]
        assert result.discarded
        assert result.discard_reason == "missing_title_or_author"

