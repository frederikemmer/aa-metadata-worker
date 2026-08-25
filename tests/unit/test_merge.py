"""Unit tests: metadata quality scoring + deterministic merge strategy."""

from datetime import UTC, datetime

from common.records import NormalizedRecord, merge_records, quality_score


def make_record(**overrides) -> NormalizedRecord:
    base = dict(
        md5=bytes(range(16)),
        title="Example Book",
        authors=["Jane Doe"],
        publisher=None,
        publication_year=None,
        languages=[],
        extension="epub",
        filesize=1234,
        source_collection="zlib3_records",
        source_timestamp=datetime(2024, 1, 1, tzinfo=UTC),
    )
    base.update(overrides)
    return NormalizedRecord(**base)


class TestQualityScore:
    def test_full_record_beats_sparse(self):
        full = make_record(
            publisher="Pub",
            publication_year=2020,
            languages=["deu"],
            isbn13=["9783161484100"],
            doi=["10.1000/182"],
        )
        sparse = make_record(title="", authors=[])
        assert quality_score(full) > quality_score(sparse) > 0

    def test_deterministic(self):
        rec = make_record()
        assert quality_score(rec) == quality_score(make_record())


class TestMerge:
    def test_fill_missing_fields(self):
        existing, score = make_record(), 0
        existing.publisher = None
        incoming = make_record(publisher="New Publisher", source_collection="ia2_records")
        merged, merged_score = merge_records(existing, score, incoming)
        assert merged.publisher == "New Publisher"
        assert merged_score >= score

    def test_never_null_out(self):
        existing = make_record(publisher="Keep Me")
        incoming = make_record(publisher=None)
        merged, _ = merge_records(existing, 10, incoming)
        assert merged.publisher == "Keep Me"

    def test_higher_quality_wins_scalar(self):
        sparse = make_record(title="Sparse Title")
        rich = make_record(
            title="Rich Title",
            publisher="P",
            publication_year=2001,
            languages=["eng"],
            isbn13=["9783161484100"],
            source_timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        )
        merged, _ = merge_records(sparse, 11, rich)
        assert merged.title == "Rich Title"
        # And the reverse direction keeps the better title:
        merged2, _ = merge_records(rich, 30, sparse)
        assert merged2.title == "Rich Title"

    def test_tie_newer_timestamp_wins(self):
        older = make_record(title="Old Title")
        newer = make_record(title="New Title", source_timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        merged, _ = merge_records(older, 20, newer)
        assert merged.title == "New Title"
        back, _ = merge_records(newer, 20, older)
        assert back.title == "New Title"  # tie broken by newer timestamp again

    def test_arrays_union(self):
        a = make_record(isbn13=["9783161484100"], authors=["A"])
        b = make_record(isbn13=["9780306406157"], authors=["B"])
        merged, _ = merge_records(a, 20, b)
        assert merged.isbn13 == ["9783161484100", "9780306406157"]
        assert set(merged.authors) == {"A", "B"}

    def test_tombstone_sticky_against_older(self):
        alive = make_record()
        removed = make_record(
            deleted=True,
            removed_reason="removed by request",
            source_timestamp=datetime(2025, 6, 1, tzinfo=UTC),
        )
        merged, _ = merge_records(alive, 15, removed)
        assert merged.deleted is True
        # Older record cannot revive it:
        revived, _ = merge_records(removed, 15, alive)
        assert revived.deleted is True

    def test_newer_source_can_clear_tombstone(self):
        removed = make_record(deleted=True, source_timestamp=datetime(2024, 1, 1, tzinfo=UTC))
        newer_alive = make_record(source_timestamp=datetime(2026, 8, 1, tzinfo=UTC))
        merged, _ = merge_records(removed, 10, newer_alive)
        assert merged.deleted is False

    def test_provenance_tracks_better_source(self):
        zlib_rec = make_record(source_collection="zlib3_records")
        ia_rich = make_record(
            source_collection="ia2_records",
            publisher="P",
            isbn13=["9783161484100"],
            doi=["10.1000/182"],
            oclc=["123"],
            source_timestamp=datetime(2025, 9, 9, tzinfo=UTC),
        )
        merged, _ = merge_records(zlib_rec, 12, ia_rich)
        assert merged.source_collection == "ia2_records"
