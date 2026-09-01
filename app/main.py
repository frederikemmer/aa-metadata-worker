"""AA Metadata Worker REST API (/api/v1).

Metadata-only search service for FE.Library. This API never serves,
proxies or resolves book file downloads.
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import control, dashboard, editions, health, records, search
from app.routes import status as status_routes
from common.config import load_settings
from common.db import apply_migrations, close_pool, connect, get_pool

logger = logging.getLogger(__name__)

# Module-level stop event shared with the sync worker thread.
_sync_stop = threading.Event()


def _configure_logging() -> None:
    """Ensure the background sync worker logs reach stdout/docker logs.

    Uvicorn configures its own (uvicorn.*) loggers but leaves the root logger
    at WARNING with only a placeholder handler, which silently swallows every
    `logger.info(...)` emitted by sync.*, app.* and common.*.  Install a real
    StreamHandler on the root logger so download/import progress is visible
    in `docker logs` (used for diagnosing stalled torrents).
    """
    root = logging.getLogger()
    # Skip if something already attached a non-stub handler.
    if any(getattr(h, "_aa_installed", False) for h in root.handlers):
        return
    level_name = load_settings().log_level
    root.setLevel(getattr(logging, level_name, logging.INFO))
    handler = logging.StreamHandler(sys.stdout)
    handler._aa_installed = True  # type: ignore[attr-defined]
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)


_configure_logging()


def _start_sync_worker() -> None:
    """Launch the sync scheduler as a daemon thread (non-blocking)."""
    if not load_settings().sync_enabled:
        logger.info("SYNC_ENABLED=false; background sync worker not started.")
        return

    def _run() -> None:
        from sync.worker import run_worker_forever

        try:
            run_worker_forever()
        except Exception:  # noqa: BLE001 - worker must never crash the API
            logger.exception("Sync worker thread failed.")

    t = threading.Thread(target=_run, name="sync-worker", daemon=True)
    t.start()
    logger.info("Background sync worker thread started.")


def _start_filter_analysis_worker() -> threading.Thread:
    """Launch the independent, statistics-only payload scanner."""
    def _run() -> None:
        from sync.filter_analysis import run_filter_analysis_worker_forever

        try:
            run_filter_analysis_worker_forever()
        except Exception:  # noqa: BLE001 - API must survive analysis failures
            logger.exception("Filter analysis worker thread failed.")

    thread = threading.Thread(target=_run, name="filter-analysis-worker", daemon=True)
    thread.start()
    logger.info("Background filter analysis worker started.")
    return thread


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

    # Start sync worker in background (if enabled).
    _start_sync_worker()
    analysis_thread = _start_filter_analysis_worker()

    yield

    # Shutdown: signal the sync worker to stop.
    from sync.worker import request_stop

    request_stop()
    from sync.filter_analysis import request_stop as request_analysis_stop

    request_analysis_stop()
    analysis_thread.join(timeout=3)
    close_pool()


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="AA Metadata Worker API",
        version="1.0.1",
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
    app.include_router(editions.router)
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
