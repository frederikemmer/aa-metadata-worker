"""API tests against the real test PostgreSQL via TestClient (lifespan runs)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from common.db import close_pool


@pytest.fixture()
def client(db_conn, seeded_records):
    close_pool()  # ensure pool uses current env DSN
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client
    close_pool()


@pytest.fixture()
def seeded_records(db_conn):
    """Seed a few records directly through the importer."""
    from sync.importer import import_release
    from tests.conftest import make_zst

    books = [
        {
            "aacid": "aacid__zlib3_records__20250101T000000Z__201__Aaaa",
            "metadata": {
                "md5_reported": "ab" * 16,
                "title": "Der Schwarm",
                "author": "Frank Schätzing",
                "publisher": "Fischer",
                "year": "2004",
                "language": "german",
                "extension": "epub",
                "filesize_reported": 2500000,
                "isbns": ["978-3-596-17556-7"],
            },
        },
        {
            "aacid": "aacid__zlib3_records__20250101T000000Z__202__Bbbb",
            "metadata": {
                "md5_reported": "cd" * 16,
                "title": "Limit",
                "author": "Frank Schätzing",
                "year": "2009",
                "language": "german",
                "extension": "pdf",
                "filesize_reported": 8800000,
            },
        },
    ]
    release_id = db_conn.execute(
        "INSERT INTO sync_releases (collection, release_identifier) "
        "VALUES ('zlib3_records', 'api_seed') RETURNING id"
    ).fetchone()[0]
    payload = make_zst(books, __import__("pathlib").Path("/tmp/opencode/api_seed.zst"))
    import_release(db_conn, "zlib3_records", payload, release_id)


class TestHealth:
    def test_live(self, client):
        response = client.get("/api/v1/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "live"

    def test_ready(self, client):
        response = client.get("/api/v1/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["ready"] is True
        assert body["schemaVersion"] >= 1


class TestStatus:
    def test_status_shape(self, client):
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        body = response.json()
        for key in ("ready", "records", "collections", "databaseSizeBytes", "diskFreeBytes", "sync"):
            assert key in body
        assert body["records"] >= 2
        assert body["ready"] is True


class TestSearchEndpoint:
    def test_search_found(self, client):
        response = client.get("/api/v1/search", params={"q": "Schwarm"})
        assert response.status_code == 200
        results = response.json()["results"]
        assert any(r["md5"] == "ab" * 16 for r in results)

    def test_search_response_fields(self, client):
        response = client.get("/api/v1/search", params={"author": "schätzing", "extension": "epub"})
        record = response.json()["results"][0]
        assert set(record.keys()) >= {
            "md5",
            "title",
            "authors",
            "languages",
            "format",
            "identifiers",
            "source",
        }
        assert record["identifiers"]["isbn13"] == ["9783596175567"]
        # No download URLs ever appear in search responses.
        assert "download" not in response.text.lower()

    def test_empty_query_rejected(self, client):
        response = client.get("/api/v1/search")
        assert response.status_code == 400

    def test_limit_clamped(self, client):
        response = client.get("/api/v1/search", params={"q": "a", "limit": 5000})
        assert response.status_code == 422  # validated by FastAPI (le=100)

    def test_invalid_isbn(self, client):
        response = client.get("/api/v1/search", params={"isbn": "not-an-isbn"})
        assert response.status_code == 400

    def test_query_too_long(self, client):
        response = client.get("/api/v1/search", params={"q": "x" * 300})
        assert response.status_code == 422

    def test_pagination_cursor(self, client):
        page1 = client.get("/api/v1/search", params={"author": "schatzing", "limit": 1}).json()
        assert page1["nextCursor"]
        page2 = client.get(
            "/api/v1/search", params={"author": "schatzing", "limit": 10, "cursor": page1["nextCursor"]}
        ).json()
        md5s_page1 = {r["md5"] for r in page1["results"]}
        md5s_page2 = {r["md5"] for r in page2["results"]}
        assert not (md5s_page1 & md5s_page2)

    def test_language_filter_2letter(self, client):
        response = client.get("/api/v1/search", params={"language": "de"})
        assert all(r["languages"] == ["deu"] for r in response.json()["results"])


class TestRecordEndpoint:
    def test_get_record(self, client):
        response = client.get("/api/v1/records/" + "ab" * 16)
        assert response.status_code == 200
        assert response.json()["title"] == "Der Schwarm"

    def test_invalid_md5_400(self, client):
        response = client.get("/api/v1/records/nothex")
        assert response.status_code == 400

    def test_missing_record_404(self, client):
        response = client.get("/api/v1/records/" + "00" * 16)
        assert response.status_code == 404


class TestSourcesEndpoint:
    def test_sources_reference_only(self, client):
        response = client.get("/api/v1/records/" + "ab" * 16 + "/sources")
        assert response.status_code == 200
        body = response.json()
        assert body["aaPageUrl"].endswith("/md5/" + "ab" * 16)
        assert "download" not in body["aaPageUrl"]

    def test_deleted_record_gone(self, client, db_conn):
        db_conn.execute(
            "UPDATE metadata_records SET deleted = TRUE WHERE md5 = %s", (bytes.fromhex("cd" * 16),)
        )
        response = client.get("/api/v1/records/" + "cd" * 16 + "/sources")
        assert response.status_code == 410


class TestOpenApi:
    def test_openapi_available(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        spec = response.json()
        assert "/api/v1/search" in spec["paths"]
        assert "/api/v1/records/{md5}" in spec["paths"]
        assert spec["info"]["version"]


class TestAuth:
    @pytest.fixture()
    def authed_client(self, db_conn, seeded_records, monkeypatch):
        monkeypatch.setenv("METADATA_API_KEY", "secret-key")
        close_pool()
        app = create_app()
        with TestClient(app) as test_client:
            yield test_client
        monkeypatch.delenv("METADATA_API_KEY")
        close_pool()

    def test_requires_bearer(self, authed_client):
        response = authed_client.get("/api/v1/search", params={"q": "x"})
        assert response.status_code == 401

    def test_health_exempt(self, authed_client):
        assert authed_client.get("/api/v1/health/live").status_code == 200

    def test_valid_key_accepted(self, authed_client):
        response = authed_client.get(
            "/api/v1/search",
            params={"q": "Schwarm"},
            headers={"Authorization": "Bearer secret-key"},
        )
        assert response.status_code == 200
