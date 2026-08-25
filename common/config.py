"""Shared configuration for api and sync services, read from environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # PostgreSQL
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "aa_metadata"
    postgres_user: str = "aa_metadata"
    postgres_password: str = ""

    # API
    api_port: int = 8010
    api_key: str = ""
    api_statement_timeout_ms: int = 5000

    # Sync / collections. Order = processing order; the big upload_records
    # download deliberately runs last so small enrichment collections first.
    aa_collections: list[str] = field(
        default_factory=lambda: [
            "zlib3_records",
            "ia2_records",
            "goodreads_records",
            "gbooks_records",
            "libby_records",
            "upload_records",
        ]
    )
    aa_mirror_base_url: str = "https://annas-archive.gd"
    sync_enabled: bool = True
    sync_schedule: str = "03:15"  # HH:MM Europe/Berlin
    sync_batch_size: int = 20000
    sync_error_abort_rate: float = 0.02  # abort release import above 2% record failures

    # Book-only filtering for upload_records / ia2_records.
    upload_blocked_subcollections: list[str] = field(
        default_factory=lambda: [
            "academia_edu",
            "us_gov_tech_reports",
            "wikilib",
            "aaaaarg",
            "magzdb",
        ]
    )
    upload_require_title_author: bool = True  # drop uploads without real title+author
    ia_require_texts: bool = True  # keep only IA items with mediatype "texts"
    gbooks_require_books: bool = True  # drop Google Books magazines (printType != BOOK)
    libby_allowed_types: list[str] = field(
        default_factory=lambda: ["ebook", "audiobook"]
    )  # Libby media types kept in the index

    # Reuse the previous payload as torrent seed base so incremental updates only
    # download changed pieces (AAC cumulative releases share identical prefixes).
    sync_reuse_prev_payload: bool = True

    # Storage guard (GiB). Defaults sized for NAS deployment (see docs/sync.md).
    storage_warn_gib: int = 300
    storage_stop_gib: int = 400

    # Misc
    log_level: str = "INFO"
    tz: str = "Europe/Berlin"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"host={self.postgres_host} port={self.postgres_port} "
            f"dbname={self.postgres_db} user={self.postgres_user} password={self.postgres_password}"
        )


def load_settings() -> Settings:
    collections = [c.strip() for c in os.environ.get("AA_COLLECTIONS", "").split(",") if c.strip()]
    return Settings(
        postgres_host=os.environ.get("POSTGRES_HOST", "postgres"),
        postgres_port=_env_int("POSTGRES_PORT", 5432),
        postgres_db=os.environ.get("POSTGRES_DB", "aa_metadata"),
        postgres_user=os.environ.get("POSTGRES_USER", "aa_metadata"),
        postgres_password=os.environ.get("POSTGRES_PASSWORD", ""),
        api_port=_env_int("API_PORT", 8010),
        api_key=os.environ.get("METADATA_API_KEY", ""),
        api_statement_timeout_ms=_env_int("API_STATEMENT_TIMEOUT_MS", 5000),
        aa_collections=collections
        or [
            "zlib3_records",
            "ia2_records",
            "goodreads_records",
            "gbooks_records",
            "libby_records",
            "upload_records",
        ],
        aa_mirror_base_url=os.environ.get("AA_MIRROR_BASE_URL", "https://annas-archive.gd"),
        sync_enabled=_env_bool("SYNC_ENABLED", True),
        sync_schedule=os.environ.get("SYNC_SCHEDULE", "03:15"),
        sync_batch_size=_env_int("SYNC_BATCH_SIZE", 20000),
        sync_error_abort_rate=_env_float("SYNC_ERROR_ABORT_RATE", 0.02),
        upload_blocked_subcollections=_env_list(
            "AA_UPLOAD_BLOCKED_SUBCOLLECTIONS",
            ["academia_edu", "us_gov_tech_reports", "wikilib", "aaaaarg", "magzdb"],
        ),
        upload_require_title_author=_env_bool("AA_UPLOAD_REQUIRE_TITLE_AUTHOR", True),
        ia_require_texts=_env_bool("AA_IA_REQUIRE_TEXTS", True),
        gbooks_require_books=_env_bool("AA_GBOOKS_REQUIRE_BOOKS", True),
        libby_allowed_types=_env_list("AA_LIBBY_ALLOWED_TYPES", ["ebook", "audiobook"]),
        sync_reuse_prev_payload=_env_bool("SYNC_REUSE_PREV_PAYLOAD", True),
        storage_warn_gib=_env_int("STORAGE_WARN_GIB", 300),
        storage_stop_gib=_env_int("STORAGE_STOP_GIB", 400),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        tz=os.environ.get("TZ", "Europe/Berlin"),
    )
