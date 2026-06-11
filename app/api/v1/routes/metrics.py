"""Prometheus metrics exposition endpoint.

GET /metrics

Protected by either:
  1. A static ``METRICS_TOKEN`` bearer token (preferred for scraping agents),
  2. An ``admin`` role JWT (for human inspection via the dashboard).

The endpoint generates the full Prometheus text format response from the
default registry, which includes both the custom CrisisMap metrics defined
in ``app/core/metrics.py`` and the process/platform metrics automatically
registered by ``prometheus_client``.

The ``prometheus-fastapi-instrumentator`` library automatically instruments
all FastAPI routes (latency, request counts, status codes) when
``Instrumentator().instrument(app).expose(app)`` is called in ``main.py``.
This module provides the *authenticated* replacement for that default
``/metrics`` endpoint so the instrumentator is initialised with
``should_group_status_codes=False`` and ``expose`` is NOT called — we expose
here instead.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import settings
from app.services.auth_service import Role

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Observability"])


def _authorise_metrics(request: Request) -> None:
    """Raise HTTP 401/403 if the caller is not permitted to scrape metrics.

    Accepted credentials (in priority order):
    1. ``Authorization: Bearer <METRICS_TOKEN>`` matching the static token.
    2. A valid JWT with ``role == admin`` (validated upstream by
       ``require_role``).  Because this function is called inline rather than
       via ``Depends``, the JWT path requires the caller to have already
       passed through ``get_current_user`` — in practice this means the
       admin dashboard.

    Design note: a static token is preferred for Prometheus scrape jobs so
    that the scraper does not need a JWT rotation mechanism.
    """
    auth_header: str = request.headers.get("Authorization", "")

    # --- Static METRICS_TOKEN path ---
    metrics_token: str = settings.METRICS_TOKEN or ""
    if metrics_token:
        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() == "bearer" and token == metrics_token:
            return  # Authorised via static token

    # --- JWT admin-role path ---
    # If the request arrived here with a valid JWT (injected by
    # RequestIDMiddleware / get_current_user earlier in the chain),
    # the role claim is available on request.state.
    user: dict = getattr(request.state, "current_user", {})
    if user.get("role") == Role.admin.value:
        return  # Authorised via admin JWT

    # Neither credential was valid
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication credentials were not provided.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Metrics access requires admin role or a valid METRICS_TOKEN.",
    )


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    description=(
        "Exposes all registered Prometheus metrics in the standard text "
        "exposition format. Protected by ``METRICS_TOKEN`` bearer token or "
        "``admin`` JWT role."
    ),
    response_class=Response,
    responses={
        200: {
            "content": {"text/plain; version=0.0.4; charset=utf-8": {}},
            "description": "Prometheus text-format metrics.",
        },
        401: {"description": "No credentials supplied."},
        403: {"description": "Insufficient permissions."},
    },
)
async def metrics_endpoint(request: Request) -> Response:
    """Return Prometheus metrics in text exposition format."""
    _authorise_metrics(request)
    remote = request.client.host if request.client else "unknown"
    logger.debug("metrics_scraped", remote=remote)
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
