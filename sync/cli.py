"""CLI for sync operations and maintenance.

Usage (inside the sync container or a local venv):
  python -m sync.cli status
  python -m sync.cli check
  python -m sync.cli run [--collections a,b] [--force]
        [--release collection=identifier_suffix …]
  python -m sync.cli bootstrap [--collections a,b]
        [--release collection=identifier_suffix …]
  python -m sync.cli retry <release_id>
  python -m sync.cli storage-report
  python -m sync.cli db-stats
  python -m sync.cli purge-sources [--keep a,b] [--yes]
  python -m sync.cli worker
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from common.config import load_settings
from common.db import connect
from sync.storage_guard import GIB, evaluate_storage


def _setup_logging() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _work_dir() -> Path:
    candidate = Path("/work/sync")
    if candidate.exists():
        return candidate
    fallback = Path("./sync_work")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _collections_arg(raw: str) -> list[str] | None:
    return [c.strip() for c in raw.split(",") if c.strip()] or None


def cmd_status(_args: argparse.Namespace) -> int:
    from sync import state

    conn = connect()
    try:
        releases = conn.execute(
            """
            SELECT collection, release_identifier, status, records_seen, records_inserted,
                   records_updated, records_skipped, records_failed, error_message,
                   started_at, completed_at
            FROM sync_releases ORDER BY discovered_at DESC LIMIT 50
            """
        ).fetchall()
        print(f"records total:      {state.total_records(conn):>15,}")
        print(f"database size:      {state.database_size_bytes(conn) / GIB:>12.2f} GiB")
        print(f"last successful:    {state.last_successful_sync(conn)}")
        print("\nreleases:")
        for row in releases:
            print(
                f"  [{row[2]:>16}] {row[0]} {row[1][:60]} "
                f"seen={row[3]} ins={row[4]} upd={row[5]} skip={row[6]} fail={row[7]}"
                + (f" err={row[8][:80]}" if row[8] else "")
            )
    finally:
        conn.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Discover without importing; prints what would be done."""
    from sync import state
    from sync.discovery import fetch_manifest, latest_releases

    settings = load_settings()
    collections = _collections_arg(args.collections) or settings.aa_collections
    manifest = fetch_manifest(settings.aa_mirror_base_url)
    releases = latest_releases(manifest, collections)
    conn = connect()
    try:
        for collection in collections:
            release = releases.get(collection)
            if release is None:
                print(f"{collection}: NO RELEASE FOUND in manifest")
                continue
            done = state.completed_release_identifiers(conn, collection)
            status = "COMPLETED" if release.identifier in done else "PENDING"
            print(
                f"{collection}: {status}\n  release: {release.identifier}\n"
                f"  size:    {release.data_size_bytes / GIB:.2f} GiB\n  btih:    {release.btih}"
            )
    finally:
        conn.close()
    return 0


def _release_overrides(raw_specs: list[str] | None) -> dict[str, str] | None:
    """Parse 'collection=identifier_suffix' specs into a mapping."""
    if not raw_specs:
        return None
    overrides: dict[str, str] = {}
    for spec in raw_specs:
        collection, _, suffix = spec.partition("=")
        if not collection or not suffix:
            raise SystemExit(f"Invalid --release spec '{spec}', expected 'collection=suffix'.")
        if collection in overrides:
            raise SystemExit(f"Duplicate --release for collection '{collection}'.")
        overrides[collection.strip()] = suffix.strip()
    return overrides


def _run_sync(
    collections: list[str] | None, force: bool, release_specs: list[str] | None = None
) -> int:
    from sync.run import run_sync

    summary = run_sync(
        collections=collections,
        force=force,
        work_dir=_work_dir(),
        release_overrides=_release_overrides(release_specs),
    )
    print(
        json.dumps(
            {
                "processed": [c for c, _ in summary.processed],
                "skipped_completed": summary.skipped_completed,
                "blocked": [c for c, _ in summary.blocked],
                "failed": summary.failed,
                "duration_s": round(summary.duration_s, 1),
            },
            indent=2,
        )
    )
    return 1 if summary.failed else 0


