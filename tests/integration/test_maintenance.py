"""Integration tests for destructive maintenance commands."""

from __future__ import annotations

import argparse
import gzip

import psycopg

from sync.cli import cmd_purge_sources


def test_purge_sources_backs_up_and_removes_inactive_collections(
    db_conn, postgres_docker, tmp_path, monkeypatch
):
    for index, collection in enumerate(
        ("zlib3_records", "upload_records", "gbooks_records", "ia2_records"),
        start=1,
    ):
        db_conn.execute(
            """
            INSERT INTO metadata_records (md5, title, title_norm, source_collection)
            VALUES (decode(%s, 'hex'), %s, %s, %s)
            """,
            (f"{index:032x}", collection, collection, collection),
        )
        db_conn.execute(
            """
            INSERT INTO sync_releases (collection, release_identifier, status)
            VALUES (%s, %s, 'completed')
            """,
            (collection, f"release-{collection}"),
        )
        db_conn.execute(
            "INSERT INTO collection_sync_modes (collection, mode) VALUES (%s, 'auto')",
            (collection,),
        )

    monkeypatch.setattr(
        "sync.cli.connect",
        lambda: psycopg.connect(postgres_docker, autocommit=True),
    )
    monkeypatch.setattr("sync.cli._work_dir", lambda: tmp_path)

    result = cmd_purge_sources(
        argparse.Namespace(keep="zlib3_records,upload_records", yes=True)
    )

    assert result == 0
    expected = {"zlib3_records", "upload_records"}
    assert {
        row[0]
        for row in db_conn.execute(
            "SELECT DISTINCT source_collection FROM metadata_records"
        ).fetchall()
    } == expected
    assert {
        row[0]
        for row in db_conn.execute("SELECT DISTINCT collection FROM sync_releases").fetchall()
    } == expected
    assert {
        row[0]
        for row in db_conn.execute(
            "SELECT DISTINCT collection FROM collection_sync_modes"
        ).fetchall()
    } == expected

    backups = sorted((tmp_path / "backup").glob("*_purged_*.bin.gz"))
    assert len(backups) == 3
    for backup in backups:
        with gzip.open(backup, "rb") as source:
            assert source.read(11) == b"PGCOPY\n\xff\r\n\x00"
