"""Streaming import pipeline: compressed stream -> parser -> normalizer ->
batch buffer -> PostgreSQL upsert (merge in application layer).

Memory usage is bounded by SYNC_BATCH_SIZE regardless of file size. The merge
implementation lives in common.records.merge_records and is used both here and
in tests, so there is exactly one definition of the strategy.
"""

from __future__ import annotations

import io
import json
import logging
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import zstandard

from common.config import Settings, load_settings
from common.normalize import (
    derive_work_key,
    md5_to_hex,
    normalize_text,
)
from common.records import NormalizedRecord, merge_records, quality_score
from sync import state
from sync.sources import get_adapter

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)

_UPSERT_SQL = """
INSERT INTO metadata_records (
    md5, title, title_norm, authors, author_tokens, publisher, publication_year,
    languages, extension, filesize, isbn10, isbn13, doi, oclc, openlibrary_ids,
    work_key, series_name, series_position, edition,
    source_collection, source_record_id, aacid, source_timestamp,
    quality_score, deleted, removed_reason, ipfs_cid
) VALUES (
    %(md5)s, %(title)s, %(title_norm)s, %(authors)s, %(author_tokens)s, %(publisher)s,
    %(publication_year)s, %(languages)s, %(extension)s, %(filesize)s,
    %(isbn10)s, %(isbn13)s, %(doi)s, %(oclc)s, %(openlibrary_ids)s,
    %(work_key)s, %(series_name)s, %(series_position)s, %(edition)s,
    %(source_collection)s, %(source_record_id)s, %(aacid)s,
    %(source_timestamp)s, %(quality_score)s, %(deleted)s, %(removed_reason)s, %(ipfs_cid)s
)
ON CONFLICT (md5) DO UPDATE SET
    title = EXCLUDED.title,
    title_norm = EXCLUDED.title_norm,
    authors = EXCLUDED.authors,
    author_tokens = EXCLUDED.author_tokens,
    publisher = EXCLUDED.publisher,
    publication_year = EXCLUDED.publication_year,
    languages = EXCLUDED.languages,
    extension = EXCLUDED.extension,
    filesize = EXCLUDED.filesize,
    isbn10 = EXCLUDED.isbn10,
    isbn13 = EXCLUDED.isbn13,
    doi = EXCLUDED.doi,
    oclc = EXCLUDED.oclc,
    openlibrary_ids = EXCLUDED.openlibrary_ids,
    work_key = EXCLUDED.work_key,
    series_name = COALESCE(EXCLUDED.series_name, metadata_records.series_name),
    series_position = COALESCE(EXCLUDED.series_position, metadata_records.series_position),
    edition = COALESCE(EXCLUDED.edition, metadata_records.edition),
    source_collection = EXCLUDED.source_collection,
    source_record_id = EXCLUDED.source_record_id,
    aacid = EXCLUDED.aacid,
    source_timestamp = COALESCE(EXCLUDED.source_timestamp, metadata_records.source_timestamp),
    quality_score = GREATEST(metadata_records.quality_score, EXCLUDED.quality_score),
    deleted = EXCLUDED.deleted,
    removed_reason = EXCLUDED.removed_reason,
    ipfs_cid = COALESCE(EXCLUDED.ipfs_cid, metadata_records.ipfs_cid)
"""



class GracefulShutdown(RuntimeError):
    """Raised between batches when SIGTERM was received."""


_shutdown_requested = False


def request_shutdown(signum, _frame) -> None:  # pragma: no cover - signal plumbing
    global _shutdown_requested
    _shutdown_requested = True
    logger.info("Shutdown requested (%s); finishing current batch...", signum)


def install_signal_handlers() -> None:
    global _shutdown_requested
    _shutdown_requested = False
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


@dataclass
class ImportStats:
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    discarded: int = 0
    failed: int = 0
    batches: int = 0
    error_samples: list[str] = field(default_factory=list)


def _strip_nul(value: str | None) -> str | None:
    """Remove NUL (0x00) bytes — PostgreSQL text columns reject them."""
    if value is None:
        return None
    return value.replace("\x00", "") if "\x00" in value else value


