"""Adapter for gbooks_records (Google Books metadata dump).

Verified against official samples (aacid_small in AnnaArchivist/annas-archive):
  {"aacid": "...", "metadata": {"id": "<volume id>", "industryIdentifiers":
   [{"type": "ISBN_13"|"ISBN_10", "identifier": "..."}], "title", "subtitle",
   "authors": [...], "pageCount", "printType": "BOOK"|"MAGAZINE",
   "language": "en", "publishedDate": "2011-01-20"}}

These records have NO file md5 - a deterministic synthetic md5
(collection|gbooks_id) is used as primary key. Google Books is enrichment
metadata: records never carry download links.

Book-only filtering (configurable): records with printType != "BOOK"
(magazines) are discarded unless AA_GBOOKS_REQUIRE_BOOKS is disabled.
"""

from __future__ import annotations

from common.config import Settings, load_settings
from common.normalize import normalize_isbn_list, normalize_language, normalize_year, split_authors
from common.records import NormalizedRecord
from sync.sources.base import SourceAdapter, synthetic_md5


def _identifiers(meta: dict) -> tuple[list[str], list[str]]:
    values = [
        str(entry.get("identifier") or "").strip()
        for entry in meta.get("industryIdentifiers") or []
        if isinstance(entry, dict)
    ]
    return normalize_isbn_list([v for v in values if v])


class GbooksAdapter(SourceAdapter):
    collection = "gbooks_records"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()

    def parse(self, raw: dict) -> list[NormalizedRecord]:
        meta = raw.get("metadata") or {}
        record_id = meta.get("id")
        md5 = synthetic_md5(self.collection, str(record_id) if record_id is not None else None)
        if md5 is None:
            return []

        print_type = str(meta.get("printType") or "").strip()
        if self.settings.gbooks_require_books and print_type and print_type != "BOOK":
            return [
                NormalizedRecord(
                    md5=md5,
                    source_collection=self.collection,
                    discarded=True,
                    discard_reason=f"gbooks_printtype:{print_type}",
                )
            ]

        isbn13, isbn10 = _identifiers(meta)
        language = normalize_language(meta.get("language"))
        languages = [language] if language else []

        return [
            NormalizedRecord(
                md5=md5,
                title=str(meta.get("title") or "").strip(),
                authors=split_authors("; ".join(str(a) for a in meta.get("authors") or [])),
                publisher=None,
                publication_year=normalize_year(meta.get("publishedDate")),
                languages=languages,
                extension=None,
                filesize=None,
                isbn13=isbn13,
                isbn10=isbn10,
                source_collection=self.collection,
                source_record_id=str(record_id),
                aacid=raw.get("aacid"),
                source_timestamp=self.record_timestamp(raw),
            )
        ]
