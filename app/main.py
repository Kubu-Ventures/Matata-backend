"""FastAPI application entry point.

Registers all routers, middleware, and exception handlers.

Lifespan
--------
The ``lifespan`` context manager handles startup and shutdown tasks:
* Startup — nothing required yet (engine is created lazily on first request).
* Shutdown — disposes the async SQLAlchemy engine, closing all pooled
  connections cleanly so the process exits without resource-leak warnings.

Exception handlers
------------------
All HTTP errors are reshaped to ``{"error": "<detail>"}`` for a consistent
client-facing envelope.  Unhandled exceptions return a generic 500 response
so that internal details are never exposed to clients.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.routes.auth import router as auth_router
from app.api.v1.routes.gis import router as gis_router
from app.api.v1.routes.reports import router as reports_router
from app.core.config import settings
from app.core.dependencies import _engine  # noqa: WPS436 — private import for shutdown
from app.schemas.health import HealthResponse


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """Manage application lifespan resources.

    Startup — no blocking I/O required; the DB engine is created at module
    import time by ``app.core.dependencies`` and begins pooling lazily.

    Shutdown — dispose the async engine to close all pooled connections.
    Without this step, uvicorn may log resource-leak warnings on exit.
    """
    yield
    # Gracefully close all SQLAlchemy pooled connections on shutdown.
    await _engine.dispose()


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Reshape FastAPI/Starlette HTTPException to a consistent error envelope.

    The signature accepts ``Exception`` to satisfy Starlette's type contract,
    but the handler is only registered for ``HTTPException`` instances.
    """
    http_exc = exc if isinstance(exc, HTTPException) else HTTPException(status_code=500)
    return JSONResponse(
        status_code=http_exc.status_code,
        content={"error": http_exc.detail},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return a generic 500 for any unhandled exception.

    Internal details are intentionally suppressed — they are available in
    application logs but must never be forwarded to clients.
    """
    return JSONResponse(
        status_code=500,
        content={"error": "An unexpected error occurred."},
    )


app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth_router, prefix="/api/v1")
app.include_router(reports_router, prefix="/api/v1")
app.include_router(gis_router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Returns ``{status: ok}`` when the application is running.",
    tags=["Health"],
)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version="0.1.0")
