"""Custom ASGI middleware for CrisisMap.

Middleware stack (applied in the order they appear in ``main.py``):

RequestIDMiddleware
    Generates or propagates a UUID ``X-Request-ID`` on every request and
    binds it to the structlog context so every log line carries it.

SecurityHeadersMiddleware
    Adds defensive HTTP response headers to every response.

SanitisationMiddleware
    Strips HTML/script tags from every string field in JSON request bodies
    using ``bleach``.  Binary multipart content is excluded.

RateLimitHeaderMiddleware
    Injects ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``, and
    ``Retry-After`` headers onto HTTP 429 responses produced by slowapi.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import bleach
import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# RequestIDMiddleware
# ---------------------------------------------------------------------------

_MAX_REQUEST_ID_LENGTH = 128


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

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        raw = request.headers.get("X-Request-ID", "")
        request_id = raw[:_MAX_REQUEST_ID_LENGTH].strip() or str(uuid.uuid4())
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


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware
# ---------------------------------------------------------------------------

_SECURITY_HEADERS: dict[str, str] = {
    # Prevent MIME-type sniffing.
    "X-Content-Type-Options": "nosniff",
    # Disallow framing to block clickjacking.
    "X-Frame-Options": "DENY",
    # HSTS: enforce HTTPS for one year including subdomains.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # Limit referrer information sent to third-party origins.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Restrict browser feature access to first-party only.
    "Permissions-Policy": "geolocation=(self), camera=(self)",
    # API responses carry no HTML, so a restrictive CSP is appropriate.
    "Content-Security-Policy": "default-src 'none'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach defensive security headers to every HTTP response.

    The header set follows OWASP recommendations for REST API services where
    responses are JSON, not HTML.  The ``Content-Security-Policy`` is
    intentionally strict (``default-src 'none'``) because no browser
    resources are served from this API.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


# ---------------------------------------------------------------------------
# SanitisationMiddleware
# ---------------------------------------------------------------------------

# Maximum character length applied as a second-layer defence for string
# fields.  Individual Pydantic schema fields carry their own tighter limits;
# this cap prevents extremely long strings from reaching the validator at all.
_GLOBAL_STRING_MAX_LENGTH = 10_000

# Content-type prefixes that carry JSON bodies we should sanitise.
_JSON_CONTENT_TYPES = ("application/json",)

# Content-type prefixes that carry binary/multipart data we must skip.
_SKIP_CONTENT_TYPES = ("multipart/form-data", "application/octet-stream")


def _sanitise_value(value: Any, max_length: int = _GLOBAL_STRING_MAX_LENGTH) -> Any:
    """Recursively sanitise a parsed JSON value.

    * Strings: strip HTML/script tags with bleach, then truncate.
    * Dicts: sanitise each value recursively.
    * Lists: sanitise each element recursively.
    * All other types: returned unchanged.
    """
    if isinstance(value, str):
        cleaned = bleach.clean(value, tags=[], strip=True)
        return cleaned[:max_length]
    if isinstance(value, dict):
        return {k: _sanitise_value(v, max_length) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitise_value(item, max_length) for item in value]
    return value


class SanitisationMiddleware(BaseHTTPMiddleware):
    """Strip HTML and script tags from JSON request bodies globally.

    Only ``application/json`` request bodies are processed.
    ``multipart/form-data`` and ``application/octet-stream`` bodies are
    passed through untouched so that binary uploads are not corrupted.

    The sanitised body is re-encoded and stored on
    ``request.state.sanitised_body`` so that downstream middleware or route
    handlers can access the raw bytes if needed.  The ASGI ``receive``
    callable is replaced with one that returns the sanitised bytes, making
    the process transparent to FastAPI.

    Malformed JSON bodies are passed through unchanged; Pydantic validation
    will reject them with a 422 response in the normal flow.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        content_type = request.headers.get("content-type", "").lower()

        # Skip binary/multipart payloads immediately.
        if any(content_type.startswith(skip) for skip in _SKIP_CONTENT_TYPES):
            return await call_next(request)

        # Only process JSON bodies.
        if not any(content_type.startswith(ct) for ct in _JSON_CONTENT_TYPES):
            return await call_next(request)

        # Read and parse the body.
        try:
            raw_body = await request.body()
        except Exception:
            return await call_next(request)

        if not raw_body:
            return await call_next(request)

        try:
            parsed = json.loads(raw_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Pass malformed JSON through; Pydantic will reject it.
            return await call_next(request)

        sanitised = _sanitise_value(parsed)
        sanitised_bytes = json.dumps(sanitised, ensure_ascii=False).encode("utf-8")

        # Patch the body cache on the request scope so both TestClient
        # and the real ASGI server see the sanitised bytes.
        request._body = sanitised_bytes  # type: ignore[attr-defined]

        # Replace the receive callable so FastAPI reads the sanitised body.
        async def _receive() -> dict:
            return {
                "type": "http.request",
                "body": sanitised_bytes,
                "more_body": False,
            }

        request = Request(request.scope, _receive)
        return await call_next(request)


# ---------------------------------------------------------------------------
# RateLimitHeaderMiddleware
# ---------------------------------------------------------------------------


class RateLimitHeaderMiddleware(BaseHTTPMiddleware):
    """Inject rate-limit information headers onto HTTP 429 responses.

    slowapi raises a 429 when a route's limit is exceeded.  This middleware
    inspects every outgoing response and, for 429s, adds:

    ``X-RateLimit-Limit``
        The maximum number of requests permitted in the window.
        Extracted from the slowapi ``detail`` string when available.

    ``X-RateLimit-Remaining``
        Always ``0`` on a 429; the caller has exhausted the window.

    ``Retry-After``
        Seconds until the window resets.  Extracted from the slowapi
        ``detail`` string when available, defaulting to 60.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response: Response = await call_next(request)

        if response.status_code != 429:
            return response

        # Attempt to extract limit/reset information from the response body.
        limit_value = "unknown"
        retry_after = "60"

        try:
            body_bytes = b""
            async for chunk in response.body_iterator:  # type: ignore[attr-defined]
                body_bytes += chunk
            body_str = body_bytes.decode("utf-8")
            data = json.loads(body_str)
            detail = data.get("detail", "") or data.get("error", "")
            # slowapi detail format: "X per Y second" or similar.
            if "per" in detail:
                parts = detail.split()
                if parts:
                    limit_value = parts[0]
                # Attempt to derive retry_after from the window unit.
                unit_map = {
                    "second": "1",
                    "minute": "60",
                    "hour": "3600",
                    "day": "86400",
                }
                for unit, seconds in unit_map.items():
                    if unit in detail:
                        retry_after = seconds
                        break
        except Exception:
            body_bytes = b""

        from starlette.responses import Response as StarletteResponse

        new_response = StarletteResponse(
            content=body_bytes,
            status_code=429,
            media_type=response.headers.get("content-type", "application/json"),
        )
        # Copy original headers then add rate-limit headers.
        for key, value in response.headers.items():
            if key.lower() not in ("content-length",):
                new_response.headers[key] = value

        new_response.headers["X-RateLimit-Limit"] = limit_value
        new_response.headers["X-RateLimit-Remaining"] = "0"
        new_response.headers["Retry-After"] = retry_after
        return new_response
