"""Sync state persistence + server-side single-import locking."""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from psycopg.types.json import Jsonb

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
        ON CONFLICT (collection, release_identifier) DO UPDATE
            SET btih = COALESCE(EXCLUDED.btih, sync_releases.btih)
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
    import_started = ""
    if status == "importing":
        import_started = (
            ", import_started_at = now()"
            if reset_counters
            else ", import_started_at = COALESCE(import_started_at, now())"
        )
    completed = ", completed_at = now()" if status == "completed" else ""
    counters = (
        ", records_seen = 0, records_inserted = 0, records_updated = 0, "
        "records_skipped = 0, records_discarded = 0, records_failed = 0, "
        "import_done_bytes = 0, import_total_bytes = 0"
        + ("" if status == "importing" else ", import_started_at = NULL")
        + ", "
        "discard_reasons = '{}'::jsonb, discard_samples = '{}'::jsonb"
        if reset_counters
        else ""
    )
    conn.execute(
        f"""
        UPDATE sync_releases
        SET status = %s,
            error_message = %s{started}{import_started}{completed}{counters}
        WHERE id = %s
        """,
        (status, error_message, release_id),
    )
    if reset_counters:
        conn.execute(
            "DELETE FROM import_performance_buckets WHERE release_id = %s",
            (release_id,),
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
    record_import_sample(conn, release_id)


def update_discard_analysis(
    conn: psycopg.Connection,
    release_id: int,
    reason_deltas: dict[str, int],
    samples: dict[str, list[dict]],
) -> None:
    """Persist bounded discard reason counters and representative samples."""
    for reason, delta in reason_deltas.items():
        if delta <= 0:
            continue
        conn.execute(
            """
            UPDATE sync_releases
            SET discard_reasons = jsonb_set(
                discard_reasons,
                ARRAY[%s],
                to_jsonb(COALESCE((discard_reasons ->> %s)::bigint, 0) + %s),
                true
            )
            WHERE id = %s
            """,
            (reason, reason, delta, release_id),
        )
    if samples:
        conn.execute(
            "UPDATE sync_releases SET discard_samples = discard_samples || %s WHERE id = %s",
            (Jsonb(samples), release_id),
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
    record_import_sample(conn, release_id)


def record_import_sample(conn: psycopg.Connection, release_id: int) -> None:
    """Roll live counters into durable five-minute performance buckets."""
    conn.execute(
        """
        INSERT INTO import_performance_buckets (
            release_id, bucket_start, first_sample_at, last_sample_at,
            first_records_seen, last_records_seen, first_bytes, last_bytes
        )
        SELECT id,
               date_bin('5 minutes', now(), TIMESTAMPTZ '2001-01-01'),
               now(), now(), records_seen, records_seen,
               import_done_bytes, import_done_bytes
        FROM sync_releases WHERE id = %s
        ON CONFLICT (release_id, bucket_start) DO UPDATE SET
            last_sample_at = EXCLUDED.last_sample_at,
            last_records_seen = EXCLUDED.last_records_seen,
            last_bytes = EXCLUDED.last_bytes
        """,
        (release_id,),
    )


def get_subcollection_filter_overrides(
    conn: psycopg.Connection,
) -> dict[str, bool]:
    rows = conn.execute(
        "SELECT subcollection, blocked FROM upload_subcollection_filters"
    ).fetchall()
    return {str(row[0]): bool(row[1]) for row in rows}


def effective_upload_blocked_subcollections(
    conn: psycopg.Connection, defaults: list[str]
) -> list[str]:
    blocked = set(defaults)
    for subcollection, is_blocked in get_subcollection_filter_overrides(conn).items():
        if is_blocked:
            blocked.add(subcollection)
        else:
            blocked.discard(subcollection)
    return sorted(blocked)


def set_subcollection_filter(
    conn: psycopg.Connection, subcollection: str, blocked: bool
) -> None:
    conn.execute(
        """
        INSERT INTO upload_subcollection_filters (subcollection, blocked, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (subcollection) DO UPDATE
            SET blocked = EXCLUDED.blocked, updated_at = now()
        """,
        (subcollection, blocked),
    )


def enqueue_filter_analysis(
    conn: psycopg.Connection,
    release_id: int,
    filters_snapshot: dict[str, bool],
    total_bytes: int,
) -> tuple[int, bool]:
    """Queue a full statistics-only scan, deduplicating active jobs."""
    existing = conn.execute(
        """
        SELECT id FROM filter_analysis_jobs
        WHERE release_id = %s AND status IN ('pending', 'running')
        ORDER BY id DESC LIMIT 1
        """,
        (release_id,),
    ).fetchone()
    if existing:
        return int(existing[0]), False
    row = conn.execute(
        """
        INSERT INTO filter_analysis_jobs
            (release_id, filters_snapshot, total_bytes)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (release_id, Jsonb(filters_snapshot), total_bytes),
    ).fetchone()
    assert row is not None
    return int(row[0]), True


def claim_filter_analysis_job(conn: psycopg.Connection) -> dict | None:
    with conn.transaction():
        row = conn.execute(
            """
            SELECT j.id, j.release_id, r.collection, r.release_identifier,
                   j.filters_snapshot, j.total_bytes
            FROM filter_analysis_jobs j
            JOIN sync_releases r ON r.id = j.release_id
            WHERE j.status = 'pending'
            ORDER BY j.id LIMIT 1
            FOR UPDATE OF j SKIP LOCKED
            """
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE filter_analysis_jobs
            SET status = 'running', started_at = now(), error_message = NULL,
                progress_bytes = 0, records_scanned = 0, result_counts = '{}'::jsonb
            WHERE id = %s
            """,
            (row[0],),
        )
    keys = (
        "id", "release_id", "collection", "release_identifier",
        "filters_snapshot", "total_bytes",
    )
    return dict(zip(keys, row, strict=True))


def update_filter_analysis_progress(
    conn: psycopg.Connection, job_id: int, progress_bytes: int, records_scanned: int
) -> None:
    conn.execute(
        """
        UPDATE filter_analysis_jobs
        SET progress_bytes = %s, records_scanned = %s
        WHERE id = %s AND status = 'running'
        """,
        (progress_bytes, records_scanned, job_id),
    )


def complete_filter_analysis(
    conn: psycopg.Connection,
    job_id: int,
    release_id: int,
    filters_snapshot: dict[str, bool],
    counts: dict[str, int],
    total_bytes: int,
    records_scanned: int,
) -> None:
    with conn.transaction():
        for name in filters_snapshot:
            conn.execute(
                """
                INSERT INTO release_subcollection_stats
                    (release_id, subcollection, matching_records, filter_blocked,
                     analyzed_at, analysis_job_id)
                VALUES (%s, %s, %s, %s, now(), %s)
                ON CONFLICT (release_id, subcollection) DO UPDATE SET
                    matching_records = EXCLUDED.matching_records,
                    filter_blocked = EXCLUDED.filter_blocked,
                    analyzed_at = EXCLUDED.analyzed_at,
                    analysis_job_id = EXCLUDED.analysis_job_id
                """,
                (
                    release_id, name, counts.get(name, 0),
                    filters_snapshot[name], job_id,
                ),
            )
        conn.execute(
            """
            UPDATE filter_analysis_jobs
            SET status = 'completed', progress_bytes = %s, records_scanned = %s,
                result_counts = %s, completed_at = now()
            WHERE id = %s
            """,
            (total_bytes, records_scanned, Jsonb(counts), job_id),
        )


def fail_filter_analysis(conn: psycopg.Connection, job_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE filter_analysis_jobs
        SET status = 'failed', error_message = %s, completed_at = now()
        WHERE id = %s
        """,
        (error[:2000], job_id),
    )


def clear_retained_payload_identifier(
    conn: psycopg.Connection, collection: str
) -> None:
    conn.execute(
        """
        UPDATE collection_sync_modes
        SET last_imported_identifier = NULL, mode = 'auto', updated_at = now()
        WHERE collection = %s
        """,
        (collection,),
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


@dataclass
class CollectionMode:
    """Per-collection sync mode + which release the local payload corresponds to."""

    collection: str
    mode: str = "auto"  # 'auto' | 'import'
    last_imported_identifier: str | None = None


def get_collection_mode(conn: psycopg.Connection, collection: str) -> CollectionMode:
    """Read one collection mode, defaulting to ``auto`` when no row exists."""
    row = conn.execute(
        "SELECT collection, mode, last_imported_identifier "
        "FROM collection_sync_modes WHERE collection = %s",
        (collection,),
    ).fetchone()
    if row is None:
        return CollectionMode(collection=collection, mode="auto")
    return CollectionMode(collection=row[0], mode=row[1], last_imported_identifier=row[2])


def get_collection_modes(conn: psycopg.Connection) -> dict[str, CollectionMode]:
    """Return all explicitly configured collection modes."""
    rows = conn.execute(
        "SELECT collection, mode, last_imported_identifier FROM collection_sync_modes"
    ).fetchall()
    return {
        row[0]: CollectionMode(
            collection=row[0], mode=row[1], last_imported_identifier=row[2]
        )
        for row in rows
    }


def set_collection_mode(
    conn: psycopg.Connection, collection: str, mode: str
) -> None:
    assert mode in ("auto", "import")
    conn.execute(
        """
        INSERT INTO collection_sync_modes (collection, mode, updated_at)
        VALUES (%s, %s, now())
        ON CONFLICT (collection) DO UPDATE SET mode = EXCLUDED.mode, updated_at = now()
        """,
        (collection, mode),
    )


def record_imported_release(
    conn: psycopg.Connection, collection: str, release_identifier: str
) -> None:
    """Remember that the local .prev payload now corresponds to this release."""
    conn.execute(
        """
        INSERT INTO collection_sync_modes (collection, mode, last_imported_identifier, updated_at)
        VALUES (%s, 'auto', %s, now())
        ON CONFLICT (collection) DO UPDATE
            SET mode = 'auto', last_imported_identifier = EXCLUDED.last_imported_identifier,
                updated_at = now()
        """,
        (collection, release_identifier),
    )


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
