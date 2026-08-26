"""Adapter for goodreads_records (Goodreads scrape by volunteer "tc").

Verified against official samples (aacid_small in AnnaArchivist/annas-archive):
  {"aacid": "...", "metadata": {"id": <int>, "record": "<GoodreadsResponse XML>"}}

The XML embeds the classic Goodreads API book payload: id, title,
title_without_series, isbn, isbn13, publication_year(/month/day), publisher,
language_code (ISO 639-2), num_pages, format, edition_information,
authors > author > name, work > original_publication_year.

These records have NO file md5 - a deterministic synthetic md5
(collection|goodreads_id) is used as primary key so repeated imports merge
onto the same row. Goodreads is enrichment metadata: records never carry
download links.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from common.normalize import normalize_isbn_list, normalize_language, normalize_year, split_authors
from common.records import NormalizedRecord
from sync.sources.base import SourceAdapter, synthetic_md5


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


class GoodreadsAdapter(SourceAdapter):
    collection = "goodreads_records"

    def parse(self, raw: dict) -> list[NormalizedRecord]:
        meta = raw.get("metadata") or {}
        record_id = meta.get("id")
        md5 = synthetic_md5(self.collection, str(record_id) if record_id is not None else None)
        if md5 is None:
            return []

        root = ET.fromstring(str(meta.get("record") or ""))
        book = root.find("book")

        title = _text(book.find("title")) or _text(book.find("title_without_series"))
        authors = split_authors("; ".join(filter(None, (_text(a.find("name")) for a in book.iter("author")))))

        raw_isbns = [_text(book.find(tag)) for tag in ("isbn13", "isbn")]
        isbn13, isbn10 = normalize_isbn_list([i for i in raw_isbns if i])

        year = normalize_year(_text(book.find("publication_year"))) or normalize_year(
            _text(book.findtext("work/original_publication_year"))
        )
        language = normalize_language(_text(book.find("language_code")))
        languages = [language] if language else []

        return [
            NormalizedRecord(
                md5=md5,
                title=title,
                authors=authors,
                publisher=_text(book.find("publisher")) or None,
                publication_year=year,
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
