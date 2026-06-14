"""FastAPI application entry point.

Registers all routers, middleware, and exception handlers.

Lifespan
--------
The ``lifespan`` context manager handles startup and shutdown tasks:
* Startup — configures structlog and instruments Prometheus.
* Shutdown — disposes the async SQLAlchemy engine.

Exception handlers
------------------
All HTTP errors are reshaped to ``{"error": "<detail>"}`` for a consistent
client-facing envelope.  Unhandled exceptions return a generic 500 response
so that internal details are never exposed to clients.

Both handlers re-attach the ``X-Request-ID`` header so that the value
injected by ``RequestIDMiddleware`` is preserved even when the normal
response path is short-circuited by an exception.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.v1.routes.analyst import analyst_router, stats_router
from app.api.v1.routes.analyst_auth import router as analyst_auth_router
from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.export import router as export_router
from app.api.v1.routes.gis import router as gis_router
from app.api.v1.routes.health import router as health_router
from app.api.v1.routes.voice import router as voice_router
from app.api.v1.routes.metrics import router as metrics_router
from app.api.v1.routes.reports import router as reports_router
from app.core.config import settings
from app.core.dependencies import _async_session_factory, _engine  # noqa: WPS436
from app.core.logging import configure_logging
from app.core.middleware import RequestIDMiddleware

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ANN001
    """Manage application lifespan resources."""
    configure_logging()
    logger.info(
        "startup",
        environment=settings.ENVIRONMENT,
        version="0.1.0",
    )
    await _warn_if_no_building_footprints()
    yield
    await _engine.dispose()
    logger.info("shutdown")


async def _warn_if_no_building_footprints() -> None:
    """Log a WARNING at startup when the building table is empty.

    An empty building table means every report will receive building_id=NULL
    and confidence=0.0 — the GIS matching pipeline produces no results until
    footprints are imported via ``python -m app.cli.import_footprints``.
    """
    from sqlalchemy import text

    try:
        async with _async_session_factory() as db:
            result = await db.execute(text("SELECT COUNT(*) FROM building"))
            count: int = result.scalar_one()
        if count == 0:
            logger.warning(
                "building_table_empty",
                message=(
                    "No building footprints loaded — GPS matching will return "
                    "building_id=NULL for every report. "
                    "Import data with: "
                    "python -m app.cli.import_footprints --source <path>"
                ),
            )
        else:
            logger.info("building_footprints_loaded", count=count)
    except Exception as exc:
        logger.warning("building_footprint_check_failed", error=str(exc))


app = FastAPI(
    title="CrisisMap — Matata Backend",
    version="0.1.0",
    description=(
        "Community crisis damage reporting API for crisis-response field teams. "
        "Supports anonymous and OTP-verified report submission, content moderation, "
        "GIS building footprint matching, and analyst review workflows."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Middleware  (order matters — outermost first)
# ---------------------------------------------------------------------------

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Prometheus instrumentation
# ---------------------------------------------------------------------------
# ``expose=False`` — we serve /metrics ourselves (authenticated) in
# app/api/v1/routes/metrics.py.

Instrumentator(
    should_group_status_codes=False,
    excluded_handlers=["/metrics", "/health", "/health/ready", "/health/worker"],
).instrument(app)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


def _attach_request_id(request: Request, response: JSONResponse) -> JSONResponse:
    """Copy the request ID set by middleware onto an exception response."""
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Reshape HTTPException to a consistent ``{"error": ...}`` envelope."""
    http_exc = exc if isinstance(exc, HTTPException) else HTTPException(status_code=500)
    response = JSONResponse(
        status_code=http_exc.status_code,
        content={"error": http_exc.detail},
    )
    return _attach_request_id(request, response)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a generic 500 — never leak internal details to clients."""
    logger.exception("unhandled_exception", exc_info=exc)
    response = JSONResponse(
        status_code=500,
        content={"error": "An unexpected error occurred."},
    )
    return _attach_request_id(request, response)


app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

# Health endpoints at root — must not carry /api/v1 prefix so orchestrators
# can reach them without knowing the API version.
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(voice_router)

app.include_router(auth_router, prefix="/api/v1")
app.include_router(analyst_auth_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(gis_router, prefix="/api/v1")
app.include_router(analyst_router, prefix="/api/v1")
app.include_router(stats_router, prefix="/api/v1")
app.include_router(export_router, prefix="/api/v1")