def record_to_params(record: NormalizedRecord) -> dict:
    """NormalizedRecord -> DB row dict incl. derived normalized columns."""
    title_norm = normalize_text(record.title)
    author_norm_names = [normalize_text(a) for a in record.authors if normalize_text(a)]
    author_tokens: list[str] = []
    for name in author_norm_names:
        for token in _TOKEN_RE.findall(name):
            if token not in author_tokens:
                author_tokens.append(token)
    return {
        "md5": record.md5,
        "title": _strip_nul(record.title) or "",
        "title_norm": _strip_nul(title_norm),
        "authors": [_strip_nul(a) for a in record.authors if a],
        "author_tokens": author_tokens,
        "publisher": _strip_nul(record.publisher),
        "publication_year": record.publication_year,
        "languages": [_strip_nul(lang) for lang in record.languages],
        "extension": record.extension,
        "filesize": record.filesize,
        "isbn10": record.isbn10,
        "isbn13": record.isbn13,
        "doi": record.doi,
        "oclc": record.oclc,
        "openlibrary_ids": record.openlibrary_ids,
        "work_key": derive_work_key(record.isbn13, record.isbn10, record.doi, record.openlibrary_ids),
        "series_name": _strip_nul(record.series_name),
        "series_position": record.series_position,
        "edition": _strip_nul(record.edition),
        "source_collection": record.source_collection,
        "source_record_id": record.source_record_id,
        "aacid": record.aacid,
        "source_timestamp": record.source_timestamp,
        "quality_score": quality_score(record),
        "deleted": record.deleted,
        "removed_reason": record.removed_reason,
        "ipfs_cid": record.ipfs_cid,
    }



def process_batch(
    conn: psycopg.Connection,
    records: list[tuple[NormalizedRecord, str]],
    stats: ImportStats,
) -> None:
    """Merge one batch of parsed records into the database atomically.

    `records` items are (record, raw_line_for_errors). Duplicates inside a
    batch are folded first so every md5 appears once per statement. The
    UPSERT's ON CONFLICT clause handles field-level merge (COALESCE for
    provenance fields, GREATEST for quality_score), so we skip the costly
    SELECT+merge round-trip against existing rows.
    """
    # Fold duplicates within the batch.
    folded: dict[bytes, tuple[NormalizedRecord, int]] = {}
    for record, _raw in records:
        score = quality_score(record)
        existing_entry = folded.get(record.md5)
        if existing_entry is None:
            folded[record.md5] = (record, score)
        else:
            merged, merged_score = merge_records(existing_entry[0], existing_entry[1], record)
            folded[record.md5] = (merged, merged_score)

    final_rows: list[dict] = []
    for _md5, (incoming, incoming_score) in folded.items():
        final_rows.append({**record_to_params(incoming), "quality_score": incoming_score})

    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(_UPSERT_SQL, final_rows)

    stats.inserted += len(final_rows)
    stats.batches += 1


class _CountingFile:
    """Thin wrapper counting compressed bytes read from the payload file."""

    def __init__(self, file_obj):
        self._file = file_obj
        self.bytes_read = 0

    def read(self, size=-1):
        data = self._file.read(size)
        self.bytes_read += len(data)
        return data

    def __getattr__(self, name):  # pragma: no cover - delegation
        return getattr(self._file, name)


def iter_jsonl(payload_path: Path, on_bytes=None):
    """Stream-decompress a .jsonl(.seekable).zst file line by line.

    `on_bytes(compressed_bytes_read)` is invoked after every chunk pulled from
    the underlying file (used for live import-progress reporting).
    """
    with open(payload_path, "rb") as raw_file:
        counting = _CountingFile(raw_file)
        decompressor = zstandard.ZstdDecompressor(max_window_size=2**27)
        stream = decompressor.stream_reader(counting)
        text_stream = io.TextIOWrapper(stream, encoding="utf-8", errors="replace")
        for line in text_stream:
            if on_bytes is not None:
                on_bytes(counting.bytes_read)
            line = line.strip()
            if line:
                yield line


def _log_error_samples(payload_path: Path, stats: ImportStats, max_samples: int = 5) -> None:
    """Log a few representative parse-failure samples (release-level diagnostics)."""
    if stats.failed == 0:
        return
    logger.error(
        "Import failures in %s (%d failed): %d sample(s): %s",
        payload_path.name[:60],
        stats.failed,
        min(max_samples, len(stats.error_samples)),
        " || ".join(stats.error_samples[:max_samples]),
    )


