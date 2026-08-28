"""Import-free filter reanalysis against a retained upload payload."""

from __future__ import annotations

from sync import state
from sync.filter_analysis import process_filter_analysis_job, reset_stop
from tests.conftest import make_zst


def test_filter_analysis_updates_only_statistics(db_conn, tmp_path):
    release_id = state.ensure_release(
        db_conn, "upload_records", "upload-release", "btih", "url", 1000
    )
    state.set_release_status(db_conn, release_id, "completed")
    state.record_imported_release(db_conn, "upload_records", "upload-release")
    payload = tmp_path / ".prev" / "upload_records.payload"
    make_zst(
        [
            {"aacid": "aacid__upload_records_aaaaarg__20260101T000000Z__1__A"},
            {"aacid": "aacid__upload_records_aaaaarg__20260101T000000Z__2__B"},
            {"aacid": "aacid__upload_records_magzdb__20260101T000000Z__3__C"},
            {"aacid": "aacid__upload_records__20260101T000000Z__4__D"},
        ],
        payload,
    )
    snapshot = {"aaaaarg": True, "magzdb": False, "new_filter": True}
    job_id, created = state.enqueue_filter_analysis(
        db_conn, release_id, snapshot, payload.stat().st_size
    )
    assert created is True
    job = state.claim_filter_analysis_job(db_conn)
    assert job is not None and job["id"] == job_id

    reset_stop()
    process_filter_analysis_job(db_conn, job, tmp_path)

    rows = db_conn.execute(
        """
        SELECT subcollection, matching_records, filter_blocked
        FROM release_subcollection_stats WHERE release_id = %s
        ORDER BY subcollection
        """,
        (release_id,),
    ).fetchall()
    assert rows == [
        ("aaaaarg", 2, True),
        ("magzdb", 1, False),
        ("new_filter", 0, True),
    ]
    job_row = db_conn.execute(
        "SELECT status, records_scanned FROM filter_analysis_jobs WHERE id = %s",
        (job_id,),
    ).fetchone()
    assert job_row == ("completed", 4)
    assert db_conn.execute("SELECT COUNT(*) FROM metadata_records").fetchone()[0] == 0
    release = db_conn.execute(
        "SELECT status, records_seen FROM sync_releases WHERE id = %s", (release_id,)
    ).fetchone()
    assert release == ("completed", 0)
