"""Single record lookup + source references endpoints."""

from __future__ import annotations

import os
import re

import psycopg
from fastapi import APIRouter, HTTPException

from app.deps import GetConnectionDependency
from app.schemas import RecordResponse, RecordSourcesResponse, SourceInfo
from app.search import row_to_record_response

router = APIRouter(prefix="/api/v1", tags=["records"])

_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_AA_BASE_URL = os.environ.get("AA_PUBLIC_BASE_URL", "https://annas-archive.org")


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
def get_record_sources(md5: str, conn: psycopg.Connection = GetConnectionDependency) -> RecordSourcesResponse:
    """Reference information so clients can obtain the file themselves.

    This service never downloads, hosts or proxies book files. It only returns
    stable public identifiers (AA page URL, IPFS CID when known).
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
    return RecordSourcesResponse(
        md5=bytes(row[0]).hex(),
        aaPageUrl=f"{_AA_BASE_URL}/md5/{bytes(row[0]).hex()}",
        ipfsCid=row[4],
        source=SourceInfo(collection=row[1], record_id=row[2], aacid=row[3]),
    )