def cmd_run(args: argparse.Namespace) -> int:
    return _run_sync(_collections_arg(args.collections), args.force, args.release)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Explicit initial import of the current cumulative releases."""
    logging.getLogger(__name__).info("Bootstrap starting; this can take many hours.")
    return _run_sync(_collections_arg(args.collections), force=False, release_specs=args.release)


def cmd_retry(args: argparse.Namespace) -> int:
    from sync import state

    conn = connect()
    try:
        row = state.find_release(conn, args.release_id)
        if row is None:
            print(f"Release id {args.release_id} not found.", file=sys.stderr)
            return 2
        conn.execute(
            "UPDATE sync_releases SET status = 'discovered', error_message = NULL WHERE id = %s",
            (args.release_id,),
        )
        collection = row["collection"]
        print(f"Reset release {args.release_id} ({collection}) to 'discovered'.")
    finally:
        conn.close()
    return _run_sync([collection], force=False)


def cmd_storage_report(_args: argparse.Namespace) -> int:
    from sync import state

    settings = load_settings()
    work_dir = _work_dir()
    conn = connect()
    try:
        apply_migrations_if_needed(conn)
        db_size = state.database_size_bytes(conn)
        record_count = state.total_records(conn)
        relations = conn.execute(
            """
            SELECT relname, pg_total_relation_size(relid) AS total_bytes,
                   pg_relation_size(relid) AS heap_bytes
            FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10
            """
        ).fetchall()
    finally:
        conn.close()

    decision = evaluate_storage(
        str(work_dir), db_size, 0.0, settings.storage_warn_gib, settings.storage_stop_gib
    )
    per_record = (db_size / record_count) if record_count else 0.0

    print(f"records:              {record_count:>15,}")
    print(f"database size:        {db_size / GIB:>12.2f} GiB")
    print(f"bytes/record:         {per_record:>12.0f}")
    print(f"disk free (work dir): {decision.disk_free_bytes / GIB:>12.2f} GiB")
    print(f"warn/stop thresholds: {settings.storage_warn_gib} / {settings.storage_stop_gib} GiB")
    print(f"decision:             {decision.level.upper()} - {decision.message}")

    projected_30m = per_record * 30_000_000
    print(f"\nprojection @30M records @current bytes/record: {projected_30m / GIB:.1f} GiB")

    print("\ntop relations (heap + indexes):")
    for relname, total, heap in relations:
        print(
            f"  {relname:<24} total={total / GIB:>9.2f} GiB  heap={heap / GIB:>9.2f} GiB "
            f" idx={(total - heap) / GIB:>8.2f} GiB"
        )
    return 0


def apply_migrations_if_needed(conn) -> None:
    from common.db import apply_migrations

    apply_migrations(conn)


def cmd_purge_sources(args: argparse.Namespace) -> int:
    """Back up and remove every source collection not listed in ``--keep``.

    The sync advisory lock prevents an import from racing the backup/delete.
    PostgreSQL binary COPY backups are written for records and release history
    before anything is deleted.
    """
    import gzip
    import time as _time

    from sync import state

    keep = [c.strip() for c in (args.keep or "").split(",") if c.strip()]
    if not keep:
        print("No --keep collections given; refusing to purge everything.", file=sys.stderr)
        return 2

    work_dir = _work_dir()
    backup_dir = work_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)

    conn = connect()
    lock_acquired = False
    try:
        apply_migrations_if_needed(conn)
        lock_acquired = state.acquire_sync_lock(conn)
        if not lock_acquired:
            print(
                "Another sync/import is active; refusing to purge while data is changing.",
                file=sys.stderr,
            )
            return 3

        rows = conn.execute(
            "SELECT source_collection, COUNT(*) FROM metadata_records "
            "WHERE source_collection <> ALL(%s) GROUP BY 1 ORDER BY 2 DESC",
            (keep,),
        ).fetchall()
        total = sum(int(r[1]) for r in rows)
        db_size_now = state.database_size_bytes(conn) / GIB
        print(f"keep sources: {keep}")
        print(f"to purge ({total:,} records):")
        for name, count in rows:
            print(f"  {name:<24}{int(count):>13,}")
        print(f"db size now: {db_size_now:.2f} GiB")
        if total == 0:
            print("Nothing to purge.")
            return 0

        if not args.yes:
            answer = input(
                f"Export {total:,} records to {backup_dir} then DELETE from "
                "metadata_records? Type 'yes' to continue: "
            ).strip().lower()
            if answer != "yes":
                print("Aborted.")
                return 0

        stamp = _time.strftime("%Y%m%dT%H%M%SZ", _time.gmtime())
        backup_specs = (
            (
                "metadata_records",
                "COPY (SELECT * FROM metadata_records "
                "WHERE source_collection <> ALL(%s)) TO STDOUT WITH (FORMAT binary)",
            ),
            (
                "sync_releases",
                "COPY (SELECT * FROM sync_releases "
                "WHERE collection <> ALL(%s)) TO STDOUT WITH (FORMAT binary)",
            ),
            (
                "collection_sync_modes",
                "COPY (SELECT * FROM collection_sync_modes "
                "WHERE collection <> ALL(%s)) TO STDOUT WITH (FORMAT binary)",
            ),
        )
        for table, copy_sql in backup_specs:
            backup_path = backup_dir / f"{table}_purged_{stamp}.bin.gz"
            # Keep gzip framing for integrity/restore tooling, but store the
            # PostgreSQL binary stream without Deflate compression. For tens
            # of millions of rows compression would otherwise hold the
            # maintenance transaction open for hours on a single CPU core.
            with gzip.open(backup_path, "wb", compresslevel=0) as out:
                with conn.cursor().copy(copy_sql, params=(keep,)) as copy:
                    while chunk := copy.read():
                        out.write(chunk)
            backup_size = backup_path.stat().st_size / GIB
            print(f"Backup written: {backup_path} ({backup_size:.2f} GiB)")

        with conn.transaction():
            deleted_records = conn.execute(
                "DELETE FROM metadata_records WHERE source_collection <> ALL(%s)",
                (keep,),
            ).rowcount
            deleted_releases = conn.execute(
                "DELETE FROM sync_releases WHERE collection <> ALL(%s)", (keep,)
            ).rowcount
            deleted_modes = conn.execute(
                "DELETE FROM collection_sync_modes WHERE collection <> ALL(%s)",
                (keep,),
            ).rowcount
        print(
            f"Deleted {deleted_records:,} records, {deleted_releases:,} release rows "
            f"and {deleted_modes:,} collection-mode rows."
        )
        print("VACUUM (ANALYZE, PARALLEL 0) metadata_records (space becomes reusable):")
        # Docker's default /dev/shm is intentionally small. Disabling parallel
        # vacuum avoids shared-memory allocation failures on large tables.
        conn.execute("VACUUM (ANALYZE, PARALLEL 0) metadata_records")
        remaining = conn.execute(
            "SELECT source_collection, COUNT(*) FROM metadata_records "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
        unexpected = [row for row in remaining if row[0] not in keep]
        if unexpected:
            raise RuntimeError(f"Purge verification failed; unexpected sources remain: {unexpected}")
        print("remaining sources:")
        for name, count in remaining:
            print(f"  {name:<24}{int(count):>13,}")
        db_size_after = state.database_size_bytes(conn) / GIB
        print(f"db size after vacuum: {db_size_after:.2f} GiB")
    finally:
        if lock_acquired:
            state.release_sync_lock(conn)
        conn.close()
    return 0


def cmd_db_stats(_args: argparse.Namespace) -> int:
    conn = connect()
    try:
        apply_migrations_if_needed(conn)
        version_row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        records = conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()
        deleted = conn.execute("SELECT COUNT(*) FROM metadata_records WHERE deleted").fetchone()
        with_work = conn.execute(
            "SELECT COUNT(*) FROM metadata_records WHERE work_key IS NOT NULL"
        ).fetchone()
        by_collection = conn.execute(
            "SELECT source_collection, COUNT(*) FROM metadata_records GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        by_format = conn.execute(
            "SELECT extension, COUNT(*) FROM metadata_records WHERE NOT deleted "
            "GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ).fetchall()
        assert records and deleted and with_work and version_row
        print(f"schema version:       {version_row[0]:>15}")
        print(f"records:              {int(records[0]):>15,}")
        print(f"deleted/tombstones:   {int(deleted[0]):>15,}")
        print(f"with work_key:        {int(with_work[0]):>15,}")
        print("\nby collection:")
        for name, count in by_collection:
            print(f"  {name:<28}{count:>13,}")
        print("\nby format (top 10, non-deleted):")
        for name, count in by_format:
            print(f"  {(name or '-'):<28}{count:>13,}")
    finally:
        conn.close()
    return 0


def cmd_worker(_args: argparse.Namespace) -> int:
    from sync.worker import run_worker_forever

    run_worker_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="metadata", description="AA Metadata Worker CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show sync state and recent releases")
    p_status.set_defaults(func=cmd_status)

    p_check = sub.add_parser("check", help="Discover releases without importing")
    p_check.add_argument("--collections", default="", help="Comma separated collection list")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", help="Run one incremental sync pass")
    p_run.add_argument("--collections", default="", help="Comma separated collection list")
    p_run.add_argument("--force", action="store_true", help="Re-import even completed releases")
    p_run.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="COLLECTION=SUFFIX",
        help="Pin a specific release (identifier suffix) instead of the newest; repeatable",
    )
    p_run.set_defaults(func=cmd_run)

    p_boot = sub.add_parser("bootstrap", help="Explicit initial full bootstrap")
    p_boot.add_argument("--collections", default="", help="Comma separated collection list")
    p_boot.add_argument(
        "--release",
        action="append",
        default=[],
        metavar="COLLECTION=SUFFIX",
        help="Pin a specific release (identifier suffix) instead of the newest; repeatable",
    )
    p_boot.set_defaults(func=cmd_bootstrap)

    p_retry = sub.add_parser("retry", help="Retry a failed/blocked release by id")
    p_retry.add_argument("release_id", type=int)
    p_retry.set_defaults(func=cmd_retry)

    p_store = sub.add_parser("storage-report", help="Storage usage report and projection")
    p_store.set_defaults(func=cmd_storage_report)

    p_dbstats = sub.add_parser("db-stats", help="Record counts by collection/format")
    p_dbstats.set_defaults(func=cmd_db_stats)

    p_purge = sub.add_parser(
        "purge-sources",
        help="Backup + delete records from source collections not in --keep",
    )
    p_purge.add_argument(
        "--keep", default="zlib3_records,upload_records",
        help="Comma separated collections to keep (default: zlib3_records,upload_records)",
    )
    p_purge.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_purge.set_defaults(func=cmd_purge_sources)

    p_worker = sub.add_parser("worker", help="Run the scheduled sync worker loop")
    p_worker.set_defaults(func=cmd_worker)

    return parser


def main() -> int:
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
