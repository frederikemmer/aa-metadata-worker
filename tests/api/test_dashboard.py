"""Dashboard tests: sync/status JSON shape + HTML page availability."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from common.db import close_pool


@pytest.fixture()
def client(db_conn):
    close_pool()
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    close_pool()


class TestSyncStatusJson:
    def test_shape_empty_db(self, client):
        response = client.get("/api/v1/sync/status")
        assert response.status_code == 200
        body = response.json()
        for key in (
            "ready", "appVersion", "records", "databaseSizeBytes", "diskFreeBytes",
            "storageWarnGib", "storageStopGib", "activeSync",
            "collections", "recentReleases", "totalDiscarded",
            "discardAnalysis",
            "importHistory", "subcollectionFilters", "retainedPayloads",
            "filterAnalysisJobs",
            "collectionBreakdown",
        ):
            assert key in body, f"missing key {key}"
        assert body["activeSync"] is None
        assert body["records"] == 0
        # Configured collections are always listed (even without releases).
        names = {c["collection"] for c in body["collections"]}
        assert names == {"zlib3_records", "upload_records"}
        filters = {item["subcollection"]: item for item in body["subcollectionFilters"]}
        assert filters["aaaaarg"]["blocked"] is True
        assert filters["aaaaarg"]["latestFiltered"] is None
        assert {item["collection"] for item in body["collectionBreakdown"]} == {
            "zlib3_records", "upload_records"
        }
        assert body["collectionBreakdownAvailable"] is True
        assert body["records"] == 0
        assert sum(item["share"] for item in body["collectionBreakdown"]) == 0

    def test_collection_breakdown_uses_current_record_provenance(
        self, client, db_conn
    ):
        db_conn.execute(
            """
            INSERT INTO metadata_records (md5, source_collection, search_tsv)
            VALUES
                (decode('00000000000000000000000000000001', 'hex'), 'zlib3_records', ''::tsvector),
                (decode('00000000000000000000000000000002', 'hex'), 'zlib3_records', ''::tsvector),
                (decode('00000000000000000000000000000003', 'hex'), 'upload_records', ''::tsvector),
                (decode('00000000000000000000000000000004', 'hex'), 'upload_records', ''::tsvector)
            """
        )
        db_conn.execute(
            """
            INSERT INTO sync_releases
                (collection, release_identifier, status, records_inserted,
                 records_updated, completed_at, discovered_at)
            VALUES ('upload_records', 'latest', 'completed', 0, 2, now(), now())
            """
        )

        body = client.get("/api/v1/sync/status").json()
        breakdown = {item["collection"]: item for item in body["collectionBreakdown"]}

        assert body["records"] == 4
        assert body["collectionBreakdownAvailable"] is True
        assert breakdown["zlib3_records"]["estimatedRecords"] == 2
        assert breakdown["upload_records"]["estimatedRecords"] == 2
        assert breakdown["zlib3_records"]["share"] == 0.5
        assert breakdown["upload_records"]["share"] == 0.5
        assert breakdown["zlib3_records"]["estimatedDatabaseBytes"] == round(
            body["databaseSizeBytes"] / 2
        )
        assert breakdown["upload_records"]["estimatedDatabaseBytes"] == round(
            body["databaseSizeBytes"] / 2
        )

    def test_performance_chart_has_interactive_detail_surface(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        for fragment in (
            "spark-wrap",
            "hit-area",
            "spark-tooltip",
            "pointermove",
            "Pfeiltasten",
        ):
            assert fragment in response.text

    def test_inactive_release_is_hidden(self, client, db_conn):
        db_conn.execute(
            """
            INSERT INTO sync_releases
                (collection, release_identifier, status, started_at)
            VALUES ('gbooks_records', 'old_release', 'importing', now())
            """
        )
        body = client.get("/api/v1/sync/status").json()
        assert body["activeSync"] is None
        assert body["recentReleases"] == []
        assert body["releasesTracked"] == 0

    def test_collection_mode_round_trip(self, client):
        url = "/api/v1/sync/collections/upload_records/mode"
        assert client.get(url).json()["mode"] == "auto"
        response = client.post(url, json={"mode": "import"})
        assert response.status_code == 200
        assert response.json()["mode"] == "import"
        assert client.get(url).json()["mode"] == "import"

    def test_collection_mode_rejects_inactive_source(self, client):
        response = client.post(
            "/api/v1/sync/collections/gbooks_records/mode",
            json={"mode": "import"},
        )
        assert response.status_code == 400

    def test_active_sync_visible(self, client, db_conn):
        db_conn.execute(
            """
            INSERT INTO sync_releases
                (collection, release_identifier, status,
                 download_done_bytes, download_total_bytes,
                 records_seen, records_inserted, records_discarded, started_at)
            VALUES ('zlib3_records', 'rel_live', 'importing', 524288000, 1073741824,
                    1234, 1200, 34, now())
            """
        )
        body = client.get("/api/v1/sync/status").json()
        active = body["activeSync"]
        assert active is not None
        assert active["status"] == "importing"
        assert active["collection"] == "zlib3_records"
        assert active["downloadDoneBytes"] == 524288000
        assert active["downloadTotalBytes"] == 1073741824
        assert active["recordsSeen"] == 1234
        assert active["recordsDiscarded"] == 34
        # It also shows up in the collections list and recent releases.
        assert any(r["releaseIdentifier"] == "rel_live" for r in body["recentReleases"])

    def test_import_analytics_and_discard_reasons_visible(self, client, db_conn):
        db_conn.execute(
            """
            INSERT INTO sync_releases
                (collection, release_identifier, status, import_started_at,
                 import_done_bytes, import_total_bytes, records_seen, records_discarded,
                 discard_reasons, discard_samples, started_at)
            VALUES
                ('upload_records', 'analytics', 'importing', now() - interval '10 seconds',
                 1048576, 2097152, 1000, 700,
                 '{"missing_title_or_author": 700}',
                 jsonb_build_object(
                   'missing_title_or_author',
                   jsonb_build_array(jsonb_build_object(
                     'title', null, 'authors', jsonb_build_array(), 'sourceRecordId', 'sample-1'
                   ))
                 ),
                 now() - interval '1 minute')
            """
        )
        body = client.get("/api/v1/sync/status").json()
        release = next(r for r in body["recentReleases"] if r["releaseIdentifier"] == "analytics")
        assert release["importDurationSeconds"] >= 9
        assert release["importTimingEstimated"] is False
        assert release["importStartedAt"] is not None
        assert release["discardReasons"] == {"missing_title_or_author": 700}
        assert body["discardAnalysis"] == [
            {
                "reason": "missing_title_or_author",
                "count": 700,
                "samples": [
                    {"title": None, "authors": [], "sourceRecordId": "sample-1"}
                ],
            }
        ]

    def test_import_history_and_subcollection_stats(self, client, db_conn):
        release_id = db_conn.execute(
            """
            INSERT INTO sync_releases
                (collection, release_identifier, status, records_seen,
                 import_done_bytes, discard_reasons, discovered_at)
            VALUES ('upload_records', 'filters', 'importing', 600, 9000,
                    '{"blocked_subcollection:aaaaarg": 25}', now())
            RETURNING id
            """
        ).fetchone()[0]
        db_conn.execute(
            """
            INSERT INTO import_performance_buckets
                (release_id, bucket_start, first_sample_at, last_sample_at,
                 first_records_seen, last_records_seen, first_bytes, last_bytes)
            VALUES (%s, date_bin('5 minutes', now(), TIMESTAMPTZ '2001-01-01'),
                    now() - interval '60 seconds', now(), 0, 600, 0, 9000)
            """,
            (release_id,),
        )
        body = client.get("/api/v1/sync/status").json()
        assert body["importHistory"][0]["recordsPerSecond"] == 10
        item = next(
            value for value in body["subcollectionFilters"]
            if value["subcollection"] == "aaaaarg"
        )
        assert item["latestFiltered"] == 25
        assert item["totalFiltered"] == 25
        assert item["releases"] == [{"releaseIdentifier": "filters", "count": 25}]

    def test_subcollection_filter_override_round_trip(self, client):
        response = client.post(
            "/api/v1/sync/subcollections/aaaaarg", json={"blocked": False}
        )
        assert response.status_code == 200
        body = client.get("/api/v1/sync/status").json()
        item = next(
            value for value in body["subcollectionFilters"]
            if value["subcollection"] == "aaaaarg"
        )
        assert item["blocked"] is False
        assert item["hasOverride"] is True
        assert item["effectiveSince"] is not None

    def test_reanalysis_stats_override_import_counter(self, client, db_conn):
        release_id = db_conn.execute(
            """
            INSERT INTO sync_releases
                (collection, release_identifier, status, discard_reasons)
            VALUES ('upload_records', 'reanalyzed', 'completed',
                    '{"blocked_subcollection:aaaaarg": 5}')
            RETURNING id
            """
        ).fetchone()[0]
        job_id = db_conn.execute(
            """
            INSERT INTO filter_analysis_jobs
                (release_id, status, filters_snapshot, records_scanned, completed_at)
            VALUES (%s, 'completed', '{"aaaaarg": true}', 100, now())
            RETURNING id
            """,
            (release_id,),
        ).fetchone()[0]
        db_conn.execute(
            """
            INSERT INTO release_subcollection_stats
                (release_id, subcollection, matching_records, filter_blocked,
                 analyzed_at, analysis_job_id)
            VALUES (%s, 'aaaaarg', 19, true, now(), %s)
            """,
            (release_id, job_id),
        )
        body = client.get("/api/v1/sync/status").json()
        item = next(
            value for value in body["subcollectionFilters"]
            if value["subcollection"] == "aaaaarg"
        )
        assert item["latestFiltered"] == 19
        assert item["releases"][0]["analyzedAt"] is not None
        assert body["filterAnalysisJobs"][0]["status"] == "completed"

    def test_filter_analysis_endpoint_queues_retained_release(
        self, client, db_conn, tmp_path, monkeypatch
    ):
        from app.routes import control

        release_id = db_conn.execute(
            """
            INSERT INTO sync_releases (collection, release_identifier, status)
            VALUES ('upload_records', 'retained-release', 'completed') RETURNING id
            """
        ).fetchone()[0]
        db_conn.execute(
            """
            INSERT INTO collection_sync_modes
                (collection, mode, last_imported_identifier)
            VALUES ('upload_records', 'auto', 'retained-release')
            """
        )
        payload = tmp_path / ".prev" / "upload_records.payload"
        payload.parent.mkdir()
        payload.write_bytes(b"payload")
        monkeypatch.setattr(control, "WORK_DIR", tmp_path)
        captured = {}

        def fake_enqueue(conn, rid, snapshot, total_bytes):
            captured.update(release_id=rid, snapshot=snapshot, total_bytes=total_bytes)
            return 42, True

        monkeypatch.setattr(control, "enqueue_filter_analysis", fake_enqueue)
        response = client.post(
            "/api/v1/sync/filter-analysis", json={"release_id": release_id}
        )
        assert response.status_code == 202
        assert response.json() == {
            "queued": True, "created": True, "id": 42, "releaseId": release_id
        }
        assert captured["release_id"] == release_id
        assert captured["snapshot"]["aaaaarg"] is True
        assert captured["total_bytes"] == 7

    def test_delete_retained_payload(self, client, db_conn, tmp_path, monkeypatch):
        from app.routes import control

        payload = tmp_path / ".prev" / "upload_records.payload"
        payload.parent.mkdir()
        payload.write_bytes(b"payload")
        db_conn.execute(
            """
            INSERT INTO collection_sync_modes
                (collection, mode, last_imported_identifier)
            VALUES ('upload_records', 'import', 'release-1')
            """
        )
        monkeypatch.setattr(control, "WORK_DIR", Path(tmp_path))
        response = client.delete("/api/v1/sync/payloads/upload_records")
        assert response.status_code == 200
        assert response.json()["deletedBytes"] == 7
        assert not payload.exists()
        row = db_conn.execute(
            "SELECT mode, last_imported_identifier FROM collection_sync_modes "
            "WHERE collection = 'upload_records'"
        ).fetchone()
        assert row == ("auto", None)


class TestDashboardHtml:
    def test_root_redirects(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code in (301, 302, 307)
        assert "/dashboard" in response.headers["location"]

    def test_dashboard_page_served(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 200
        html = response.text
        assert "AA Metadata Worker" in html
        assert "/api/v1/sync/status" in html  # the page polls this endpoint
        assert "setInterval(tick" not in html  # slow requests must never overlap
        assert "Status wird geladen" in html
        assert "Import-Leistung" in html
        assert "Filteranalyse" in html
        assert "Subcollection-Filter hinzufügen" in html
        assert "toggle-track" in html
        assert "Werte neu auswerten" in html
        assert "aktuell gespeicherten Datensätzen (Provenienz)" in html
        assert "collectionBreakdownAvailable" in html
        assert "<h2>Releases</h2>" in html
        assert "active-details" in html
        assert "release-logs" in html
        assert "captureDetailsOpenState" in html
        assert "restoreDetailsOpenState" in html
        assert 'data-detail-key="filter-analysis"' in html
        assert 'data-detail-key="release-logs"' in html
        assert "poll-cadence" not in html
        assert "<h2>Status</h2>" not in html
        assert "Gespeicherte Importdateien" not in html

    def test_dashboard_exempt_from_auth(self, db_conn, monkeypatch):
        monkeypatch.setenv("METADATA_API_KEY", "secret")
        close_pool()
        app = create_app()
        with TestClient(app) as client:
            assert client.get("/dashboard").status_code == 200
            assert client.get("/api/v1/sync/status").status_code == 200
            # Record endpoints stay protected; search is used by the dashboard.
            assert client.get("/api/v1/records/" + "ab" * 16).status_code == 401
        monkeypatch.delenv("METADATA_API_KEY")
        close_pool()
