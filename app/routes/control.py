"""Sync control endpoints for the dashboard (DB-driven command queue).

Read endpoints are auth-exempt like /api/v1/sync/status; POST /commands goes
through the bearer middleware (protected when METADATA_API_KEY is set).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.deps import GetConnectionDependency, GetSettingsDependency
from common.config import Settings
from sync.state import (
    clear_retained_payload_identifier,
    enqueue_command,
    enqueue_filter_analysis,
    get_subcollection_filter_overrides,
    is_paused,
    last_command,
    set_collection_mode,
    set_paused,
    set_subcollection_filter,
)
from sync.worker import next_scheduled_run

router = APIRouter(tags=["sync-control"])
WORK_DIR = Path("/work/sync")
_SUBCOLLECTION_RE = re.compile(r"^[a-z0-9_]{1,80}$")


class SyncCommandRequest(BaseModel):
    action: Literal["run_now", "pause", "resume"]
    note: str | None = None


class CollectionModeRequest(BaseModel):
    mode: Literal["auto", "import"]

    model_config = {"json_schema_extra": {"examples": [{"mode": "import"}]}}


class SubcollectionFilterRequest(BaseModel):
    blocked: bool


class FilterAnalysisRequest(BaseModel):
    release_id: int


@router.get("/api/v1/sync/collections/{collection}/mode")
def collection_mode(
    collection: str,
    conn: psycopg.Connection = GetConnectionDependency,
) -> dict:
    from sync.state import get_collection_mode

    entry = get_collection_mode(conn, collection)
    return {
        "collection": entry.collection,
        "mode": entry.mode,
        "lastImportedIdentifier": entry.last_imported_identifier,
    }


@router.post("/api/v1/sync/collections/{collection}/mode", status_code=200)
def set_collection_sync_mode(
    collection: str,
    request: CollectionModeRequest,
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    if collection not in settings.aa_collections:
        raise HTTPException(
            status_code=400,
            detail=f"Collection '{collection}' is not active (AA_COLLECTIONS).",
        )
    set_collection_mode(conn, collection, request.mode)
    return {"collection": collection, "mode": request.mode, "queued": True}


@router.post("/api/v1/sync/subcollections/{subcollection}", status_code=200)
def set_upload_subcollection_filter(
    subcollection: str,
    request: SubcollectionFilterRequest,
    conn: psycopg.Connection = GetConnectionDependency,
) -> dict:
    if not _SUBCOLLECTION_RE.fullmatch(subcollection):
        raise HTTPException(status_code=400, detail="Invalid subcollection name.")
    set_subcollection_filter(conn, subcollection, request.blocked)
    return {"subcollection": subcollection, "blocked": request.blocked}


@router.post("/api/v1/sync/filter-analysis", status_code=202)
def queue_filter_analysis(
    request: FilterAnalysisRequest,
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    release = conn.execute(
        """
        SELECT id, release_identifier FROM sync_releases
        WHERE id = %s AND collection = 'upload_records'
        """,
        (request.release_id,),
    ).fetchone()
    if release is None:
        raise HTTPException(status_code=404, detail="upload_records release not found.")
    mode = conn.execute(
        """
        SELECT last_imported_identifier FROM collection_sync_modes
        WHERE collection = 'upload_records'
        """
    ).fetchone()
    payload = WORK_DIR / ".prev" / "upload_records.payload"
    if mode is None or mode[0] != release[1] or not payload.is_file():
        raise HTTPException(
            status_code=409,
            detail="The retained payload for this release is not available.",
        )

    overrides = get_subcollection_filter_overrides(conn)
    names = set(settings.upload_blocked_subcollections) | set(overrides)
    blocked_names = set(settings.upload_blocked_subcollections)
    for name, blocked in overrides.items():
        if blocked:
            blocked_names.add(name)
        else:
            blocked_names.discard(name)
    snapshot = {name: name in blocked_names for name in sorted(names)}
    job_id, created = enqueue_filter_analysis(
        conn, int(release[0]), snapshot, payload.stat().st_size
    )
    return {
        "queued": True,
        "created": created,
        "id": job_id,
        "releaseId": int(release[0]),
    }


@router.delete("/api/v1/sync/payloads/{collection}", status_code=200)
def delete_retained_payload(
    collection: str,
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    if collection not in settings.aa_collections:
        raise HTTPException(status_code=400, detail="Collection is not active.")
    in_use = conn.execute(
        """
        SELECT 1 FROM filter_analysis_jobs j
        JOIN sync_releases r ON r.id = j.release_id
        WHERE r.collection = %s AND j.status IN ('pending', 'running')
        LIMIT 1
        """,
        (collection,),
    ).fetchone()
    if in_use:
        raise HTTPException(
            status_code=409,
            detail="The payload is currently used by a filter analysis.",
        )
    payload = WORK_DIR / ".prev" / f"{collection}.payload"
    if not payload.is_file():
        raise HTTPException(status_code=404, detail="No retained payload found.")
    deleted_bytes = payload.stat().st_size
    payload.unlink()
    clear_retained_payload_identifier(conn, collection)
    return {"deleted": True, "collection": collection, "deletedBytes": deleted_bytes}


@router.get("/api/v1/sync/control")
def control_state(
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    return {
        "paused": is_paused(conn),
        "enabled": settings.sync_enabled,
        "schedule": settings.sync_schedule,
        "tz": settings.tz,
        "nextScheduledRun": next_scheduled_run(settings.sync_schedule, settings.tz),
        "lastCommand": last_command(conn),
    }


@router.post("/api/v1/sync/commands", status_code=202)
def post_command(
    request: SyncCommandRequest,
    conn: psycopg.Connection = GetConnectionDependency,
    settings: Settings = GetSettingsDependency,
) -> dict:
    if not settings.sync_enabled:
        raise HTTPException(status_code=409, detail="Worker disabled via SYNC_ENABLED=false")
    command_id = enqueue_command(conn, request.action, request.note)
    if request.action == "pause":
        # Visible immediately in the dashboard; worker picks up the interrupt.
        set_paused(conn, True)
    elif request.action == "resume":
        set_paused(conn, False)
    return {"queued": True, "id": command_id, "action": request.action}
