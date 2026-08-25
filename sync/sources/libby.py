"""Adapter for libby_records (Libby/OverDrive scrape by volunteer "tc").

Verified against official samples (aacid_small in AnnaArchivist/annas-archive):
  {"aacid": "...", "metadata": {"id": "...", "reserveId", "title",
   "creators": [{"name", "role": "Author"|"Narrator", ...}],
   "languages": [{"id": "en", "name": "English"}],
   "publisher": {"id", "name"}, "imprint": {"id", "name"},
   "type": {"id": "audiobook"|"ebook", "name"}, "edition",
   "publishDate": "2024-08-29T00:00:00", ...}}

These records have NO file md5 - a deterministic synthetic md5
(collection|libby_id) is used as primary key. Libby is enrichment metadata:
records never carry download links.

Filtering (configurable): only media types listed in AA_LIBBY_ALLOWED_TYPES
(default: ebook,audiobook) are kept; everything else is discarded.
"""

from __future__ import annotations

from common.config import Settings, load_settings
from common.normalize import normalize_language, normalize_year
from common.records import NormalizedRecord
from sync.sources.base import SourceAdapter, synthetic_md5


def _named(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or "").strip()
    return str(entry or "").strip()


def _type_id(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("id") or entry.get("name") or "").strip()
    return str(entry or "").strip()


class LibbyAdapter(SourceAdapter):
    collection = "libby_records"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()

    def parse(self, raw: dict) -> list[NormalizedRecord]:
        meta = raw.get("metadata") or {}
        record_id = meta.get("id")
        md5 = synthetic_md5(self.collection, str(record_id) if record_id is not None else None)
        if md5 is None:
            return []

        media_type = _type_id(meta.get("type")).lower()
        if media_type and media_type not in set(self.settings.libby_allowed_types):
            return [
                NormalizedRecord(
                    md5=md5,
                    source_collection=self.collection,
                    discarded=True,
                    discard_reason=f"libby_type:{media_type}",
                )
            ]

        authors = [
            _named(creator.get("name")).strip()
            for creator in meta.get("creators") or []
            if isinstance(creator, dict)
            and "author" in str(creator.get("role") or "").lower()
            and _named(creator.get("name"))
        ]
        if not authors:
            first = str(meta.get("firstCreatorName") or "").strip()
            authors = [first] if first else []

        languages: list[str] = []
        for entry in meta.get("languages") or []:
            code = normalize_language(entry.get("id") if isinstance(entry, dict) else entry)
            if code and code not in languages:
                languages.append(code)

        publisher = _named(meta.get("publisher")) or _named(meta.get("imprint")) or None

        return [
            NormalizedRecord(
                md5=md5,
                title=str(meta.get("title") or "").strip(),
                authors=authors,
                publisher=publisher,
                publication_year=normalize_year(meta.get("publishDate")),
                languages=languages,
                extension=None,
                filesize=None,
                edition=_named(meta.get("edition")) or None,
                source_collection=self.collection,
                source_record_id=str(record_id),
                aacid=raw.get("aacid"),
                source_timestamp=self.record_timestamp(raw),
            )
        ]
