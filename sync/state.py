"""Sync state persistence + server-side single-import locking."""

from __future__ import annotations

import psycopg

# Stable advisory-lock key so concurrent syncs (e.g. worker + manual CLI)
# are impossible even across container restarts.
ADVISORY_LOCK_KEY = 0x41414D45  # 'AAME'

RELEASE_STATUSES = (
    "discovered",
    "downloading",
    "importing",
    "validating",
    "completed",
    "failed",
    "blocked_storage",
)

COMMANDS = ("run_now", "pause", "resume")


class SyncLockBusy(RuntimeError):
    """Raised when another sync process holds the advisory lock."""


def enqueue_command(
    conn: psycopg.Connection,
    command: str,
    note: str | None = None,
) -> int:
    """Queue a control command for the sync worker; returns its id."""
    assert command in COMMANDS
    row = conn.execute(
        "INSERT INTO sync_commands (command, note) VALUES (%s, %s) RETURNING id",
        (command, note),
    ).fetchone()
    assert row is not None
    return int(row[0])


def pop_pending_commands(conn: psycopg.Connection) -> list[tuple[int, str]]:
    """Atomically claim all pending commands (oldest first); marks them picked."""
    with conn.transaction():
        rows = conn.execute(
            """
            SELECT id, command FROM sync_commands
            WHERE picked_at IS NULL
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            """
        ).fetchall()
        if not rows:
            return []
        conn.execute(
            "UPDATE sync_commands SET picked_at = now() WHERE id = ANY(%s)",
            ([row[0] for row in rows],),
        )
    return [(int(row[0]), str(row[1])) for row in rows]


def is_paused(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM sync_control_state WHERE key = 'paused'"
    ).fetchone()
    return bool(row and row[0] == "true")


def set_paused(conn: psycopg.Connection, paused: bool) -> None:
    conn.execute(
        """
        INSERT INTO sync_control_state (key, value, updated_at)
        VALUES ('paused', %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        ("true" if paused else "false",),
    )


def last_command(conn: psycopg.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT command, to_char(created_at, 'YYYY-MM-DD HH24:MI:SS'),
               (picked_at IS NOT NULL)
        FROM sync_commands ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {"command": row[0], "createdAt": row[1], "picked": bool(row[2])}


def acquire_sync_lock(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_LOCK_KEY,)).fetchone()
    assert row is not None
    return bool(row[0])


def release_sync_lock(conn: psycopg.Connection) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def ensure_release(
    conn: psycopg.Connection,
    collection: str,
    release_identifier: str,
    btih: str | None,
    source_url: str | None,
    data_size_bytes: int | None,
) -> int:
    """Insert release row if unknown; returns its id."""
    row = conn.execute(
        """
        INSERT INTO sync_releases (collection, release_identifier, btih, source_url, data_size_bytes)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (collection, release_identifier) DO UPDATE SET btih = EXCLUDED.btih
        RETURNING id
        """,
        (collection, release_identifier, btih, source_url, data_size_bytes),
    ).fetchone()
    assert row is not None
    return int(row[0])


def set_release_status(
    conn: psycopg.Connection,
    release_id: int,
    status: str,
    error_message: str | None = None,
    reset_counters: bool = False,
) -> None:
    assert status in RELEASE_STATUSES
    started = ", started_at = now()" if status == "downloading" else ""
    completed = ", completed_at = now()" if status == "completed" else ""
    counters = (
        ", records_seen = 0, records_inserted = 0, records_updated = 0, "
        "records_skipped = 0, records_failed = 0, "
        "import_done_bytes = 0, import_total_bytes = 0"
        if reset_counters
        else ""
    )
    conn.execute(
        f"""
        UPDATE sync_releases
        SET status = %s,
            error_message = %s{started}{completed}{counters}
        WHERE id = %s
        """,
        (status, error_message, release_id),
    )


def update_release_counters(
    conn: psycopg.Connection,
    release_id: int,
    seen: int = 0,
    inserted: int = 0,
    updated: int = 0,
    skipped: int = 0,
    discarded: int = 0,
    failed: int = 0,
) -> None:
    conn.execute(
        """
        UPDATE sync_releases SET
            records_seen = records_seen + %s,
            records_inserted = records_inserted + %s,
            records_updated = records_updated + %s,
            records_skipped = records_skipped + %s,
            records_discarded = records_discarded + %s,
            records_failed = records_failed + %s
        WHERE id = %s
        """,
        (seen, inserted, updated, skipped, discarded, failed, release_id),
    )


def update_download_progress(
    conn: psycopg.Connection,
    release_id: int,
    done_bytes: int,
    total_bytes: int,
) -> None:
    """Persist live torrent progress for the dashboard (throttled by caller)."""
    conn.execute(
        "UPDATE sync_releases SET download_done_bytes = %s, download_total_bytes = %s WHERE id = %s",
        (done_bytes, total_bytes, release_id),
    )


def update_import_progress(
    conn: psycopg.Connection,
    release_id: int,
    done_bytes: int,
    total_bytes: int,
) -> None:
    """Persist live import progress (compressed bytes consumed) for the dashboard.

    Throttled by the caller; errors must never abort an import.
    """
    conn.execute(
        "UPDATE sync_releases SET import_done_bytes = %s, import_total_bytes = %s WHERE id = %s",
        (done_bytes, total_bytes, release_id),
    )


def completed_release_identifiers(conn: psycopg.Connection, collection: str) -> set[str]:
    rows = conn.execute(
        "SELECT release_identifier FROM sync_releases WHERE collection = %s AND status = 'completed'",
        (collection,),
    ).fetchall()
    return {row[0] for row in rows}


def find_release(conn: psycopg.Connection, release_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, collection, release_identifier, status FROM sync_releases WHERE id = %s",
        (release_id,),
    ).fetchone()
    if row is None:
        return None
    keys = ("id", "collection", "release_identifier", "status")
    return dict(zip(keys, row, strict=True))


def last_successful_sync(conn: psycopg.Connection) -> str | None:
    row = conn.execute("SELECT MAX(completed_at) FROM sync_releases").fetchone()
    return row[0].isoformat() if row and row[0] else None


def total_records(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()
    assert row is not None
    return int(row[0])


def database_size_bytes(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    assert row is not None
    return int(row[0])