def import_release(
    conn: psycopg.Connection,
    collection: str,
    payload_path: Path,
    release_id: int,
    settings: Settings | None = None,
) -> ImportStats:
    """Import one release payload with per-batch commits and error-rate abort."""
    settings = settings or load_settings()
    adapter = get_adapter(collection)
    stats = ImportStats()
    batch_size = max(1, settings.sync_batch_size)
    batch: list[tuple[NormalizedRecord, str]] = []

    state.set_release_status(conn, release_id, "importing")
    logger.info("Importing %s from %s", collection, payload_path.name)

    flushed = {"seen": 0, "inserted": 0, "updated": 0, "skipped": 0, "discarded": 0, "failed": 0}

    def flush() -> None:
        nonlocal batch
        if not batch:
            return
        process_batch(conn, batch, stats)
        state.update_release_counters(
            conn,
            release_id,
            seen=stats.seen - flushed["seen"],
            inserted=stats.inserted - flushed["inserted"],
            updated=stats.updated - flushed["updated"],
            skipped=stats.skipped - flushed["skipped"],
            discarded=stats.discarded - flushed["discarded"],
            failed=stats.failed - flushed["failed"],
        )
        for key in flushed:
            flushed[key] = getattr(stats, key)
        batch = []

    # Live import progress: report compressed-byte position, throttled.
    total_bytes = payload_path.stat().st_size
    last_progress = 0.0

    def on_bytes(done: int) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress < 3.0:
            return
        last_progress = now
        try:
            state.update_import_progress(conn, release_id, min(done, total_bytes), total_bytes)
        except Exception:  # noqa: BLE001 - progress reporting must never kill imports
            pass

    state.update_import_progress(conn, release_id, 0, total_bytes)

    for line in iter_jsonl(payload_path, on_bytes=on_bytes):
        if _shutdown_requested:
            flush()
            raise GracefulShutdown("SIGTERM received during import")

        stats.seen += 1
        try:
            raw = json.loads(line)
            parsed = adapter.parse(raw)
            if not parsed:
                stats.skipped += 1
            else:
                for record in parsed:
                    if record.discarded:
                        stats.discarded += 1
                    else:
                        batch.append((record, line))
        except Exception as error:  # noqa: BLE001 - single broken record must not kill import
            stats.failed += 1
            if len(stats.error_samples) < 20:
                stats.error_samples.append(f"{type(error).__name__}: {error} :: {line[:200]}")

        if len(batch) >= batch_size:
            flush()
            if stats.seen >= 10000 and stats.failed / max(stats.seen, 1) > settings.sync_error_abort_rate:
                flush()
                _log_error_samples(payload_path, stats)
                raise RuntimeError(
                    f"Error rate {stats.failed}/{stats.seen} exceeds threshold "
                    f"{settings.sync_error_abort_rate:.2%}; aborting release import."
                )

    flush()
    state.update_import_progress(conn, release_id, total_bytes, total_bytes)

    failure_rate = stats.failed / max(stats.seen, 1)
    if stats.seen == 0:
        raise RuntimeError("Payload contained no records; refusing to mark completed.")
    if failure_rate > settings.sync_error_abort_rate:
        _log_error_samples(payload_path, stats)
        raise RuntimeError(
            f"Final error rate {failure_rate:.2%} exceeds threshold {settings.sync_error_abort_rate:.2%}."
        )
    return stats


def validate_import(conn: psycopg.Connection, release_id: int, stats: ImportStats) -> None:
    """Post-import sanity gate before a release may be marked completed."""
    state.set_release_status(conn, release_id, "validating")
    if stats.seen == 0:
        raise RuntimeError("Validation failed: no records seen.")
    if stats.inserted + stats.updated == 0 and stats.skipped == stats.seen:
        # Fully idempotent re-import of an identical payload is legitimate.
        logger.info("All %d records were already present (skipped).", stats.skipped)


def delete_payload(payload_path: Path) -> None:
    try:
        if payload_path.exists():
            payload_path.unlink()
            logger.info("Deleted temporary payload %s", payload_path)
    except OSError as error:  # pragma: no cover - filesystem dependent
        logger.warning("Could not delete payload %s: %s", payload_path, error)


def hexdump_md5(md5: bytes) -> str:  # pragma: no cover - debug helper
    return md5_to_hex(md5)
