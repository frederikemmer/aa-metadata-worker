"""Progress dashboard: JSON status + self-contained HTML page.

Designed to be checked from any browser in the LAN (http://<host>:8010/dashboard)
without docker CLI access. All progress is read from PostgreSQL (sync_releases),
which the sync worker updates live during downloads/imports - so the dashboard
works across container boundaries.
"""

from __future__ import annotations

import os
import pathlib
import shutil

import psycopg
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import GetConnectionDependency, GetSettingsDependency
from common.config import Settings
from common.db import approx_count, list_migrations

router = APIRouter(tags=["dashboard"])

WORK_DIR = "/work/sync"

_RELEASE_SUMMARY_SQL = """
SELECT collection, release_identifier, status, data_size_bytes,
       download_done_bytes, download_total_bytes,
       import_done_bytes, import_total_bytes,
       records_seen, records_inserted, records_updated,
       records_skipped, records_discarded, records_failed,
       error_message,
       to_char(started_at, 'YYYY-MM-DD HH24:MI:SS') AS started,
       to_char(completed_at, 'YYYY-MM-DD HH24:MI:SS') AS completed
FROM sync_releases
"""


@router.get("/api/v1/sync/status")
def sync_status(
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    active_rows = conn.execute(
        f"{_RELEASE_SUMMARY_SQL} WHERE status IN ('downloading','importing','validating') "
        "ORDER BY started_at DESC NULLS LAST LIMIT 1"
    ).fetchall()
    active = _row_to_release(active_rows[0]) if active_rows else None

    collections = []
    for name in settings.aa_collections:
        rows = conn.execute(
            f"{_RELEASE_SUMMARY_SQL} WHERE collection = %s ORDER BY discovered_at DESC LIMIT 1",
            (name,),
        ).fetchall()
        if rows:
            entry = _row_to_release(rows[0])
        else:
            entry = {"collection": name, "status": "not_discovered"}
        collections.append(entry)

    recent_rows = conn.execute(f"{_RELEASE_SUMMARY_SQL} ORDER BY discovered_at DESC LIMIT 12").fetchall()
    totals_row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(records_discarded), 0), COALESCE(SUM(records_failed), 0) "
        "FROM sync_releases"
    ).fetchone()
    records_row = approx_count(conn, "metadata_records")
    db_size_row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    version_row = conn.execute("SELECT COALESCE((SELECT MAX(version) FROM schema_migrations), 0)").fetchone()
    last_sync_row = conn.execute(
        "SELECT to_char(MAX(completed_at), 'YYYY-MM-DD\"T\"HH24:MI:SSOF') FROM sync_releases "
        "WHERE status='completed'"
    ).fetchone()

    try:
        disk_free = shutil.disk_usage(WORK_DIR).free
    except OSError:
        disk_free = 0

    assert records_row and db_size_row and version_row and totals_row and last_sync_row
    migrations = list_migrations()
    ready = bool(migrations) and int(version_row[0]) >= migrations[-1][0]

    return {
        "ready": ready,
        "appVersion": os.environ.get("APP_VERSION", "dev"),
        "schemaVersion": int(version_row[0]),
        "records": records_row,
        "lastSuccessfulSync": last_sync_row[0],
        "databaseSizeBytes": int(db_size_row[0]),
        "diskFreeBytes": int(disk_free),
        "storageWarnGib": settings.storage_warn_gib,
        "storageStopGib": settings.storage_stop_gib,
        "releasesTracked": int(totals_row[0]),
        "totalDiscarded": int(totals_row[1]),
        "totalFailed": int(totals_row[2]),
        "activeSync": active,
        "collections": collections,
        "recentReleases": [_row_to_release(row) for row in recent_rows],
    }


_COLUMNS = (
    "collection",
    "release_identifier",
    "status",
    "data_size_bytes",
    "download_done_bytes",
    "download_total_bytes",
    "import_done_bytes",
    "import_total_bytes",
    "records_seen",
    "records_inserted",
    "records_updated",
    "records_skipped",
    "records_discarded",
    "records_failed",
    "error_message",
    "started",
    "completed",
)


def _row_to_release(row: tuple) -> dict:
    data = dict(zip(_COLUMNS, row, strict=True))
    return {
        "collection": data["collection"],
        "releaseIdentifier": data["release_identifier"],
        "status": data["status"],
        "dataSizeBytes": data["data_size_bytes"],
        "downloadDoneBytes": data["download_done_bytes"],
        "downloadTotalBytes": data["download_total_bytes"],
        "importDoneBytes": data["import_done_bytes"],
        "importTotalBytes": data["import_total_bytes"],
        "recordsSeen": data["records_seen"],
        "recordsInserted": data["records_inserted"],
        "recordsUpdated": data["records_updated"],
        "recordsSkipped": data["records_skipped"],
        "recordsDiscarded": data["records_discarded"],
        "recordsFailed": data["records_failed"],
        "errorMessage": data["error_message"],
        "startedAt": data["started"],
        "completedAt": data["completed"],
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = (pathlib.Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")
