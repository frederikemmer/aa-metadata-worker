"""Adapter for upload_records (Anna's Archive direct uploads).

Verified against official samples:
  {"aacid": "aacid__upload_records[_<subcollection>]__...", "metadata": {
   "primary_id", "md5", "filepath", "filename", "filesize", "file_type",
   ["is_useful_file": bool], ["total_pages"], ["exiftool_output": {...}],
   ["pikepdf_docinfo": {"/Author", "/Title", ...}],
   ["deleted_as_duplicate": true, ...]}}

Book-only filtering (configurable, see docker-compose.yaml):
  * Subcollections that are known non-book material are discarded entirely
    (AA_UPLOAD_BLOCKED_SUBCOLLECTIONS).
  * Records without real bibliographic title+author (from exiftool/pikepdf,
    NOT the filename fallback) are discarded when
    AA_UPLOAD_REQUIRE_TITLE_AUTHOR is enabled.
Discarded records are counted in sync_releases.records_discarded.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from common.config import Settings, load_settings
from common.normalize import (
    normalize_extension,
    normalize_language,
    normalize_md5,
    normalize_year,
)
from common.records import NormalizedRecord
from sync.sources.base import SourceAdapter

logger = logging.getLogger(__name__)

_FILENAME_CLEAN_RE = re.compile(r"[\W_]+")
_SUBCOLL_RE = re.compile(r"^aacid__upload_records_(?:([a-z0-9_]+?)__)?\d{8}T\d{6}Z__")


def subcollection_of(aacid: str | None) -> str | None:
    """Extract the upload subcollection from an AACID, e.g. 'aaaaarg'."""
    if not aacid:
        return None
    match = _SUBCOLL_RE.match(aacid)
    return match.group(1) if match else None


def _title_from_filepath(filepath: str) -> str:
    words = _FILENAME_CLEAN_RE.sub(" ", PurePosixPath(filepath).stem).strip()
    return re.sub(r"\s+", " ", words)


class UploadsAdapter(SourceAdapter):
    collection = "upload_records"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings()

    def parse(self, raw: dict) -> list[NormalizedRecord]:
        meta = raw.get("metadata") or {}
        md5 = normalize_md5(meta.get("md5"))
        if md5 is None:
            return []

        settings = self.settings
        exif = meta.get("exiftool_output") or {}
        pikepdf = meta.get("pikepdf_docinfo") or {}
        bibliographic_title = str(exif.get("Title") or "").strip() or str(pikepdf.get("/Title") or "").strip()
        bibliographic_author = (
            str(exif.get("Author") or "").strip() or str(pikepdf.get("/Author") or "").strip()
        )

        def discarded_record(reason: str) -> NormalizedRecord:
            return NormalizedRecord(
                md5=md5,
                title=bibliographic_title,
                authors=[author.strip() for author in bibliographic_author.split(";") if author.strip()],
                source_collection=self.collection,
                source_record_id=str(meta.get("primary_id")) if meta.get("primary_id") else None,
                aacid=raw.get("aacid"),
                discarded=True,
                discard_reason=reason,
            )

        # ── Book-only filters ────────────────────────────────────────────
        subcoll = subcollection_of(raw.get("aacid"))
        if subcoll and subcoll in set(settings.upload_blocked_subcollections):
            return [discarded_record(f"blocked_subcollection:{subcoll}")]

        if settings.upload_require_title_author and not (bibliographic_title and bibliographic_author):
            return [discarded_record("missing_title_or_author")]
        # ─────────────────────────────────────────────────────────────────

        title = bibliographic_title or _title_from_filepath(str(meta.get("filepath") or ""))
        publisher = (
            str(exif.get("Publisher") or "").strip() or str(pikepdf.get("/Publisher") or "").strip() or None
        )

        languages: list[str] = []
        lang_code = normalize_language(exif.get("Language"))
        if lang_code:
            languages.append(lang_code)

        year = normalize_year(exif.get("CreateDate")) or normalize_year(pikepdf.get("/CreationDate"))

        extension = normalize_extension(meta.get("file_type")) or normalize_extension(
            PurePosixPath(str(meta.get("filepath") or "")).suffix
        )

        deleted = bool(meta.get("deleted_as_duplicate"))

        record = NormalizedRecord(
            md5=md5,
            title=title,
            authors=[
                a
                for a in (
                    bibliographic_author.split(";") if ";" in bibliographic_author else [bibliographic_author]
                )
                if a.strip()
            ],
            publisher=publisher,
            publication_year=year,
            languages=languages,
            extension=extension,
            filesize=int(meta["filesize"]) if meta.get("filesize") else None,
            source_collection=self.collection,
            source_record_id=str(meta.get("primary_id")) if meta.get("primary_id") else None,
            aacid=raw.get("aacid"),
            source_timestamp=self.record_timestamp(raw),
            deleted=deleted,
            removed_reason="deleted_as_duplicate" if deleted else None,
        )
        return [record]
