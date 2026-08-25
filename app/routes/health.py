"""Health endpoints: liveness and readiness.

Migrations are applied during application startup (see app.main.lifespan);
readiness only verifies reachability and the current schema version.
An empty but migrated database counts as ready: the service is fully
functional (search simply returns no results) before/during a bootstrap.
Data availability is reported via /api/v1/status (`records`).
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter

from app.deps import GetConnectionDependency
from app.schemas import HealthReady
from common.db import list_migrations

router = APIRouter(prefix="/api/v1/health", tags=["health"])


@router.get("/live")
def live() -> dict:
    """Container process is alive (no external dependencies checked)."""
    return {"status": "live"}


@router.get("/ready", response_model=HealthReady)
def ready(conn: psycopg.Connection = GetConnectionDependency) -> HealthReady:
    try:
        row = conn.execute(
            "SELECT CASE WHEN to_regclass('public.schema_migrations') IS NULL THEN 0 "
            "ELSE COALESCE((SELECT MAX(version) FROM schema_migrations), 0) END"
        ).fetchone()
    except psycopg.Error as error:
        return HealthReady(ready=False, schemaVersion=0, migrationsApplied=False, message=str(error))
    assert row is not None
    current_version = int(row[0])
    latest_version = list_migrations()[-1][0] if list_migrations() else 0
    applied = current_version >= latest_version and latest_version > 0
    return HealthReady(
        ready=applied,
        schemaVersion=current_version,
        migrationsApplied=applied,
        message="ok" if applied else "migrations pending",
    )
