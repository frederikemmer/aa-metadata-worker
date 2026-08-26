"""Normalized metadata record + deterministic quality score and merge rules.

The same MD5 can appear in multiple collections/releases. We never blindly do
"last write wins": field values are merged so that information is only ever
added or replaced by *better* data (see docs/data-model.md "Merge Strategy").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Weights for metadata_quality_score. Higher = more useful for search/matching.
WEIGHTS = {
    "title": 10,
    "authors": 8,
    "publisher": 3,
    "publication_year": 3,
    "languages": 2,
    "isbn13": 6,
    "isbn10": 3,
    "doi": 3,
    "oclc": 2,
    "openlibrary_ids": 2,
    "extension": 1,
    "filesize": 1,
    "series_name": 2,
    "series_position": 1,
    "edition": 1,
}


@dataclass
class NormalizedRecord:
    md5: bytes  # 16 raw bytes
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str | None = None
    publication_year: int | None = None
    languages: list[str] = field(default_factory=list)
    extension: str | None = None
    filesize: int | None = None

    isbn13: list[str] = field(default_factory=list)
    isbn10: list[str] = field(default_factory=list)
    doi: list[str] = field(default_factory=list)
    oclc: list[str] = field(default_factory=list)
    openlibrary_ids: list[str] = field(default_factory=list)

    series_name: str | None = None
    series_position: int | None = None
    edition: str | None = None

    source_collection: str = ""
    source_record_id: str | None = None
    aacid: str | None = None
    source_timestamp: datetime | None = None

    deleted: bool = False
    removed_reason: str | None = None
    ipfs_cid: str | None = None

    # Set by source adapters when a record is deliberately discarded during
    # import (non-book subcollection, missing bibliographic data, ...).
    # Discarded records are counted, never written to the database.
    discarded: bool = False
    discard_reason: str | None = None


def quality_score(record: NormalizedRecord) -> int:
    """Reproducible completeness score used by the merge strategy."""
    score = 0
    if record.title:
        score += WEIGHTS["title"]
    if record.authors:
        score += WEIGHTS["authors"]
    if record.publisher:
        score += WEIGHTS["publisher"]
    if record.publication_year:
        score += WEIGHTS["publication_year"]
    if record.languages:
        score += WEIGHTS["languages"]
    if record.isbn13:
        score += WEIGHTS["isbn13"]
    if record.isbn10:
        score += WEIGHTS["isbn10"]
    if record.doi:
        score += WEIGHTS["doi"]
    if record.oclc:
        score += WEIGHTS["oclc"]
    if record.openlibrary_ids:
        score += WEIGHTS["openlibrary_ids"]
    if record.extension:
        score += WEIGHTS["extension"]
    if record.filesize:
        score += WEIGHTS["filesize"]
    if record.series_name:
        score += WEIGHTS["series_name"]
    if record.series_position is not None:
        score += WEIGHTS["series_position"]
    if record.edition:
        score += WEIGHTS["edition"]
    return score


def _merge_arrays(existing: list[str], incoming: list[str]) -> list[str]:
    """Order-stable union."""
    return list(dict.fromkeys([*existing, *incoming]))


def merge_records(
    existing: NormalizedRecord, existing_score: int, incoming: NormalizedRecord
) -> tuple[NormalizedRecord, int]:
    """Merge `incoming` into `existing` (both represent the same MD5).

    Rules (deterministic):
      * Arrays: union of both sides.
      * Scalar fields: the higher-quality side wins; on a quality tie the newer
        source_timestamp wins; if still tied, keep the stored value.
      * A scalar is only replaced by a non-empty value (never nulled out).
      * deleted/removed_reason: once removed, stays unless a strictly newer
        source record clears it.
    Returns (merged_record, merged_quality_score).
    """
    inc_score = quality_score(incoming)

    def prefer_scalar(existing_val, incoming_val) -> object:
        if existing_val in (None, "", []) and incoming_val not in (None, ""):
            return incoming_val
        if incoming_val in (None, ""):
            return existing_val
        if inc_score > existing_score:
            return incoming_val
        if inc_score < existing_score:
            return existing_val
        # Tie: newer timestamp wins; equal timestamps keep existing.
        inc_ts = incoming.source_timestamp or datetime.min.replace(tzinfo=None)
        ex_ts = existing.source_timestamp or datetime.min.replace(tzinfo=None)
        return incoming_val if inc_ts > ex_ts else existing_val

    merged = NormalizedRecord(
        md5=existing.md5,
        title=str(prefer_scalar(existing.title, incoming.title)),
        authors=_merge_arrays(existing.authors, incoming.authors),
        publisher=prefer_scalar(existing.publisher, incoming.publisher),  # type: ignore[arg-type]
        publication_year=prefer_scalar(existing.publication_year, incoming.publication_year),  # type: ignore[arg-type]
        languages=_merge_arrays(existing.languages, incoming.languages),
        extension=prefer_scalar(existing.extension, incoming.extension),  # type: ignore[arg-type]
        filesize=prefer_scalar(existing.filesize, incoming.filesize),  # type: ignore[arg-type]
        series_name=prefer_scalar(existing.series_name, incoming.series_name),  # type: ignore[arg-type]
        series_position=prefer_scalar(existing.series_position, incoming.series_position),  # type: ignore[arg-type]
        edition=prefer_scalar(existing.edition, incoming.edition),  # type: ignore[arg-type]
        isbn13=_merge_arrays(existing.isbn13, incoming.isbn13),
        isbn10=_merge_arrays(existing.isbn10, incoming.isbn10),
        doi=_merge_arrays(existing.doi, incoming.doi),
        oclc=_merge_arrays(existing.oclc, incoming.oclc),
        openlibrary_ids=_merge_arrays(existing.openlibrary_ids, incoming.openlibrary_ids),
        # Provenance tracks the highest-quality contributing record seen.
        source_collection=(
            existing.source_collection
            if inc_score < existing_score
            else (incoming.source_collection or existing.source_collection)
        ),
        source_record_id=(
            existing.source_record_id
            if inc_score < existing_score
            else (incoming.source_record_id or existing.source_record_id)
        ),
        aacid=incoming.aacid or existing.aacid,
        source_timestamp=max(
            filter(
                None,
                [existing.source_timestamp, incoming.source_timestamp],
            ),
            default=None,
        ),
        ipfs_cid=existing.ipfs_cid or incoming.ipfs_cid,
    )

    # Deletion tombstones are sticky against older/equal records.
    if incoming.deleted and not existing.deleted:
        merged.deleted = True
        merged.removed_reason = incoming.removed_reason
    elif existing.deleted:
        if (
            not incoming.deleted
            and incoming.source_timestamp
            and (not existing.source_timestamp or incoming.source_timestamp > existing.source_timestamp)
        ):
            merged.deleted = False
            merged.removed_reason = None
        else:
            merged.deleted = True
            merged.removed_reason = existing.removed_reason

    merged_score = max(existing_score, inc_score)
    return merged, merged_score
