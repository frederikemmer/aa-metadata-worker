"""Unit tests for normalization (md5, ISBN, DOI, language, format, text)."""

import pytest

from common.normalize import (
    derive_work_key,
    normalize_doi,
    normalize_extension,
    normalize_isbn10,
    normalize_isbn13,
    normalize_isbn_list,
    normalize_language,
    normalize_md5,
    normalize_text,
    normalize_year,
    split_authors,
)


class TestNormalizeText:
    def test_diakritika(self):
        assert normalize_text("Žižek") == "zizek"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Harry  Potter", "harry potter"),
            ("ÚRODA", "uroda"),
            ("ÜBER", "uber"),
            ("Åse", "ase"),
            ("Дюна", "дюна"),  # cyrillic preserved
            (None, ""),
            ("", ""),
        ],
    )
    def test_variants(self, raw, expected):
        assert normalize_text(raw) == expected

    def test_idempotent(self):
        once = normalize_text("Grüße aus Köln")
        twice = normalize_text(once)
        assert once == twice


class TestMd5:
    def test_valid(self):
        assert normalize_md5("0123456789abcdef0123456789ABCDEF") == bytes.fromhex(
            "0123456789abcdef0123456789abcdef"
        )

    @pytest.mark.parametrize("bad", [None, "", "xyz", "0123", "g" * 32, "0" * 31])
    def test_invalid(self, bad):
        assert normalize_md5(bad) is None


class TestIsbn13:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("978-3-16-148410-0", "9783161484100"),
            ("978 3 16 148410 0", "9783161484100"),
            ("9783161484100", "9783161484100"),
            # ISBN-10 input converted to ISBN-13
            ("0-306-40615-2", "9780306406157"),
        ],
    )
    def test_valid(self, raw, expected):
        assert normalize_isbn13(raw) == expected

    @pytest.mark.parametrize("raw", ["9783161484101", "123", "", None, "abcdefghij"])
    def test_invalid(self, raw):
        assert normalize_isbn13(raw) is None


class TestIsbn10:
    @pytest.mark.parametrize("raw,expected", [("0-306-40615-2", "0306406152"), ("080442957X", "080442957X")])
    def test_valid(self, raw, expected):
        assert normalize_isbn10(raw) == expected

    @pytest.mark.parametrize("raw", ["0306406153", "12345", None])
    def test_invalid(self, raw):
        assert normalize_isbn10(raw) is None


class TestIsbnList:
    def test_filters_asins(self):
        isbn13, isbn10 = normalize_isbn_list(["B0B6HNHVV9", "978-3-16-148410-0"])
        assert isbn13 == ["9783161484100"]
        assert isbn10 == []

    def test_mixed_and_dedup(self):
        isbn13, isbn10 = normalize_isbn_list(["0306406152", "0306406152", "9780306406157"])
        assert isbn13 == ["9780306406157"]  # ISBN-10 converted, deduped against explicit
        assert isbn10 == []


class TestDoi:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("10.1000/182", "10.1000/182"),
            ("https://doi.org/10.1000/182", "10.1000/182"),
            ("http://dx.doi.org/10.1000/182", "10.1000/182"),
            ("doi:10.1000/182", "10.1000/182"),
            ("info:doi/10.1000/182", "10.1000/182"),
            ("10.1000/journal.2016.01", "10.1000/journal.2016.01"),
        ],
    )
    def test_valid(self, raw, expected):
        assert normalize_doi(raw) == expected

    @pytest.mark.parametrize("raw", ["notadoi", "11.1000/182", "", None, "10.abc"])
    def test_invalid(self, raw):
        assert normalize_doi(raw) is None


class TestLanguage:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("german", "deu"),
            ("de", "deu"),
            ("deu", "deu"),
            ("ger", "deu"),
            ("en-us", "eng"),
            ("english", "eng"),
            ("russian", "rus"),
            ("zho", "zho"),
        ],
    )
    def test_mapping(self, raw, expected):
        assert normalize_language(raw) == expected

    def test_unknown_three_letter_passthrough(self):
        assert normalize_language("xyz") == "xyz"

    @pytest.mark.parametrize("raw", ["", None, "d", "unknownlanguage"])
    def test_invalid(self, raw):
        assert normalize_language(raw) is None


class TestExtension:
    @pytest.mark.parametrize("raw,expected", [("EPUB", "epub"), (".pdf", "pdf"), ("azw3", "azw3")])
    def test_valid(self, raw, expected):
        assert normalize_extension(raw) == expected

    @pytest.mark.parametrize("raw", ["", None, "a" * 20, "p df!", "../../etc"])
    def test_invalid(self, raw):
        assert normalize_extension(raw) is None


class TestYear:
    @pytest.mark.parametrize(
        "raw,expected",
        [("2020", 2020), ("published in 1999", 1999), ("2008:05:06", 2008), ("99", None), (None, None)],
    )
    def test_variants(self, raw, expected):
        assert normalize_year(raw) == expected


class TestAuthors:
    def test_semicolon_split(self):
        assert split_authors("West, Annika; Doe, John") == ["West, Annika", "Doe, John"]

    def test_and_split(self):
        assert split_authors("John Smith and Jane Doe") == ["John Smith", "Jane Doe"]

    def test_plain_comma_not_split(self):
        assert split_authors("Jaeger, Werner Wilhelm") == ["Jaeger, Werner Wilhelm"]

    def test_empty(self):
        assert split_authors("") == []
        assert split_authors(None) == []


class TestWorkKey:
    def test_priority_isbn13_first(self):
        key = derive_work_key(["9783161484100"], ["0306406152"], ["10.1000/182"], ["OL1M"])
        assert key == "isbn:9783161484100"

    def test_fallback_chain(self):
        assert derive_work_key([], ["0306406152"], ["10.1000/182"], []) == "isbn:0306406152"
        assert derive_work_key([], [], ["10.1000/182"], ["OL1M"]) == "doi:10.1000/182"
        assert derive_work_key([], [], [], ["OL1M"]) == "ol:OL1M"
        assert derive_work_key([], [], [], []) is None
