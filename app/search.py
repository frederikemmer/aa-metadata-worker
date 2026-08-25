"""Row -> API model mapping and search query building.

Search uses PostgreSQL FTS over pre-normalized values ('simple' config) plus
structured identifier/array lookups. Pagination is keyset-based on
(rank, md5) so no large SQL OFFSETs are ever used.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

import psycopg

from app.schemas import Identifiers, RecordResponse, SourceInfo
from common.normalize import (
    normalize_doi,
    normalize_isbn10,
    normalize_isbn13,
    normalize_language,
    normalize_text,
)

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CURSOR_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")

_BASE_SELECT = """
SELECT md5, title, authors, publisher, publication_year, languages, extension,
       filesize, isbn10, isbn13, doi, oclc, openlibrary_ids, work_key,
       source_collection, source_record_id, aacid, rank
FROM (
    SELECT md5, title, authors, publisher, publication_year, languages, extension,
           filesize, isbn10, isbn13, doi, oclc, openlibrary_ids, work_key,
           source_collection, source_record_id, aacid,
           COALESCE(ts_rank_cd(search_tsv, query, 32), 0) AS rank
    FROM metadata_records, {tsquery_join}
    WHERE NOT deleted AND (query IS NULL OR search_tsv @@ query) {extra_filters}
) candidates
"""


def build_tsquery_tokens(q: str) -> str | None:
    """'simple'-config to_tsquery expression with prefix matching per token.

    Input must already be normalize_text()-ed (identical to indexing).
    """
    tokens = _TOKEN_RE.findall(q)
    if not tokens:
        return None
    return " & ".join(f"{token}:*" for token in tokens)


def encode_cursor(rank: float, md5_hex: str) -> str:
    payload = json.dumps({"r": rank, "m": md5_hex}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[float, str]:
    if not cursor or not _CURSOR_RE.match(cursor) or len(cursor) > 200:
        raise ValueError("Malformed cursor")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        rank = float(payload["r"])
        md5_hex = str(payload["m"])
        if len(md5_hex) != 32 or not re.fullmatch(r"[0-9a-f]{32}", md5_hex):
            raise ValueError("Bad md5 in cursor")
        int(md5_hex, 16)
        return rank, md5_hex
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as error:
        raise ValueError("Malformed cursor") from error


def build_search_sql(
    *,
    q: str | None = None,
    title: str | None = None,
    author: str | None = None,
    isbn: str | None = None,
    doi_param: str | None = None,
    language: str | None = None,
    extension: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build parameterized keyset-paginated search SQL. Returns (sql, params)."""
    params: dict[str, Any] = {"limit": limit + 1}
    extra_filters = ""
    tsquery_join = "(SELECT websearch_to_tsquery('simple', %(q_norm)s) AS query)"
    params["q_norm"] = normalize_text(q or "")

    has_query = bool(q and q.strip())
    if not has_query:
        # Filter-only queries: rank is constant (0); order by md5 for stable keyset.
        tsquery_join = "(SELECT NULL::tsquery AS query)"

    if title:
        tokens = _TOKEN_RE.findall(normalize_text(title))
        if tokens:
            params["title_query"] = " & ".join(f"{t}:*" for t in tokens)
            extra_filters += " AND search_tsv @@ to_tsquery('simple', %(title_query)s)"

    if author:
        author_tokens: list[str] = []
        for token in _TOKEN_RE.findall(normalize_text(author)):
            if token not in author_tokens:
                author_tokens.append(token)
        if author_tokens:
            params["author_tokens"] = author_tokens
            extra_filters += " AND author_tokens @> %(author_tokens)s"

    if isbn:
        as13 = normalize_isbn13(isbn)
        as10 = normalize_isbn10(isbn.replace("-", "").replace(" ", ""))
        conditions = []
        if as13:
            params["isbn13"] = [as13]
            conditions.append("isbn13 @> %(isbn13)s")
        if as10:
            params["isbn10"] = [as10]
            conditions.append("isbn10 @> %(isbn10)s")
        if not conditions:
            raise ValueError("Invalid ISBN")
        extra_filters += " AND (" + " OR ".join(conditions) + ")"

    if doi_param:
        normalized_doi = normalize_doi(doi_param)
        if not normalized_doi:
            raise ValueError("Invalid DOI")
        params["doi"] = [normalized_doi]
        extra_filters += " AND doi @> %(doi)s"

    if language:
        lang_code = normalize_language(language)
        if not lang_code:
            raise ValueError("Invalid language")
        params["language"] = [lang_code]
        extra_filters += " AND languages @> %(language)s"

    if extension:
        if not re.fullmatch(r"[a-z0-9]{1,10}", extension.lower()):
            raise ValueError("Invalid extension")
        params["extension"] = extension.lower()
        extra_filters += " AND extension = %(extension)s"

    if year_from is not None:
        params["year_from"] = year_from
        extra_filters += " AND publication_year >= %(year_from)s"
    if year_to is not None:
        params["year_to"] = year_to
        extra_filters += " AND publication_year <= %(year_to)s"

    sql = _BASE_SELECT.format(tsquery_join=tsquery_join, extra_filters=extra_filters)

    if cursor is None:
        sql += "ORDER BY rank DESC, md5 ASC LIMIT %(limit)s"
    else:
        rank_value, md5_hex = decode_cursor(cursor)
        params["cur_rank"] = rank_value
        params["cur_md5"] = bytes.fromhex(md5_hex)
        # Keyset predicate matching ORDER BY rank DESC, md5 ASC.
        sql += (
            "WHERE (rank < %(cur_rank)s OR (rank = %(cur_rank)s AND md5 > %(cur_md5)s)) "
            "ORDER BY rank DESC, md5 ASC LIMIT %(limit)s"
        )
    return sql, params


def row_to_record_response(row: tuple) -> RecordResponse:
    (
        md5,
        title,
        authors,
        publisher,
        publication_year,
        languages,
        extension,
        filesize,
        isbn10,
        isbn13,
        doi,
        oclc,
        openlibrary_ids,
        work_key,
        source_collection,
        source_record_id,
        aacid,
        _rank,
    ) = row
    return RecordResponse(
        md5=bytes(md5).hex(),
        title=title or "",
        authors=list(authors or []),
        publisher=publisher,
        publicationYear=publication_year,
        languages=list(languages or []),
        format=extension,
        filesize=filesize,
        identifiers=Identifiers(
            isbn10=list(isbn10 or []),
            isbn13=list(isbn13 or []),
            doi=list(doi or []),
            oclc=list(oclc or []),
            openlibrary=list(openlibrary_ids or []),
        ),
        workKey=work_key,
        source=SourceInfo(
            collection=source_collection,
            record_id=source_record_id,
            aacid=aacid,
        ),
    )


def fetch_search_page(
    conn: psycopg.Connection,
    sql: str,
    params: dict[str, Any],
    limit: int,
) -> tuple[list[RecordResponse], str | None, int]:
    rows = conn.execute(sql, params).fetchall()
    total_lower_bound = len(rows)
    has_next = len(rows) > limit
    page_rows = rows[:limit]

    results = [row_to_record_response(row) for row in page_rows]
    next_cursor = None
    if has_next and results:
        last = page_rows[-1]
        last_rank = float(last[-1] or 0.0)
        next_cursor = encode_cursor(last_rank, bytes(last[0]).hex())
    return results, next_cursor, total_lower_bound
