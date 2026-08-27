"""Sync control endpoints for the dashboard (DB-driven command queue).

Read endpoints are auth-exempt like /api/v1/sync/status; POST /commands goes
through the bearer middleware (protected when METADATA_API_KEY is set).
"""

from __future__ import annotations

from typing import Literal

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.deps import GetConnectionDependency, GetSettingsDependency
from common.config import Settings
from sync.state import (
    enqueue_command,
    is_paused,
    last_command,
    set_collection_mode,
    set_paused,
)
from sync.worker import next_scheduled_run

router = APIRouter(tags=["sync-control"])


class SyncCommandRequest(BaseModel):
    action: Literal["run_now", "pause", "resume"]
    note: str | None = None


class CollectionModeRequest(BaseModel):
    mode: Literal["auto", "import"]

    model_config = {"json_schema_extra": {"examples": [{"mode": "import"}]}}


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
