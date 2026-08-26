"""Search endpoint with validated parameters and keyset pagination."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, HTTPException, Query

from app.deps import GetConnectionDependency
from app.schemas import SearchResponse
from app.search import build_search_sql, fetch_search_page

router = APIRouter(prefix="/api/v1", tags=["search"])

MAX_QUERY_LENGTH = 200


@router.get("/search", response_model=SearchResponse)
def search(
    conn: psycopg.Connection = GetConnectionDependency,
    q: str | None = Query(default=None, max_length=MAX_QUERY_LENGTH, description="Free text search"),
    title: str | None = Query(default=None, max_length=MAX_QUERY_LENGTH),
    author: str | None = Query(default=None, max_length=MAX_QUERY_LENGTH),
    isbn: str | None = Query(default=None, max_length=20),
    doi: str | None = Query(default=None, max_length=100),
    language: str | None = Query(default=None, max_length=30),
    series: str | None = Query(
        default=None, max_length=MAX_QUERY_LENGTH, description="Series name filter"
    ),
    series_position: int | None = Query(
        default=None, ge=1, le=9999, description="Series position (volume number)"
    ),
    extension: str | None = Query(default=None, max_length=10),
    year_from: int | None = Query(default=None, ge=1000, le=2100),
    year_to: int | None = Query(default=None, ge=1000, le=2100),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=256),
) -> SearchResponse:
    if not any([q, title, author, isbn, doi, language, series, extension, year_from, year_to]):
        raise HTTPException(status_code=400, detail="At least one search parameter is required")
    if year_from and year_to and year_from > year_to:
        raise HTTPException(status_code=400, detail="year_from must be <= year_to")

    try:
        sql, params = build_search_sql(
            q=q,
            title=title,
            author=author,
            isbn=isbn,
            doi_param=doi,
            language=language,
            series=series,
            series_position=series_position,
            extension=extension,
            year_from=year_from,
            year_to=year_to,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    try:
        results, next_cursor, total_lower_bound = fetch_search_page(conn, sql, params, limit)
    except psycopg.errors.SyntaxError as error:
        raise HTTPException(status_code=400, detail="Invalid query syntax") from error
    return SearchResponse(
        totalLowerBound=total_lower_bound,
        limit=limit,
        nextCursor=next_cursor,
        results=results,
    )
