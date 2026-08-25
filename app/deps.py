"""API dependencies: settings, DB pool connection, auth middleware helper."""

from __future__ import annotations

import psycopg
from fastapi import Depends, Request

from common.config import Settings, load_settings
from common.db import get_pool


def get_settings() -> Settings:
    return load_settings()


def get_connection(request: Request) -> psycopg.Connection:
    """Borrow a pooled connection for one request; always returned clean."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.rollback()  # discard any open transaction; no-op in autocommit
    except BaseException:
        try:
            conn.rollback()
        except psycopg.Error:
            pass
        raise
    finally:
        pool.putconn(conn)


GetConnectionDependency = Depends(get_connection)
GetSettingsDependency = Depends(get_settings)
