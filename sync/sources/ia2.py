"""Adapter for ia2_records (Internet Archive item metadata).

Verified against official samples:
  {"aacid": "...", "metadata": {"ia_id", "metadata_json": {
      ..., "metadata": {"identifier", "title", "creator"/"associated-names",
      "publisher", "date", "isbn" (may hold multiple, mixed formats),
      "language" (ISO 639-1), "oclc-id", "openlibrary_edition",
      "openlibrary_work", "mediatype", ...},
      ["aa_shorter_files": [{"name", "size", "md5", ...}]]}}}

One NormalizedRecord is emitted per usable file (aa_shorter_files entry with a
valid md5), inheriting the item-level bibliographic data. Items without usable
files produce zero records.

Book-only filtering: items with an explicit IA mediatype other than "texts"
(audio, video, ...) are discarded when AA_IA_REQUIRE_TEXTS is enabled.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from common.config import Settings, load_settings
from common.normalize import (
    normalize_extension,
    normalize_isbn_list,
    normalize_language,
    normalize_md5,
    normalize_series_position,
    normalize_year,
    split_authors,
)
from common.records import NormalizedRecord
from sync.sources.base import SourceAdapter


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def _as_text(value) -> str:
    """Scalar text from an IA field; IA sometimes sends lists (e.g. title).

    Uses the first non-empty entry - multi-entry semantics differ per field
    and are handled explicitly where they matter (authors, isbn, ...).
    """
    items = _as_list(value)
    return items[0] if items else ""


class Ia2Adapter(SourceAdapter):
    collection = "ia2_records"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()

    def parse(self, raw: dict) -> list[NormalizedRecord]:
        meta = raw.get("metadata") or {}
        ia_meta = ((meta.get("metadata_json") or {}).get("metadata")) or {}
        files = (meta.get("metadata_json") or {}).get("aa_shorter_files") or []

        # ── Book-only filter: only IA "texts" items ──────────────────────
        mediatype = _as_text(ia_meta.get("mediatype")).lower()
        if self.settings.ia_require_texts and mediatype and mediatype != "texts":
            first_md5 = normalize_md5(next((f.get("md5") for f in files if isinstance(f, dict)), None))
            if first_md5:
                return [
                    NormalizedRecord(
                        md5=first_md5,
                        source_collection=self.collection,
                        source_record_id=str(meta.get("ia_id")),
                        discarded=True,
                        discard_reason=f"ia_mediatype:{mediatype}",
                    )
                ]
            return []
        # ─────────────────────────────────────────────────────────────────

        isbn13, isbn10 = normalize_isbn_list(_as_list(ia_meta.get("isbn")))
        oclc = [v for v in _as_list(ia_meta.get("oclc-id")) if v.isdigit()]
        ol_ids = [
            v
            for v in _as_list(ia_meta.get("openlibrary_edition")) + _as_list(ia_meta.get("openlibrary_work"))
            if re.fullmatch(r"OL\d+[MW]", v)
        ]

        authors: list[str] = []
        for name in _as_list(ia_meta.get("creator")) + _as_list(ia_meta.get("associated-names")):
            for author in split_authors(name):
                if author not in authors:
                    authors.append(author)

        languages: list[str] = []
        lang_code = normalize_language(ia_meta.get("language"))
        if lang_code:
            languages.append(lang_code)

        year = normalize_year(ia_meta.get("date"))

        series_name = _as_text(ia_meta.get("series")) or None
        series_position = normalize_series_position(ia_meta.get("volume"))
        edition = _as_text(ia_meta.get("edition")) or None

        records: list[NormalizedRecord] = []
        seen_md5: set[bytes] = set()
        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            md5 = normalize_md5(file_info.get("md5"))
            if md5 is None or md5 in seen_md5:
                continue
            seen_md5.add(md5)
            name = str(file_info.get("name") or "")
            extension = normalize_extension(PurePosixPath(name).suffix)
            try:
                filesize = int(file_info.get("size")) if file_info.get("size") else None
            except (TypeError, ValueError):
                filesize = None
            records.append(
                NormalizedRecord(
                    md5=md5,
                    title=_as_text(ia_meta.get("title")),
                    authors=list(authors),
                    publisher=(_as_text(ia_meta.get("publisher")) or None),
                    publication_year=year,
                    languages=languages,
                    extension=extension,
                    filesize=filesize,
                    isbn13=isbn13,
                    isbn10=isbn10,
                    doi=[],
                    oclc=list(oclc),
                    openlibrary_ids=ol_ids,
                    series_name=series_name,
                    series_position=series_position,
                    edition=edition,
                    source_collection=self.collection,
                    source_record_id=str(meta.get("ia_id")) if meta.get("ia_id") else None,
                    aacid=raw.get("aacid"),
                    source_timestamp=self.record_timestamp(raw),
                )
            )
        return records
