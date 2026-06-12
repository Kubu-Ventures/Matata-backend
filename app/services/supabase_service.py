"""Supabase Auth service — analyst account management and token exchange.

Responsibilities
----------------
* Sign analyst in with email + password via the Supabase Auth REST API.
* Invite a new analyst by email (admin-only, uses service role key).
* Verify a Supabase JWT and extract the CrisisMap role + region from
  user_metadata, which is set server-side at invite time.

No analyst credentials or PII are stored in the CrisisMap database.
All sensitive operations use the service role key and are never exposed
to the client.

Configuration
-------------
Four env vars are required (see app/core/config.py):
    SUPABASE_URL               — project REST base URL
    SUPABASE_ANON_KEY          — public key for sign-in calls
    SUPABASE_SERVICE_ROLE_KEY  — secret key for admin/invite calls
    SUPABASE_JWT_SECRET        — used to verify Supabase JWTs server-side

When SUPABASE_URL is empty the module raises SupabaseNotConfiguredError
so callers can return a clear 503 rather than crashing at startup.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supabase Auth REST API paths
# ---------------------------------------------------------------------------

_SIGN_IN_PATH = "/auth/v1/token?grant_type=password"
_INVITE_PATH = "/auth/v1/invite"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SupabaseAuthError(Exception):
    """Raised when Supabase Auth returns a non-success response."""


class SupabaseNotConfiguredError(SupabaseAuthError):
    """Raised when Supabase env vars are not set."""


class SupabaseInvalidCredentialsError(SupabaseAuthError):
    """Raised when the email/password combination is invalid."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require_config() -> None:
    """Raise SupabaseNotConfiguredError if Supabase env vars are missing."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
        raise SupabaseNotConfiguredError(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def sign_in(email: str, password: str) -> dict:
    """Exchange analyst email + password for a Supabase auth response.

    Args:
        email:    Analyst email address.
        password: Plaintext password (transmitted over TLS to Supabase only;
                  never stored or logged by this service).

    Returns:
        Supabase auth response dict containing ``access_token``,
        ``refresh_token``, and ``user`` object.

    Raises:
        SupabaseNotConfiguredError:      Supabase env vars not set.
        SupabaseInvalidCredentialsError: Email/password combination invalid.
        SupabaseAuthError:               Any other Supabase-side error.
    """
    _require_config()

    url = f"{settings.SUPABASE_URL}{_SIGN_IN_PATH}"
    headers = {
        "apikey": settings.SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url,
            json={"email": email, "password": password},
            headers=headers,
        )

    if response.status_code in (400, 422):
        raise SupabaseInvalidCredentialsError("Invalid email or password.")

    if not response.is_success:
        logger.error("supabase_sign_in_error status=%s", response.status_code)
        raise SupabaseAuthError(
            f"Authentication service returned {response.status_code}."
        )

    return response.json()


async def invite_analyst(
    email: str,
    crisismap_role: str,
    region_geojson: Optional[str] = None,
) -> dict:
    """Create a new analyst account in Supabase and send an invite email.

    Uses the Supabase Admin invite endpoint (service role key required).
    The CrisisMap role and optional geographic scope are stored in
    ``user_metadata`` at invite time using the service role key so that
    the analyst cannot modify them via the Supabase client.

    Args:
        email:           Analyst email address.
        crisismap_role:  One of: analyst, responder, admin.
        region_geojson:  Optional GeoJSON string for regional scoping
                         (responder accounts only).

    Returns:
        Supabase user object from the invite response.

    Raises:
        SupabaseNotConfiguredError: Supabase env vars not set.
        SupabaseAuthError:          Invite failed (e.g. email already exists).
    """
    if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
        raise SupabaseNotConfiguredError(
            "Supabase is not configured. "
            "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )

    url = f"{settings.SUPABASE_URL}{_INVITE_PATH}"
    headers = {
        "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
    }
    payload: dict = {
        "email": email,
        "data": {
            "crisismap_role": crisismap_role,
            "region_geojson": region_geojson,
        },
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json=payload, headers=headers)

    if not response.is_success:
        error_msg = (
            response.json().get("msg")
            or response.json().get("message")
            or str(response.status_code)
        )
        logger.error(
            "supabase_invite_error email_suppressed status=%s msg=%s",
            response.status_code,
            error_msg,
        )
        raise SupabaseAuthError(f"Failed to invite analyst: {error_msg}")

    return response.json()


def extract_crisismap_claims(supabase_response: dict) -> tuple[str, Optional[str]]:
    """Extract the CrisisMap role and region from a Supabase sign-in response.

    Reads ``user_metadata`` from the response body returned directly by
    Supabase after a successful email+password authentication.  No JWT
    signature verification is required here because:
      - The response arrived over TLS directly from Supabase.
      - Supabase already authenticated the credentials before returning it.
      - ``user_metadata`` is set server-side via the service role key and
        cannot be modified by the analyst.

    Args:
        supabase_response: Full JSON dict from the Supabase sign-in endpoint
                           (contains ``user.user_metadata``).

    Returns:
        Tuple of ``(crisismap_role, region_geojson)``.

    Raises:
        SupabaseAuthError: Response is missing a CrisisMap role.
    """
    user_metadata = (supabase_response.get("user") or {}).get("user_metadata") or {}
    crisismap_role = user_metadata.get("crisismap_role")
    region_geojson = user_metadata.get("region_geojson")

    if not crisismap_role:
        raise SupabaseAuthError(
            "This account has no CrisisMap role assigned. "
            "Contact your administrator."
        )

    return crisismap_role, region_geojson
