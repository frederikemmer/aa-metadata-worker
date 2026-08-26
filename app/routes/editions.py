"""Editions endpoint: list all versions of a book by work_key.

Groups records by their work_key and returns them sorted by quality score
descending so the best-available version appears first.
"""

from __future__ import annotations

import re

import psycopg
from fastapi import APIRouter, HTTPException

from app.deps import GetConnectionDependency
from app.schemas import EditionResponse
from app.search import row_to_record_response

router = APIRouter(prefix="/api/v1", tags=["editions"])

_WORK_KEY_RE = re.compile(r"^(isbn|doi|ol):.+")


@router.get("/editions/{work_key:path}", response_model=EditionResponse)
def get_editions(
    work_key: str,
    conn: psycopg.Connection = GetConnectionDependency,
) -> EditionResponse:
    work_key = work_key.strip()
    if not work_key or not _WORK_KEY_RE.match(work_key):
        raise HTTPException(
            status_code=400,
            detail="Invalid work_key: expected format 'isbn:…', 'doi:…', or 'ol:…'",
        )

    rows = conn.execute(
        """
        SELECT md5, title, authors, publisher, publication_year, languages, extension,
               filesize, isbn10, isbn13, doi, oclc, openlibrary_ids, work_key,
               series_name, series_position, edition,
               source_collection, source_record_id, aacid, 0::float8 AS rank,
               COUNT(*) OVER () AS edition_count
        FROM metadata_records
        WHERE NOT deleted AND work_key = %s
        ORDER BY quality_score DESC, md5 ASC
        """,
        (work_key,),
    ).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="No editions found for this work_key")

    editions = [row_to_record_response(row) for row in rows]
    return EditionResponse(
        workKey=work_key,
        totalEditions=len(editions),
        editions=editions,
    )
