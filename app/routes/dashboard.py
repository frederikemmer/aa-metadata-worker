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
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import GetConnectionDependency, GetSettingsDependency
from common.config import Settings
from common.db import estimated_count, list_migrations

router = APIRouter(tags=["dashboard"])

WORK_DIR = "/work/sync"

_RELEASE_SUMMARY_SQL = """
SELECT collection, release_identifier, status, data_size_bytes,
       download_done_bytes, download_total_bytes,
       import_done_bytes, import_total_bytes,
       records_seen, records_inserted, records_updated,
       records_skipped, records_discarded, records_failed,
       error_message, discard_reasons, discard_samples,
       CASE
         WHEN COALESCE(import_started_at, started_at) IS NULL THEN NULL
         WHEN status IN ('importing', 'validating')
           THEN EXTRACT(EPOCH FROM (now() - COALESCE(import_started_at, started_at)))::bigint
         WHEN completed_at IS NOT NULL
           THEN EXTRACT(EPOCH FROM (completed_at - COALESCE(import_started_at, started_at)))::bigint
         ELSE NULL
       END AS import_duration_seconds,
       (import_started_at IS NULL AND started_at IS NOT NULL) AS import_timing_estimated,
       to_char(started_at, 'YYYY-MM-DD HH24:MI:SS') AS started,
       to_char(import_started_at, 'YYYY-MM-DD HH24:MI:SS') AS import_started,
       to_char(completed_at, 'YYYY-MM-DD HH24:MI:SS') AS completed
FROM sync_releases
"""


