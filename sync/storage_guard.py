"""Storage guard: protects the configured storage budget before downloads/imports.

Assumption (documented in docs/sync.md): postgres data and sync_work live on
the same filesystem/volume host path, so free disk space measured from the sync
container's work directory reflects the space available to both.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

GIB = 1024**3


@dataclass(frozen=True)
class StorageDecision:
    allowed: bool
    level: str  # 'ok' | 'warn' | 'stop'
    database_size_bytes: int
    disk_free_bytes: int
    projected_total_bytes: int
    message: str


def _disk_free_bytes(path: str) -> int:
    usage = shutil.disk_usage(path)
    return usage.free


def evaluate_storage(
    work_dir: str,
    db_size_bytes: int = 0,
    additional_bytes_needed: float = 0.0,
    warn_gib: int = 300,
    stop_gib: int = 400,
) -> StorageDecision:
    """Decide whether an operation needing `additional_bytes_needed` may start.

    Budget model: projected total = current DB size + bytes still needed.
    Hard stop above `stop_gib`, warning above `warn_gib`. Additionally there
    must be enough *physically free* disk for the immediate operation.
    """
    disk_free = _disk_free_bytes(work_dir)
    projected_total = int(db_size_bytes + max(0, additional_bytes_needed))

    stop_bytes = stop_gib * GIB
    warn_bytes = warn_gib * GIB

    if projected_total >= stop_bytes or disk_free < additional_bytes_needed:
        return StorageDecision(
            allowed=False,
            level="stop",
            database_size_bytes=db_size_bytes,
            disk_free_bytes=disk_free,
            projected_total_bytes=projected_total,
            message=(
                f"Storage guard STOP: projected total {projected_total / GIB:.1f} GiB "
                f"(stop threshold {stop_gib} GiB) or insufficient free disk "
                f"({disk_free / GIB:.1f} GiB free, need {additional_bytes_needed / GIB:.1f} GiB)."
            ),
        )
    if projected_total >= warn_bytes:
        return StorageDecision(
            allowed=True,
            level="warn",
            database_size_bytes=db_size_bytes,
            disk_free_bytes=disk_free,
            projected_total_bytes=projected_total,
            message=(
                f"Storage guard WARN: projected total {projected_total / GIB:.1f} GiB "
                f"exceeds warning threshold {warn_gib} GiB."
            ),
        )
    return StorageDecision(
        allowed=True,
        level="ok",
        database_size_bytes=db_size_bytes,
        disk_free_bytes=disk_free,
        projected_total_bytes=projected_total,
        message=f"Storage guard OK: projected total {projected_total / GIB:.1f} GiB.",
    )
