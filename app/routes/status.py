"""System status endpoint."""

from __future__ import annotations

import shutil

import psycopg
from fastapi import APIRouter

from app.deps import GetConnectionDependency, GetSettingsDependency
from app.schemas import StatusResponse, SyncState
from common.db import approx_count, list_migrations

router = APIRouter(prefix="/api/v1", tags=["status"])

# The api container mounts sync_work read-only at /work/sync so disk free
# reflects the volume that also hosts PostgreSQL data (same host path).
WORK_DIR = "/work/sync"


@router.get("/status", response_model=StatusResponse)
def status(
    conn: psycopg.Connection = GetConnectionDependency,
    settings=GetSettingsDependency,
) -> StatusResponse:
    records = approx_count(conn, "metadata_records")
    db_size = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    last_sync = conn.execute(
        "SELECT to_char(MAX(completed_at), 'YYYY-MM-DD\"T\"HH24:MI:SSOF') FROM sync_releases "
        "WHERE status = 'completed'"
    ).fetchone()
    active = conn.execute(
        "SELECT release_identifier FROM sync_releases "
        "WHERE status IN ('downloading', 'importing', 'validating') ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    try:
        disk_free = shutil.disk_usage(WORK_DIR).free
    except OSError:
        disk_free = 0

    assert records is not None and db_size is not None and last_sync is not None
    migrations = list_migrations()
    version_row = conn.execute("SELECT COALESCE((SELECT MAX(version) FROM schema_migrations), 0)").fetchone()
    assert version_row is not None
    ready = bool(version_row[0] >= (migrations[-1][0] if migrations else 0) and migrations)

    return StatusResponse(
        ready=ready,
        records=records,
        lastSuccessfulSync=last_sync[0],
        collections=settings.aa_collections,
        databaseSizeBytes=int(db_size[0]),
        diskFreeBytes=int(disk_free),
        schemaVersion=int(version_row[0]),
        sync=SyncState(status="running" if active else "idle", activeRelease=active[0] if active else None),
    )