@router.get("/api/v1/sync/status")
def sync_status(
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    active_collections = settings.aa_collections
    active_rows = conn.execute(
        f"{_RELEASE_SUMMARY_SQL} WHERE status IN ('downloading','importing','validating') "
        "AND collection = ANY(%s) ORDER BY started_at DESC NULLS LAST LIMIT 1",
        (active_collections,),
    ).fetchall()
    active = _row_to_release(active_rows[0]) if active_rows else None

    modes = {
        (m[0]): m
        for m in conn.execute(
            "SELECT collection, mode, last_imported_identifier FROM collection_sync_modes"
        ).fetchall()
    }

    latest_sql = _RELEASE_SUMMARY_SQL.replace(
        "SELECT collection", "SELECT DISTINCT ON (collection) collection", 1
    )
    latest_by_collection = {
        row[0]: row
        for row in conn.execute(
            f"{latest_sql} WHERE collection = ANY(%s) "
            "ORDER BY collection, discovered_at DESC",
            (active_collections,),
        ).fetchall()
    }

    collections = []
    for name in settings.aa_collections:
        latest = latest_by_collection.get(name)
        if latest:
            entry = _row_to_release(latest)
        else:
            entry = {"collection": name, "status": "not_discovered"}
        mode_row = modes.get(name)
        entry["mode"] = mode_row[1] if mode_row else "auto"
        entry["lastImportedIdentifier"] = mode_row[2] if mode_row else None
        collections.append(entry)

    recent_rows = conn.execute(
        f"{_RELEASE_SUMMARY_SQL} WHERE collection = ANY(%s) "
        "ORDER BY discovered_at DESC LIMIT 12",
        (active_collections,),
    ).fetchall()
    totals_row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(records_discarded), 0), COALESCE(SUM(records_failed), 0) "
        "FROM sync_releases WHERE collection = ANY(%s)",
        (active_collections,),
    ).fetchone()
    # The dashboard refreshes frequently. Never make every poll wait for a
    # potentially multi-second COUNT(*) scan of the large metadata table.
    records_row = estimated_count(conn, "metadata_records")
    db_size_row = conn.execute("SELECT pg_database_size(current_database())").fetchone()
    version_row = conn.execute("SELECT COALESCE((SELECT MAX(version) FROM schema_migrations), 0)").fetchone()
    last_sync_row = conn.execute(
        "SELECT to_char(MAX(completed_at), 'YYYY-MM-DD\"T\"HH24:MI:SSOF') FROM sync_releases "
        "WHERE status='completed'"
    ).fetchone()
    discard_reason_rows = conn.execute(
        "SELECT reason.key, SUM(reason.value::bigint) "
        "FROM sync_releases, LATERAL jsonb_each_text(discard_reasons) AS reason "
        "WHERE collection = ANY(%s) GROUP BY reason.key ORDER BY SUM(reason.value::bigint) DESC",
        (active_collections,),
    ).fetchall()
    discard_sample_rows = conn.execute(
        "SELECT discard_samples FROM sync_releases "
        "WHERE collection = ANY(%s) AND discard_samples <> '{}'::jsonb "
        "ORDER BY discovered_at DESC LIMIT 20",
        (active_collections,),
    ).fetchall()
    discard_samples: dict[str, list[dict]] = {}
    for row in discard_sample_rows:
        for reason, samples in row[0].items():
            target = discard_samples.setdefault(reason, [])
            for sample in samples:
                if len(target) >= 3:
                    break
                if sample not in target:
                    target.append(sample)

    history_rows = conn.execute(
        """
        SELECT r.collection, b.bucket_start,
               EXTRACT(EPOCH FROM (b.last_sample_at - b.first_sample_at)),
               b.last_records_seen - b.first_records_seen,
               b.last_bytes - b.first_bytes
        FROM import_performance_buckets b
        JOIN sync_releases r ON r.id = b.release_id
        WHERE b.bucket_start >= now() - interval '12 hours'
          AND r.collection = ANY(%s)
        ORDER BY b.bucket_start
        """,
        (active_collections,),
    ).fetchall()
    import_history = []
    for collection, bucket_start, seconds, record_delta, byte_delta in history_rows:
        duration = float(seconds or 0)
        if duration <= 0:
            continue
        import_history.append(
            {
                "collection": collection,
                "bucketStart": bucket_start.isoformat(),
                "recordsPerSecond": max(0, float(record_delta) / duration),
                "bytesPerSecond": max(0, float(byte_delta) / duration),
                "sampleSeconds": duration,
            }
        )

    filter_overrides = {
        row[0]: bool(row[1])
        for row in conn.execute(
            "SELECT subcollection, blocked FROM upload_subcollection_filters"
        ).fetchall()
    }
    filter_updated = {
        row[0]: row[1].isoformat()
        for row in conn.execute(
            "SELECT subcollection, updated_at FROM upload_subcollection_filters"
        ).fetchall()
    }
    upload_release_rows = conn.execute(
        """
        SELECT release_identifier, discard_reasons, discovered_at
        FROM sync_releases
        WHERE collection = 'upload_records'
        ORDER BY discovered_at DESC
        LIMIT 12
        """
    ).fetchall()
    filtered_by_name: dict[str, dict] = {}
    for release_identifier, reasons, discovered_at in upload_release_rows:
        for reason, count in reasons.items():
            if not reason.startswith("blocked_subcollection:"):
                continue
            name = reason.split(":", 1)[1]
            entry = filtered_by_name.setdefault(name, {"total": 0, "releases": []})
            value = int(count)
            entry["total"] += value
            entry["releases"].append(
                {
                    "releaseIdentifier": release_identifier,
                    "count": value,
                    "sortAt": discovered_at.isoformat(),
                }
            )
    analyzed_rows = conn.execute(
        """
        SELECT s.subcollection, s.matching_records, s.filter_blocked,
               s.analyzed_at, r.id, r.release_identifier, r.discovered_at
        FROM release_subcollection_stats s
        JOIN sync_releases r ON r.id = s.release_id
        WHERE r.collection = 'upload_records'
        ORDER BY r.discovered_at DESC
        """
    ).fetchall()
    for (
        name, count, blocked_at_analysis, analyzed_at, release_id,
        release_identifier, discovered_at,
    ) in analyzed_rows:
        entry = filtered_by_name.setdefault(name, {"total": 0, "releases": []})
        entry["releases"] = [
            value for value in entry["releases"]
            if value["releaseIdentifier"] != release_identifier
        ]
        entry["releases"].append(
            {
                "releaseId": int(release_id),
                "releaseIdentifier": release_identifier,
                "count": int(count),
                "analyzedAt": analyzed_at.isoformat(),
                "filterBlocked": bool(blocked_at_analysis),
                "sortAt": discovered_at.isoformat(),
            }
        )
    for entry in filtered_by_name.values():
        entry["releases"].sort(key=lambda value: value["sortAt"], reverse=True)
        entry["total"] = sum(value["count"] for value in entry["releases"])
        for release_stats in entry["releases"]:
            release_stats.pop("sortAt", None)
    default_filters = set(settings.upload_blocked_subcollections)
    filter_names = default_filters | set(filter_overrides) | set(filtered_by_name)
    avg_db_bytes = int(db_size_row[0]) / max(int(records_row), 1)
    subcollection_filters = []
    for name in sorted(filter_names):
        stats = filtered_by_name.get(name, {"total": 0, "releases": []})
        latest_count = stats["releases"][0]["count"] if stats["releases"] else None
        blocked = filter_overrides.get(name, name in default_filters)
        subcollection_filters.append(
            {
                "subcollection": name,
                "blocked": blocked,
                "isDefault": name in default_filters,
                "hasOverride": name in filter_overrides,
                "effectiveSince": filter_updated.get(name),
                "latestFiltered": latest_count,
                "totalFiltered": stats["total"],
                "estimatedAdditionalDbBytes": (
                    int(latest_count * avg_db_bytes)
                    if latest_count is not None and blocked else (0 if latest_count is not None else None)
                ),
                "releases": stats["releases"],
            }
        )

    modes_by_collection = {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT collection, mode, last_imported_identifier FROM collection_sync_modes"
        ).fetchall()
    }
    payloads = []
    for collection in active_collections:
        path = pathlib.Path(WORK_DIR) / ".prev" / f"{collection}.payload"
        stat = path.stat() if path.is_file() else None
        mode = modes_by_collection.get(collection, ("auto", None))
        release_row = (
            conn.execute(
                """
                SELECT id FROM sync_releases
                WHERE collection = %s AND release_identifier = %s
                """,
                (collection, mode[1]),
            ).fetchone()
            if mode[1] else None
        )
        payloads.append(
            {
                "collection": collection,
                "available": stat is not None,
                "sizeBytes": stat.st_size if stat else 0,
                "modifiedAt": (
                    datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()
                    if stat else None
                ),
                "releaseIdentifier": mode[1],
                "releaseId": int(release_row[0]) if release_row else None,
            }
        )

    analysis_jobs = [
        {
            "id": int(row[0]),
            "releaseId": int(row[1]),
            "releaseIdentifier": row[2],
            "status": row[3],
            "progressBytes": int(row[4]),
            "totalBytes": int(row[5]),
            "recordsScanned": int(row[6]),
            "errorMessage": row[7],
            "createdAt": row[8].isoformat(),
            "completedAt": row[9].isoformat() if row[9] else None,
        }
        for row in conn.execute(
            """
            SELECT j.id, j.release_id, r.release_identifier, j.status,
                   j.progress_bytes, j.total_bytes, j.records_scanned,
                   j.error_message, j.created_at, j.completed_at
            FROM filter_analysis_jobs j
            JOIN sync_releases r ON r.id = j.release_id
            ORDER BY j.id DESC LIMIT 8
            """
        ).fetchall()
    ]

    try:
        disk_free = shutil.disk_usage(WORK_DIR).free
    except OSError:
        disk_free = 0

    assert db_size_row and version_row and totals_row and last_sync_row
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
        "discardAnalysis": [
            {
                "reason": row[0],
                "count": int(row[1]),
                "samples": discard_samples.get(row[0], []),
            }
            for row in discard_reason_rows
        ],
        "importHistory": import_history,
        "subcollectionFilters": subcollection_filters,
        "retainedPayloads": payloads,
        "filterAnalysisJobs": analysis_jobs,
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
    "discard_reasons",
    "discard_samples",
    "import_duration_seconds",
    "import_timing_estimated",
    "started",
    "import_started",
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
        "discardReasons": data["discard_reasons"],
        "discardSamples": data["discard_samples"],
        "importDurationSeconds": data["import_duration_seconds"],
        "importTimingEstimated": data["import_timing_estimated"],
        "startedAt": data["started"],
        "importStartedAt": data["import_started"],
        "completedAt": data["completed"],
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


_DASHBOARD_HTML = (pathlib.Path(__file__).resolve().parent / "dashboard.html").read_text(encoding="utf-8")
