"""AA Metadata Worker REST API (/api/v1).

Metadata-only search service for FE.Library. This API never serves,
proxies or resolves book file downloads.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import control, dashboard, health, records, search
from app.routes import status as status_routes
from common.config import load_settings
from common.db import apply_migrations, close_pool, connect, get_pool

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    app.state.settings = settings
    # Startup: retry DB connection with backoff (compose may start us first).
    applied = []
    for attempt in range(1, 13):
        try:
            conn = connect(settings)
            try:
                applied = apply_migrations(conn)
            finally:
                conn.close()
            break
        except Exception as error:  # noqa: BLE001 - retry until DB is up
            logger.warning(
                "DB not ready (attempt %d/12): %s; retrying in %ds",
                attempt,
                error,
                min(2**attempt, 30),
            )
            import time

            time.sleep(min(2**attempt, 30))
    if applied:
        logger.info("Applied migrations: %s", applied)
    get_pool(settings)  # warm up pool
    yield
    close_pool()


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="AA Metadata Worker API",
        version="1.0.0",
        description=(
            "Local book-metadata search index built from Anna's Archive public "
            "metadata datasets (AAC). Metadata-only: no book files are hosted, "
            "streamed or resolved."
        ),
        openapi_url="/openapi.json",
        docs_url="/docs",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(status_routes.router)
    app.include_router(control.router)
    app.include_router(search.router)
    app.include_router(records.router)
    app.include_router(dashboard.router)

    @app.middleware("http")
    async def bearer_auth(request, call_next):
        import hmac as hmac_module

        from fastapi.responses import JSONResponse

        expected = getattr(app.state, "settings", settings).api_key
        path = request.url.path
        auth_exempt = (
            path.startswith("/api/v1/health")
            or path == "/api/v1/sync/status"  # read-only progress data for the dashboard
            or path == "/api/v1/sync/control"  # read-only control state for the dashboard
            or path in ("/", "/dashboard", "/openapi.json", "/docs", "/redoc")
            or path.startswith("/docs/")
        )
        if expected and not auth_exempt:
            authorization = request.headers.get("authorization", "")
            provided = (
                authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""
            )
            if not provided or not hmac_module.compare_digest(provided, expected):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid bearer token"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    return app


app = create_app()
