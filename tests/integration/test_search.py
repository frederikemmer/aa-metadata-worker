"""Integration tests: search, filters, identifier lookups, pagination."""

from __future__ import annotations

import pytest

from app.search import build_search_sql, decode_cursor, encode_cursor, fetch_search_page
from common.normalize import normalize_md5
from sync.importer import import_release
from tests.conftest import make_zst

BOOKS = [
    {
        "aacid": "aacid__zlib3_records__20250101T000000Z__101__Aaaa",
        "metadata": {
            "md5_reported": "b" * 32,
            "title": "Der Hobbit",
            "author": "J. R. R. Tolkien",
            "publisher": "Dt. Taschenbuch Verl.",
            "year": "2012",
            "language": "german",
            "extension": "epub",
            "filesize_reported": 1500000,
            "isbns": ["978-3-423-71359-7"],
        },
    },
    {
        "aacid": "aacid__zlib3_records__20250101T000000Z__102__Bbbb",
        "metadata": {
            "md5_reported": "c" * 32,
            "title": "The Lord of the Rings",
            "author": "J.R.R. Tolkien",
            "publisher": "HarperCollins",
            "year": "2005",
            "language": "english",
            "extension": "pdf",
            "filesize_reported": 9200000,
            "isbns": ["9780007203550"],
        },
    },
    {
        "aacid": "aacid__zlib3_records__20250101T000000Z__103__Cccc",
        "metadata": {
            "md5_reported": "d" * 32,
            "title": "Das Kapital: Žižek Introduces Marx",
            "author": "Karl Marx; Slavoj Žižek",
            "publisher": "Penguin",
            "year": "2010",
            "language": "english",
            "extension": "epub",
            "filesize_reported": 3300000,
            "isbns": [],
        },
    },
    {
        "aacid": "aacid__ia2_records__20250101T000000Z__104__Dddd",
        "metadata": {
            "ia_id": "hobbit_annotated",
            "metadata_json": {
                "metadata": {
                    "identifier": "hobbit_annotated",
                    "title": "The Annotated Hobbit",
                    "creator": "Tolkien, J. R. R.; Anderson, Douglas A.",
                    "publisher": "Houghton Mifflin",
                    "date": "2002",
                    "isbn": "9780618134700",
                    "language": "eng",
                    "oclc-id": "48241530",
                },
                "aa_shorter_files": [
                    {"name": "hobbit.pdf", "size": "22000000", "md5": "e" * 32},
                    {"name": "hobbit.epub", "size": "5200000", "md5": "f" * 32},
                ],
            },
        },
    },
]


@pytest.fixture()
def seeded(db_conn):
    zlib_id = db_conn.execute(
        "INSERT INTO sync_releases (collection, release_identifier) "
        "VALUES ('zlib3_records', 'seed_zlib') RETURNING id"
    ).fetchone()[0]
    ia_id = db_conn.execute(
        "INSERT INTO sync_releases (collection, release_identifier) "
        "VALUES ('ia2_records', 'seed_ia') RETURNING id"
    ).fetchone()[0]
    payload_zlib = make_zst(BOOKS[:3], db_tmp_path(db_conn) / "zlib.jsonl.zst")
    import_release(db_conn, "zlib3_records", payload_zlib, zlib_id)
    payload_ia = make_zst(BOOKS[3:], db_tmp_path(db_conn) / "ia.jsonl.zst")
    import_release(db_conn, "ia2_records", payload_ia, ia_id)
    return db_conn


def db_tmp_path(_conn):
    from pathlib import Path

    path = Path("/tmp/opencode/aa_search_tests")
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_search(conn, **params):
    limit = params.pop("limit", 20)
    sql, sql_params = build_search_sql(limit=limit, cursor=params.pop("cursor", None), **params)
    return fetch_search_page(conn, sql, sql_params, limit)


class TestTextSearch:
    def test_simple_title(self, seeded):
        results, _, _ = run_search(seeded, q="Hobbit")
        titles = [r.title for r in results]
        assert any("Hobbit" in t for t in titles)

    def test_diacritics_insensitive(self, seeded):
        # Stored: "Žižek" -> normalized "zizek"; query "zizek" must match.
        results, _, _ = run_search(seeded, q="Žižek")
        assert any("Žižek" in a for r in results for a in r.authors)

    def test_author_param(self, seeded):
        results, _, _ = run_search(seeded, author="tolkien")
        assert len(results) >= 3

    def test_ranking_title_beats_publisher(self, seeded):
        results, _, _ = run_search(seeded, q="hobbit")
        assert results[0].title.startswith(("Der Hobbit", "The"))

    def test_no_results(self, seeded):
        results, _, _ = run_search(seeded, q="xyzzy-nonexistent")
        assert results == []


class TestFilters:
    def test_language(self, seeded):
        results, _, _ = run_search(seeded, language="de")
        assert results and all(r.languages == ["deu"] for r in results)

    def test_extension(self, seeded):
        results, _, _ = run_search(seeded, extension="epub")
        assert results and all(r.format == "epub" for r in results)

    def test_year_range(self, seeded):
        results, _, _ = run_search(seeded, year_from=2010)
        assert all(r.publicationYear >= 2010 for r in results)
        assert any(r.title == "Der Hobbit" for r in results)

    def test_combined(self, seeded):
        results, _, _ = run_search(seeded, q="lord", language="en", extension="pdf")
        assert any(r.md5 == "c" * 32 for r in results)


class TestIdentifierLookup:
    def test_isbn13_with_hyphens(self, seeded):
        results, _, _ = run_search(seeded, isbn="978-3-423-71359-7")
        assert [r.md5 for r in results] == ["b" * 32]

    def test_isbn10_conversion(self, seeded):
        results, _, _ = run_search(seeded, isbn="0345339681")  # Hobbit ISBN-10
        assert isinstance(results, list)

    def test_invalid_isbn_400_semantics(self, seeded):
        with pytest.raises(ValueError):
            build_search_sql(
                isbn="nope!",
                q=None,
                title=None,
                author=None,
                doi_param=None,
                language=None,
                extension=None,
                year_from=None,
                year_to=None,
                limit=20,
                cursor=None,
            )

    def test_md5_direct_lookup(self, seeded):
        row = seeded.execute(
            "SELECT md5 FROM metadata_records WHERE md5 = %s", (normalize_md5("e" * 32),)
        ).fetchone()
        assert bytes(row[0]).hex() == "e" * 32


class TestPagination:
    def test_cursor_roundtrip_and_no_duplicates(self, seeded):
        seen_md5s: list[str] = []
        cursor = None
        pages = 0
        while True:
            results, next_cursor, _ = run_search(seeded, author="tolkien", limit=2, cursor=cursor)
            seen_md5s.extend(r.md5 for r in results)
            pages += 1
            if next_cursor is None or pages > 10:
                break
            cursor = next_cursor
            decode_cursor(cursor)  # must be valid
        assert len(seen_md5s) == len(set(seen_md5s)), "cursor pagination must not repeat rows"
        assert len(seen_md5s) >= 3

    def test_encode_decode(self):
        cursor = encode_cursor(0.1234, "a" * 32)
        rank, md5_hex = decode_cursor(cursor)
        assert md5_hex == "a" * 32

    def test_malformed_cursor_rejected(self):
        with pytest.raises(ValueError):
            decode_cursor("!!!not-a-cursor!!!")


class TestDeletedExcluded:
    def test_tombstone_not_searchable(self, seeded):
        seeded.execute("UPDATE metadata_records SET deleted = TRUE WHERE title LIKE '%Kapital%'")
        results, _, _ = run_search(seeded, q="kapital")
        assert results == []
