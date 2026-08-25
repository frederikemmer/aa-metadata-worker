"""PostgreSQL connection handling and versioned SQL migrations.

Follows FE.Library's philosophy: schema changes only through ordered,
immutable migration files (migrations/NNN_name.sql), tracked in a
schema_migrations table. No ad-hoc DDL anywhere else.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from common.config import Settings, load_settings

log = logging.getLogger(__name__)

# PostgreSQL error code for query cancellation (statement_timeout).
_SQLSTATE_QUERY_CANCELED = "57014"

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

_pool: ConnectionPool | None = None


def connect(settings: Settings | None = None, autocommit: bool = True) -> psycopg.Connection:
    """Open a connection. Autocommit by default; explicit transactions are used
    where atomicity matters (migrations, batch upserts)."""
    settings = settings or load_settings()
    return psycopg.connect(settings.postgres_dsn, autocommit=autocommit)


def get_pool(settings: Settings | None = None) -> ConnectionPool:
    """Process-wide connection pool (API). Applies statement_timeout to sessions."""
    global _pool
    if _pool is None:
        settings = settings or load_settings()
        _pool = ConnectionPool(
            conninfo=settings.postgres_dsn,
            min_size=1,
            max_size=8,
            open=True,
            kwargs={
                "options": f"-c statement_timeout={settings.api_statement_timeout_ms}",
                "autocommit": True,
            },
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def approx_count(conn: psycopg.Connection, table: str) -> int:
    """Return approximate row count for *table*.

    Tries an exact ``SELECT COUNT(*)`` first.  If that hits the statement
    timeout (SQLSTATE 57014) we fall back to the planner's ``reltuples``
    estimate from ``pg_class`` which is always fast (~0 ms).
    """
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
        return int(row[0])  # type: ignore[index]
    except psycopg.errors.QueryCanceled:
        log.warning("COUNT(*) on %s hit statement_timeout – falling back to reltuples", table)
        row = conn.execute(
            "SELECT GREATEST(reltuples, 0)::bigint "
            "FROM pg_class WHERE oid = %s::regclass",
            (table,),
        ).fetchone()
        return int(row[0]) if row else 0  # type: ignore[index]


def list_migrations() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.iterdir()):
        match = _MIGRATION_RE.match(path.name)
        if match:
            found.append((int(match.group(1)), path))
    found.sort(key=lambda item: item[0])
    return found


def current_schema_version(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT CASE WHEN to_regclass('public.schema_migrations') IS NULL THEN 0 "
        "ELSE (SELECT MAX(version) FROM schema_migrations) END"
    ).fetchone()
    assert row is not None
    return int(row[0])


def apply_migrations(conn: psycopg.Connection) -> list[int]:
    """Apply pending migrations in order; returns versions applied this run."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    assert row is not None
    applied_max = int(row[0])

    done: list[int] = []
    for version, path in list_migrations():
        if version <= applied_max:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                (version, path.name),
            )
        done.append(version)
    return done
