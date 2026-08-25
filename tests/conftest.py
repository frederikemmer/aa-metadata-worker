"""Shared fixtures: unit-level helpers and the PostgreSQL test container."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import psycopg
import pytest
import zstandard

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"
TEST_PG_IMAGE = "postgres:17-alpine"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def postgres_docker():
    """Start a disposable PostgreSQL container for the whole test session."""
    subprocess.run(["docker", "info"], capture_output=True, check=True)
    port = _free_port()
    password = "testpw"
    name = f"aa-metadata-test-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-e",
            "POSTGRES_DB=aa_metadata_test",
            "-p",
            f"127.0.0.1:{port}:5432",
            "--name",
            name,
            TEST_PG_IMAGE,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    dsn = f"host=127.0.0.1 port={port} dbname=aa_metadata_test user=postgres password={password}"

    deadline = time.time() + 60
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            conn = psycopg.connect(dsn, connect_timeout=3)
            conn.close()
            break
        except Exception as error:  # noqa: BLE001
            last_error = error
            time.sleep(1)
    else:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        raise RuntimeError(f"Test PostgreSQL did not become ready: {last_error}")

    os.environ["POSTGRES_HOST"] = "127.0.0.1"
    os.environ["POSTGRES_PORT"] = str(port)
    os.environ["POSTGRES_DB"] = "aa_metadata_test"
    os.environ["POSTGRES_USER"] = "postgres"
    os.environ["POSTGRES_PASSWORD"] = password

    yield dsn

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture()
def db_conn(postgres_docker) -> psycopg.Connection:
    """Fresh migrated database per test (truncate between tests is cheaper)."""
    from common.db import apply_migrations

    conn = psycopg.connect(postgres_docker, autocommit=True)
    apply_migrations(conn)
    yield conn
    # Wipe all data so each test starts clean without recreating the container.
    conn.execute("TRUNCATE metadata_records, sync_releases RESTART IDENTITY CASCADE")
    conn.close()


def make_zst(lines: list[dict] | list[str], target: Path) -> Path:
    """Compress JSONL lines into a .zst file like the AA payloads."""
    target.parent.mkdir(parents=True, exist_ok=True)
    compressor = zstandard.ZstdCompressor(level=3)
    with open(target, "wb") as handle:
        for line in lines:
            payload = line if isinstance(line, str) else json.dumps(line)
            handle.write(compressor.compress(payload.encode("utf-8") + b"\n"))
    return target


def fixture_lines(name: str) -> list[str]:
    return [line for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines() if line.strip()]
