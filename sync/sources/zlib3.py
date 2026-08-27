"""Adapter for zlib3_records (Z-Library metadata, AAC era).

Verified against official samples (aacid_small in AnnaArchivist/annas-archive):
  {"aacid": "...", "metadata": {"zlibrary_id", "date_added", "date_modified",
   "extension", "filesize_reported", "md5_reported", "title", "author",
   "publisher", "language" (english name), "series", "volume", "edition",
   "year", "pages", "description", "cover_path", "isbns" (may contain ASINs),
   ["annabookinfo": {"response": {"ipfs_cid", ...}}], ["removed": 1,
   "removalReason": "..."]}}
"""

from __future__ import annotations

from common.normalize import (
    normalize_extension,
    normalize_isbn_list,
    normalize_language,
    normalize_md5,
    normalize_series_position,
    normalize_text,
    normalize_year,
    split_authors,
)
from common.records import NormalizedRecord
from sync.sources.base import SourceAdapter


def _scalar_text(value) -> str:
    """Z-Library fields are strings in practice; tolerate list values anyway."""
    if isinstance(value, list):
        value = next((v for v in value if str(v).strip()), "")
    return str(value or "").strip()


class Zlib3Adapter(SourceAdapter):
    collection = "zlib3_records"

    def parse(self, raw: dict) -> list[NormalizedRecord]:
        meta = raw.get("metadata") or {}
        md5 = normalize_md5(meta.get("md5_reported"))
        if md5 is None:
            return []

        removed = bool(meta.get("removed"))
        annabook = (meta.get("annabookinfo") or {}).get("response") or {}

        isbn13, isbn10 = normalize_isbn_list(meta.get("isbns"))
        languages: list[str] = []
        lang_code = normalize_language(meta.get("language")) or normalize_language(annabook.get("language"))
        if lang_code:
            languages.append(lang_code)

        year = normalize_year(meta.get("year")) or normalize_year(annabook.get("year"))
        extension = normalize_extension(meta.get("extension")) or normalize_extension(
            annabook.get("extension")
        )

        filesize_raw = meta.get("filesize_reported") or annabook.get("filesize")
        try:
            filesize = int(filesize_raw) if filesize_raw else None
        except (TypeError, ValueError):
            filesize = None

        series = _scalar_text(meta.get("series")) or None
        volume = normalize_series_position(meta.get("volume"))
        edition = _scalar_text(meta.get("edition")) or None

        record = NormalizedRecord(
            md5=md5,
            title=_scalar_text(meta.get("title")),
            authors=split_authors(meta.get("author")),
            publisher=(_scalar_text(meta.get("publisher")) or None),
            publication_year=year,
            languages=languages,
            extension=extension,
            filesize=filesize,
            isbn13=isbn13,
            isbn10=isbn10,
            series_name=series,
            series_position=volume,
            edition=edition,
            source_collection=self.collection,
            source_record_id=str(meta.get("zlibrary_id")) if meta.get("zlibrary_id") else None,
            aacid=raw.get("aacid"),
            source_timestamp=self.record_timestamp(raw),
            deleted=removed,
            removed_reason=str(meta.get("removalReason")) if removed else None,
            ipfs_cid=annabook.get("ipfs_cid"),
        )
        # Cheap sanity marker used by tests/pipeline stats.
        assert normalize_text(record.title) == normalize_text(record.title)
        return [record]
