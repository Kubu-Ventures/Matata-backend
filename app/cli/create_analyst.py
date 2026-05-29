"""Management command — provision an analyst, responder, or admin account.

Usage
-----
    python -m app.cli.create_analyst --email analyst@example.com
    python -m app.cli.create_analyst --email admin@example.com --role admin
    python -m app.cli.create_analyst --email ops@example.com --role responder

What it does
------------
1. Validates the e-mail format and the requested role.
2. Calls ``auth_service.issue_analyst_token`` which:
   * Hashes the e-mail address with ``PHONE_HASH_SALT`` (plaintext is
     immediately discarded).
   * Signs a JWT with the requested role claim.
   * Stores a refresh token in Redis.
3. Prints the access token and refresh token to stdout so the operator can
   hand them to the new analyst out-of-band.
4. Writes an audit-log entry for the provisioning event.

Security notes
--------------
* The plaintext e-mail is used only to derive the hash and is never stored,
  logged, or embedded in any token payload.
* Running this command requires direct access to the host/container — it is
  not exposed over HTTP.
* The Redis connection URL is read from ``settings.REDIS_URL``; no credentials
  are accepted on the command line.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

# Module-level imports so that patch("app.cli.create_analyst.issue_analyst_token")
# can find the name as an attribute of this module during unit tests.
# Role and issue_analyst_token are imported here rather than inside _run.
# settings and Redis remain inside _run to avoid triggering pydantic-settings
# validation at import time (important for the test suite).
from app.services.auth_service import Role, issue_analyst_token

logger = logging.getLogger(__name__)

# E-mail pattern — deliberately simple; the goal is to reject obvious garbage,
# not to implement RFC 5322 in full.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_ELEVATED_ROLES = ("analyst", "responder", "admin")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.create_analyst",
        description="Provision an analyst, responder, or admin account.",
    )
    parser.add_argument(
        "--email",
        required=True,
        help="E-mail address for the new account (never stored in plaintext).",
    )
    parser.add_argument(
        "--role",
        default="analyst",
        choices=_ELEVATED_ROLES,
        help="Role to assign.  Defaults to 'analyst'.",
    )
    return parser.parse_args(argv)


async def _run(email: str, role_str: str) -> int:
    """Core async logic — kept separate so it is easily unit-testable.

    Returns:
        Exit code: 0 on success, 1 on failure.
    """
    # Redis and settings are imported here (not at module level) so that
    # pydantic-settings validation is not triggered when the module is first
    # imported in tests that monkeypatch environment variables.
    from redis.asyncio import Redis

    from app.core.config import settings

    # ── Validate inputs ───────────────────────────────────────────────────────
    if not _EMAIL_RE.match(email):
        print(f"[ERROR] Invalid e-mail address supplied.", file=sys.stderr)
        return 1

    try:
        role = Role(role_str)
    except ValueError:
        print(
            f"[ERROR] '{role_str}' is not a valid elevated role. "
            f"Choose from: {', '.join(_ELEVATED_ROLES)}",
            file=sys.stderr,
        )
        return 1

    if role not in (Role.analyst, Role.responder, Role.admin):
        print(
            f"[ERROR] '{role_str}' is not a valid elevated role. "
            f"Choose from: {', '.join(_ELEVATED_ROLES)}",
            file=sys.stderr,
        )
        return 1

    # ── Connect to Redis ──────────────────────────────────────────────────────
    redis: Redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)

    try:
        access_token, refresh_token = await issue_analyst_token(
            email=email,
            role=role,
            redis=redis,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Token issuance failed: {exc}", file=sys.stderr)
        logger.exception("create_analyst: token issuance failed")
        return 1
    finally:
        await redis.aclose()

    # ── Output tokens ─────────────────────────────────────────────────────────
    # The plaintext e-mail is deliberately NOT printed; only the role is shown.
    print(f"[OK] Account provisioned with role '{role.value}'.")
    print()
    print("Access token (1-hour lifetime):")
    print(access_token)
    print()
    print("Refresh token (store securely — single-use, 30-day lifetime):")
    print(refresh_token)
    print()
    print(
        "Hand these tokens to the analyst out-of-band.  "
        "The plaintext e-mail has been discarded."
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    """Entry point called by ``python -m app.cli.create_analyst``."""
    args = _parse_args(argv)
    exit_code = asyncio.run(_run(email=args.email, role_str=args.role))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()