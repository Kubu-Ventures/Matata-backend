"""Custom ASGI middleware for CrisisMap.

RequestIDMiddleware
-------------------
Generates (or passes through) a UUID ``X-Request-ID`` on every inbound
request and binds it to the structlog context so every log line emitted
during that request lifecycle carries the same ``request_id`` value.

The value is echoed in the response ``X-Request-ID`` header so clients can
correlate their requests with server-side log entries.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Inject a request ID into every request/response cycle.

    If the client supplies an ``X-Request-ID`` header its value is reused
    (after basic length sanitation), otherwise a fresh UUID4 is generated.
    The value is:

    * Stored on ``request.state.request_id`` for route handlers.
    * Bound to the structlog contextvars store for the lifetime of the
      request so all log lines include ``request_id``.
    * Echoed as ``X-Request-ID`` in the response.
    """

    _MAX_ID_LENGTH = 128

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        raw = request.headers.get("X-Request-ID", "")
        request_id = raw[: self._MAX_ID_LENGTH].strip() or str(uuid.uuid4())
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        logger.debug(
            "request_started",
            method=request.method,
            path=request.url.path,
        )

        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response
