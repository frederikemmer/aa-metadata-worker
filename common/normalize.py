"""Deterministic normalization shared by import pipeline and API query parsing.

Indexing and querying MUST use the exact same functions so that e.g. a search
for "Zizek" finds "Žižek". Normalization therefore lives only here (Python) and
normalized values are persisted in the database; PostgreSQL text search uses the
'simple' configuration over the already-normalized columns.
"""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")
_DOI_STRIP_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:|info:doi/)", re.IGNORECASE)
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_YEAR_RE = re.compile(r"(\d{4})")

# Known book formats; anything else is kept only if it is a plausible file extension.
KNOWN_FORMATS = {"epub", "pdf", "mobi", "azw3", "djvu", "fb2", "cbz", "cbr", "txt", "html", "rtf"}
_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")


def normalize_text(value: str | None) -> str:
    """Unicode-normalize, strip diacritics, casefold, collapse whitespace.

    "Žižek" -> "zizek", "Úroda" -> "uroda".
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return _WS_RE.sub(" ", text).strip().casefold()


def normalize_md5(value: str | None) -> bytes | None:
    """Return 16 raw bytes for a valid lowercase/uppercase hex md5, else None."""
    if not value:
        return None
    hexed = value.strip().lower()
    if not _MD5_RE.match(hexed):
        return None
    return bytes.fromhex(hexed)


def md5_to_hex(md5: bytes) -> str:
    return md5.hex()


def _isbn10_valid(isbn: str) -> bool:
    if len(isbn) != 10 or not all(ch.isdigit() or (ch == "X" and isbn.endswith("X")) for ch in isbn):
        return False
    # Weights 10..1; 'X' counts as 10.
    total = sum((10 - i) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(isbn))
    return total % 11 == 0


def _isbn13_valid(isbn: str) -> bool:
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    total = sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(isbn))
    return total % 10 == 0


def _isbn10_to_13(isbn10: str) -> str | None:
    if not _isbn10_valid(isbn10):
        return None
    core = "978" + isbn10[:9]
    check = (10 - sum(int(ch) * (1 if i % 2 == 0 else 3) for i, ch in enumerate(core)) % 10) % 10
    candidate = core + str(check)
    return candidate if _isbn13_valid(candidate) else None


def normalize_isbn13(value: str | None) -> str | None:
    """Normalize to a checksum-valid ISBN-13 string, else None."""
    if not value:
        return None
    cleaned = re.sub(r"[\s\-‐-―]", "", value.strip()).upper()
    if _isbn13_valid(cleaned):
        return cleaned
    if len(cleaned) == 10:
        return _isbn10_to_13(cleaned)
    return None


def normalize_isbn10(value: str | None) -> str | None:
    """Normalize to a checksum-valid ISBN-10 string, else None."""
    if not value:
        return None
    cleaned = re.sub(r"[\s\-‐-―]", "", value.strip()).upper()
    return cleaned if _isbn10_valid(cleaned) else None


def looks_like_asin(value: str) -> bool:
    return bool(re.fullmatch(r"B[0-9A-Z]{9}", value))


def normalize_isbn_list(values: list[str] | None) -> tuple[list[str], list[str]]:
    """Split a raw list of identifier strings into (isbn13, isbn10).

    Invalid identifiers and ASINs are dropped. Duplicates removed, order stable.
    """
    isbn13: list[str] = []
    isbn10: list[str] = []
    for raw in values or []:
        if not isinstance(raw, str):
            continue
        cleaned = re.sub(r"[\s\-‐-―]", "", raw.strip()).upper()
        if not cleaned:
            continue
        as13 = normalize_isbn13(cleaned)
        if as13:
            if as13 not in isbn13:
                isbn13.append(as13)
            continue
        # Only try ISBN-10 when it cannot be an ISBN-13 and is not obviously an ASIN.
        if len(cleaned) == 10 and not looks_like_asin(cleaned):
            as10 = normalize_isbn10(cleaned)
            if as10 and as10 not in isbn10:
                isbn10.append(as10)
    return isbn13, isbn10


def normalize_doi(value: str | None) -> str | None:
    """Strip resolver prefixes, validate shape, lowercase the prefix part."""
    if not value:
        return None
    doi = value.strip()
    doi = _DOI_STRIP_RE.sub("", doi).strip()
    doi = doi.rstrip(".")
    if not _DOI_RE.match(doi):
        return None
    prefix_end = doi.index("/", doi.index("10."))
    return doi[:prefix_end].lower() + doi[prefix_end:]


def normalize_language(value: str | list | tuple | None) -> str | None:
    """Normalize any known language name/code to ISO 639-3 (FE.Library convention)."""
    from common.languages import language_to_iso639_3

    return language_to_iso639_3(value)


def normalize_languages(values) -> list[str]:
    result: list[str] = []
    if values is None:
        return result

    def add(raw) -> None:
        code = normalize_language(raw if isinstance(raw, str) else None)
        if code and code not in result:
            result.append(code)

    if isinstance(values, str):
        for part in re.split(r"[,;/]", values):
            add(part)
    elif isinstance(values, (list, tuple)):
        for item in values:
            add(item)
    return result


def normalize_extension(value: str | None) -> str | None:
    """Lowercase plausible file extensions; reject anything exotic."""
    if not value:
        return None
    ext = value.strip().lower().lstrip(".")
    if not _EXT_RE.match(ext):
        return None
    return ext


def normalize_year(value) -> int | None:
    """Extract a plausible publication year (1000..2100) from messy input."""
    if value is None:
        return None
    match = _YEAR_RE.search(str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1000 <= year <= 2100 else None


def split_authors(value: str | None) -> list[str]:
    """Split common author field conventions into a clean author list.

    A plain comma does NOT split (protects "Last, First"); a spaced comma,
    semicolon, pipe, ampersand, or the word "and" between whitespace splits.
    """
    if not value:
        return []
    parts = re.split(r"\s*[;|]\s*|\s+,\s*|\s*&\s*|\s+\band\b\s+", value.strip())
    authors: list[str] = []
    for part in parts:
        part = _WS_RE.sub(" ", part).strip(" ,;")
        if part:
            authors.append(part)
    return authors


def derive_work_key(
    isbn13: list[str], isbn10: list[str], doi: list[str], openlibrary_ids: list[str]
) -> str | None:
    """Deterministic logical-work key from the most reliable identifier present.

    Priority: ISBN-13 > ISBN-10 > DOI > OpenLibrary ID. Title+author similarity
    is deliberately NOT used as identity (see docs/data-model.md).
    """
    if isbn13:
        return f"isbn:{isbn13[0]}"
    if isbn10:
        return f"isbn:{isbn10[0]}"
    if doi:
        return f"doi:{doi[0]}"
    if openlibrary_ids:
        return f"ol:{openlibrary_ids[0]}"
    return None
