"""Centralised rate-limit definitions for CrisisMap.

All per-route rate limits are declared here as named constants so that they
are never scattered across individual route modules.  Route handlers import
the constant they need and pass it to the slowapi limiter decorator.

Usage::

    from app.core.rate_limits import RATE_REPORT_SUBMIT

    @router.post("/reports")
    @limiter.limit(RATE_REPORT_SUBMIT)
    async def submit_report(request: Request, ...):
        ...

Limits use the slowapi string format: "<count> per <period>".
Multiple limits can be combined by passing a list to ``@limiter.limit``.

Rate-limit response headers
---------------------------
The ``RateLimitHeaderMiddleware`` in ``app/core/middleware.py`` injects
``X-RateLimit-Limit``, ``X-RateLimit-Remaining``, and ``Retry-After``
headers on every HTTP 429 response.  Route handlers and services do not
need to set these manually.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Authentication endpoints
# ---------------------------------------------------------------------------

# OTP request: prevent enumeration and SMS flooding.
RATE_OTP_REQUEST: str = "5 per minute"

# OTP verification: prevent brute-force guessing of 6-digit codes.
RATE_OTP_VERIFY: str = "10 per minute"

# Analyst login: prevent credential stuffing.
RATE_ANALYST_LOGIN: str = "10 per minute"

# Token refresh: normal clients refresh once per hour at most.
RATE_TOKEN_REFRESH: str = "20 per minute"

# ---------------------------------------------------------------------------
# Report submission
# ---------------------------------------------------------------------------

# Anonymous and verified report submission.  Set conservatively to limit
# spam while allowing legitimate bursts from field workers with batched data.
RATE_REPORT_SUBMIT: str = "30 per minute"

# Offline bundle sync: typically one batch per reconnection event.
RATE_OFFLINE_SYNC: str = "10 per minute"

# ---------------------------------------------------------------------------
# Media / uploads
# ---------------------------------------------------------------------------

# Image upload per reporter; large enough for multi-photo reports.
RATE_MEDIA_UPLOAD: str = "20 per minute"

# ---------------------------------------------------------------------------
# GIS endpoints
# ---------------------------------------------------------------------------

# Building footprint match: called once per report submission.
RATE_GIS_MATCH: str = "60 per minute"

# ---------------------------------------------------------------------------
# Export endpoints (analyst-only, but still bounded)
# ---------------------------------------------------------------------------

RATE_EXPORT: str = "10 per minute"

# ---------------------------------------------------------------------------
# Public / read endpoints
# ---------------------------------------------------------------------------

# Health check: generous limit for orchestrator probes.
RATE_HEALTH: str = "120 per minute"

# Stats endpoint accessed by dashboards.
RATE_STATS: str = "60 per minute"
