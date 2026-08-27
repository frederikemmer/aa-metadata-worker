"""Single record lookup + source references endpoints."""

from __future__ import annotations

import os
import re
from urllib.parse import quote

import psycopg
from fastapi import APIRouter, HTTPException

from app.deps import GetConnectionDependency, GetSettingsDependency
from app.schemas import RecordResponse, RecordSourcesResponse, SourceInfo
from app.search import row_to_record_response
from common.config import Settings

router = APIRouter(prefix="/api/v1", tags=["records"])

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_AA_BASE_URL = os.environ.get("AA_PUBLIC_BASE_URL", "https://annas-archive.org")

# Collections whose MD5s correspond to files hosted by Anna's Archive for fast_download.
_FAST_DOWNLOAD_COLLECTIONS = frozenset({"zlib3_records", "upload_records"})


@router.get("/records/{md5}", response_model=RecordResponse)
def get_record(md5: str, conn: psycopg.Connection = GetConnectionDependency) -> RecordResponse:
    md5_lower = md5.strip().lower()
    if not _MD5_RE.match(md5_lower):
        raise HTTPException(status_code=400, detail="Invalid MD5: expected 32 hex characters")
    row = conn.execute(
        """
        SELECT md5, title, authors, publisher, publication_year, languages, extension,
               filesize, isbn10, isbn13, doi, oclc, openlibrary_ids, work_key,
               series_name, series_position, edition,
               source_collection, source_record_id, aacid, 0::float8 AS rank,
               1::bigint AS edition_count
        FROM metadata_records WHERE md5 = %s
        """,
        (bytes.fromhex(md5_lower),),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return row_to_record_response(row)


@router.get("/records/{md5}/sources", response_model=RecordSourcesResponse)
def get_record_sources(
    md5: str,
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> RecordSourcesResponse:
    """Reference information so clients can obtain the file themselves.

    This service never downloads, hosts or proxies book files. It only returns
    stable public identifiers (AA page URL, IPFS CID when known) and optionally
    a fast_download URL when the record's MD5 matches a hosted file collection.
    """
    md5_lower = md5.strip().lower()
    if not _MD5_RE.match(md5_lower):
        raise HTTPException(status_code=400, detail="Invalid MD5: expected 32 hex characters")
    row = conn.execute(
        """
        SELECT md5, source_collection, source_record_id, aacid, ipfs_cid, deleted
        FROM metadata_records WHERE md5 = %s
        """,
        (bytes.fromhex(md5_lower),),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Record not found")
    if row[5]:
        raise HTTPException(status_code=410, detail="Record was removed at the source")

    md5_hex = bytes(row[0]).hex()
    source_collection = row[1]

    # Build fast_download URL only when:
    # 1. A membership key is configured
    # 2. The record's collection is known to host files on AA's download servers
    fast_download_url = None
    if settings.aa_fast_download_key and source_collection in _FAST_DOWNLOAD_COLLECTIONS:
        fast_download_url = (
            f"{settings.aa_mirror_base_url}/dyn/api/fast_download.json"
            f"?md5={quote(md5_hex)}"
            f"&key={quote(settings.aa_fast_download_key)}"
        )

    return RecordSourcesResponse(
        md5=md5_hex,
        aaPageUrl=f"{_AA_BASE_URL}/md5/{md5_hex}",
        fastDownloadUrl=fast_download_url,
        ipfsCid=row[4],
        source=SourceInfo(collection=source_collection, record_id=row[2], aacid=row[3]),
    )
