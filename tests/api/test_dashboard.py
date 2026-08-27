"""Dashboard tests: sync/status JSON shape + HTML page availability."""

from __future__ import annotations

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
        ):
            assert key in body, f"missing key {key}"
        assert body["activeSync"] is None
        assert body["records"] == 0
        # Configured collections are always listed (even without releases).
        names = {c["collection"] for c in body["collections"]}
        assert names == {"zlib3_records", "upload_records"}

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
