"""Authentication service.

This module is the **single source of truth** for all authentication logic in
CrisisMap.  Route handlers in ``app/api/v1/routes/auth.py`` call this service
and must not contain any Redis operations, hashing, or JWT construction
directly.

Responsibilities
----------------
* Anonymous session token issuance (zero-friction, no registration).
* Privy email OTP login: verify the Privy access + identity tokens and issue
  our own token pair (primary login path for reporters and analysts).
* Legacy phone OTP send/verify flow with SHA-256 hashing and Redis TTL
  management (retained, dormant — ``SMS_GATEWAY=console`` by default).
* JWT construction, signing, and validation (access + refresh tokens).
* Token rotation and logout (Redis denylist).
* Rate-limit lockout tracking for OTP attempts and fixed-window limiting.
* Audit log writes for every authentication event.

Privacy guarantees
------------------
* Login identifiers (Privy DIDs, email addresses, phone numbers) are **never**
  stored in plaintext.  The plaintext is used only for the duration of the
  call that receives it and is immediately discarded.
* No personally identifiable information appears in any log line.  All log
  statements use hashed identifiers or opaque UUIDs.
* The ``PHONE_HASH_SALT`` ensures that a raw SHA-256 rainbow table cannot
  reverse the stored hashes.

JWT payload claims
------------------
``sub``   — hashed identifier (UUID for anonymous; hashed Privy DID or email
            for reporters/analysts; hashed phone for legacy OTP logins)
``role``  — one of the Role enum values
``tier``  — reporter trust tier (int)
``iat``   — issued-at timestamp (set by python-jose)
``exp``   — expiry timestamp (set by python-jose)
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from jose import JWTError, jwt
from redis.asyncio import Redis

from app.core.config import settings
from app.services.sms import SMSDeliveryError, get_sms_gateway

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.analyst_account import AnalystAccount

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role definitions
# ---------------------------------------------------------------------------


class Role(str, Enum):
    """All roles that may appear in a JWT ``role`` claim."""

    anonymous_reporter = "anonymous_reporter"
    reporter = "reporter"
    analyst = "analyst"
    responder = "responder"
    admin = "admin"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base class for authentication errors.  Route handlers map these to HTTP
    responses; no HTTP logic belongs in this service.
    """


class InvalidTokenError(AuthError):
    """JWT is missing, malformed, expired, or on the denylist."""


class InvalidOTPError(AuthError):
    """OTP code does not match the stored value."""


class OTPExpiredError(AuthError):
    """OTP TTL has elapsed."""


class OTPLockedOutError(AuthError):
    """Too many failed OTP attempts; account locked for 15 minutes."""


class OTPNotFoundError(AuthError):
    """No pending OTP found for this identifier (never sent, or already used)."""


class RateLimitedError(AuthError):
    """A fixed-window rate limit for the calling client has been exceeded."""


# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------
# All keys are namespaced to avoid collisions with other Redis consumers.

_NS = "crisismap:auth"


def _otp_key(id_hash: str) -> str:
    """Redis key for the pending OTP value."""
    return f"{_NS}:otp:{id_hash}"


def _otp_attempts_key(id_hash: str) -> str:
    """Redis key for the consecutive failed OTP attempt counter."""
    return f"{_NS}:otp_attempts:{id_hash}"


def _refresh_key(token_id: str) -> str:
    """Redis key for a valid refresh token."""
    return f"{_NS}:refresh:{token_id}"


def _denylist_key(jti: str) -> str:
    """Redis key for a revoked access token (denylist entry)."""
    return f"{_NS}:denylist:{jti}"


def _rate_limit_key(bucket: str) -> str:
    """Redis key for a fixed-window rate-limit counter."""
    return f"{_NS}:ratelimit:{bucket}"


# ---------------------------------------------------------------------------
# Hashing utilities
# ---------------------------------------------------------------------------


def hash_identifier(value: str) -> str:
    """Return the SHA-256 hex digest of ``value`` salted with ``PHONE_HASH_SALT``.

    Used for every login identifier — Privy DIDs, email addresses, and legacy
    phone numbers.  There is nothing phone-specific about it; the salt env var
    simply keeps its historical name.

    The plaintext ``value`` must **not** be logged or stored after calling this
    function.  Callers are responsible for discarding it immediately.

    Args:
        value: The plaintext identifier to hash.

    Returns:
        64-character lowercase hex string.
    """
    salted = settings.PHONE_HASH_SALT.encode() + value.encode()
    return hashlib.sha256(salted).hexdigest()


def _normalise_email(email: str) -> str:
    """Lower-case and strip an email so provisioning and login always match."""
    return email.strip().lower()


# ---------------------------------------------------------------------------
# OTP utilities
# ---------------------------------------------------------------------------

_OTP_TTL_SECONDS = 300  # 5 minutes
_OTP_MAX_ATTEMPTS = 5
_OTP_LOCKOUT_SECONDS = 900  # 15 minutes

# ---------------------------------------------------------------------------
# Test credentials (non-production only)
# ---------------------------------------------------------------------------
# When the configured SMS_GATEWAY can't actually deliver a message (e.g. no
# Africa's Talking credentials yet, or SMS_GATEWAY=console with no log
# access), these fixed phone/OTP pairs let a tester complete the real OTP
# login UI without receiving a real SMS. Strictly disabled when
# ENVIRONMENT=production — see the guard in send_otp() below.
TEST_PHONE_OTPS: dict[str, str] = {
    "+10000000001": "111111",  # reporter-tier test login
    "+10000000002": "222222",  # analyst-tier test login (must be provisioned
    # in analyst_accounts — see scripts/provision_test_analyst.py)
}


def _generate_otp() -> str:
    """Return a cryptographically random 6-digit OTP string.

    ``secrets.randbelow`` is used rather than ``random.randint`` to ensure
    the code is drawn from a CSPRNG.
    """
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------------------------------------------------------------------
# JWT utilities
# ---------------------------------------------------------------------------


def _make_jti() -> str:
    """Return a new UUID string for use as the JWT ``jti`` claim."""
    return str(uuid.uuid4())


def _build_access_token(
    sub: str,
    role: Role,
    tier: int = 0,
    jti: str | None = None,
    region_geojson: str | None = None,
) -> str:
    """Construct and sign a short-lived access token.

    Args:
        sub:  Subject claim — hashed identifier.
        role: Role claim.
        tier: Reporter trust tier (default 0 for non-reporters).
        jti:  Optional explicit JWT ID; a fresh UUID is generated if omitted.
        region_geojson: Optional responder geographic scope (GeoJSON Polygon
            string). When present it is carried as a claim so the analyst
            routes/service can apply the ``ST_Within`` filter — a responder
            with no region claim would otherwise see every report in the
            system. Only provisioned responders carry this; it is omitted
            entirely for reporters, analysts, and admins.

    Returns:
        Signed JWT string.
    """
    now = datetime.now(tz=timezone.utc)
    expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": sub,
        "role": role.value,
        "tier": tier,
        "jti": jti or _make_jti(),
        "iat": now,
        "exp": expire,
    }
    if region_geojson:
        payload["region_geojson"] = region_geojson
    return jwt.encode(
        payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
    )


def decode_access_token(token: str) -> dict:
    """Decode and validate a JWT access token.

    Does **not** check the Redis denylist — callers that need denylist
    checking must call ``verify_access_token`` instead.

    Args:
        token: Raw JWT string from the Authorization header.

    Returns:
        Decoded payload dictionary.

    Raises:
        InvalidTokenError: If the token is malformed, expired, or has an
                           invalid signature.
    """
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except JWTError as exc:
        raise InvalidTokenError("Token validation failed.") from exc


# ---------------------------------------------------------------------------
# Privy token verification (email OTP login)
# ---------------------------------------------------------------------------

_PRIVY_ISSUER = "privy.io"


def verify_privy_token(token: str) -> dict:
    """Verify a Privy-issued JWT (access token or identity token) and return
    its claims.

    Privy access and identity tokens are both ES256 JWTs signed by Privy and
    verified against the app's public key (``PRIVY_VERIFICATION_KEY``).  The
    ``aud`` claim must equal ``PRIVY_APP_ID`` and the ``iss`` claim must be
    ``privy.io``.

    Args:
        token: Raw Privy JWT string supplied by the frontend.

    Returns:
        Decoded payload dictionary.

    Raises:
        InvalidTokenError: If Privy auth is not configured, or the token is
                           malformed, expired, has an invalid signature, or
                           carries the wrong issuer/audience.
    """
    if not settings.PRIVY_APP_ID or not settings.PRIVY_VERIFICATION_KEY:
        raise InvalidTokenError("Privy authentication is not configured.")
    try:
        return jwt.decode(
            token,
            settings.PRIVY_VERIFICATION_KEY,
            algorithms=["ES256"],
            audience=settings.PRIVY_APP_ID,
            issuer=_PRIVY_ISSUER,
        )
    except JWTError as exc:
        raise InvalidTokenError("Privy token validation failed.") from exc


def _extract_email_from_identity_claims(claims: dict) -> Optional[str]:
    """Pull the verified email address out of a Privy identity token's claims.

    The ``linked_accounts`` claim is a stringified JSON array of the user's
    linked accounts.  An email login produces an entry shaped like
    ``{"type": "email", "address": "user@example.com", ...}``.

    Returns the normalised address of the first email account, or ``None`` if
    the claim is absent, unparseable, or contains no email account.
    """
    raw = claims.get("linked_accounts")
    if not raw:
        return None
    try:
        accounts = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(accounts, list):
        return None
    for account in accounts:
        if (
            isinstance(account, dict)
            and account.get("type") == "email"
            and account.get("address")
        ):
            return _normalise_email(str(account["address"]))
    return None


# ---------------------------------------------------------------------------
# Fixed-window rate limiting
# ---------------------------------------------------------------------------


async def check_rate_limit(
    redis: Redis,
    bucket: str,
    limit: int,
    window_seconds: int,
) -> None:
    """Enforce a simple fixed-window rate limit backed by Redis.

    Increments a counter for *bucket* and raises once it exceeds *limit*
    within *window_seconds*.  Mirrors the OTP-attempt counter pattern already
    used in ``verify_otp``.

    Args:
        redis:          Async Redis client.
        bucket:         Caller-supplied bucket key (e.g. ``"privy_verify:1.2.3.4"``).
        limit:          Maximum number of calls permitted per window.
        window_seconds: Window length in seconds.

    Raises:
        RateLimitedError: If the caller has exceeded *limit* in the window.
    """
    key = _rate_limit_key(bucket)
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    if count > limit:
        logger.warning(
            "Rate limit exceeded for bucket %s (%d/%d)", bucket, count, limit
        )
        raise RateLimitedError("Too many requests. Please try again shortly.")


# ---------------------------------------------------------------------------
# Anonymous session flow
# ---------------------------------------------------------------------------


async def issue_anonymous_token() -> str:
    """Issue a short-lived anonymous session JWT.

    No request body is required and no PII is collected.  The ``sub`` claim
    is a cryptographically random UUID that is not persisted to any database.

    Returns:
        Signed JWT string with ``role=anonymous_reporter``.
    """
    sub = str(uuid.uuid4())
    token = _build_access_token(sub=sub, role=Role.anonymous_reporter, tier=0)
    logger.info("Anonymous session token issued (sub suppressed for privacy).")
    return token


# ---------------------------------------------------------------------------
# Phone OTP flow
# ---------------------------------------------------------------------------


async def send_otp(phone_number: str, redis: Redis) -> None:
    """Hash the phone number, generate and store an OTP, dispatch via SMS.

    The plaintext ``phone_number`` is used only to call the SMS gateway and
    is not stored, logged, or passed to any other function.

    Args:
        phone_number: E.164-formatted phone number (e.g. ``+254700123456``).
        redis:        Async Redis client.

    Raises:
        SMSDeliveryError: If the SMS gateway fails to dispatch the message.
    """
    id_hash = hash_identifier(phone_number)

    # Test credentials — bypass the real gateway entirely for a fixed set of
    # phone numbers outside production. See TEST_PHONE_OTPS above.
    if settings.ENVIRONMENT != "production" and phone_number in TEST_PHONE_OTPS:
        otp_code = TEST_PHONE_OTPS[phone_number]
        await redis.set(_otp_key(id_hash), otp_code, ex=_OTP_TTL_SECONDS)
        logger.info(
            "Test OTP issued for a configured test phone number "
            "(identifier: %s…) — non-production only.",
            id_hash[:8],
        )
        return

    otp_code = _generate_otp()

    # Store the OTP in Redis with a 5-minute TTL.
    await redis.set(_otp_key(id_hash), otp_code, ex=_OTP_TTL_SECONDS)

    # Dispatch via the configured gateway.  The plaintext number is passed
    # here and immediately goes out of scope; it is never stored.
    gateway = get_sms_gateway()
    try:
        gateway.send_otp(phone_number, otp_code)
    except SMSDeliveryError:
        # Clean up the stored OTP so the user can retry without waiting for TTL.
        await redis.delete(_otp_key(id_hash))
        raise

    logger.info("OTP dispatched (identifier: %s…)", id_hash[:8])


async def verify_otp(
    phone_number: str,
    otp_code: str,
    redis: Redis,
    db: Optional["AsyncSession"] = None,
    tier: int = 1,
) -> tuple[str, str, Role]:
    """Verify an OTP and issue access + refresh tokens on success.

    If *db* is provided the phone hash is looked up in ``analyst_accounts``.
    Provisioned analysts, responders, and admins receive a JWT with their
    stored elevated role; all other callers receive ``role=reporter``.

    Args:
        phone_number: Plaintext E.164 number (used only to derive the hash).
        otp_code:     Six-digit string provided by the reporter.
        redis:        Async Redis client.
        db:           Optional async DB session for analyst account lookup.
        tier:         Trust tier to embed in the JWT (default 1 for phone-
                      verified reporters; ignored for elevated roles).

    Returns:
        Tuple of (access_token, refresh_token, role).

    Raises:
        OTPLockedOutError:  If the identifier is currently locked out.
        OTPNotFoundError:   If no OTP exists for this identifier.
        OTPExpiredError:    If the stored OTP has expired (Redis TTL elapsed).
        InvalidOTPError:    If the OTP does not match the stored value.
    """
    id_hash = hash_identifier(phone_number)

    # ── Lockout check ────────────────────────────────────────────────────────
    attempts_raw = await redis.get(_otp_attempts_key(id_hash))
    # Normalise to str: production Redis uses decode_responses=True and returns
    # str, but test mocks may return bytes.
    if isinstance(attempts_raw, bytes):
        attempts_raw = attempts_raw.decode()
    attempts = int(attempts_raw) if attempts_raw else 0
    if attempts >= _OTP_MAX_ATTEMPTS:
        logger.warning("OTP lockout active (identifier: %s…)", id_hash[:8])
        lockout_minutes = _OTP_LOCKOUT_SECONDS // 60
        raise OTPLockedOutError(
            f"Too many failed attempts. Try again in {lockout_minutes} minutes."
        )

    # ── Retrieve stored OTP ──────────────────────────────────────────────────
    stored_otp: bytes | str | None = await redis.get(_otp_key(id_hash))
    if stored_otp is None:
        raise OTPNotFoundError("No pending OTP found. Please request a new code.")

    # ── Constant-time comparison ─────────────────────────────────────────────
    # Normalise to str: production Redis uses decode_responses=True and returns
    # str, but test mocks may return bytes.  Both sides of compare_digest must
    # be the same type.
    if isinstance(stored_otp, bytes):
        stored_otp = stored_otp.decode()

    if not secrets.compare_digest(stored_otp, otp_code):
        # Increment failure counter; set lockout TTL on first failure.
        new_attempts = await redis.incr(_otp_attempts_key(id_hash))
        if new_attempts == 1:
            await redis.expire(_otp_attempts_key(id_hash), _OTP_LOCKOUT_SECONDS)
        logger.warning(
            "OTP mismatch for identifier %s… (attempt %d/%d)",
            id_hash[:8],
            new_attempts,
            _OTP_MAX_ATTEMPTS,
        )
        raise InvalidOTPError("Invalid OTP code.")

    # ── Success — resolve role, clean up, issue tokens ───────────────────────
    await redis.delete(_otp_key(id_hash))
    await redis.delete(_otp_attempts_key(id_hash))

    role = Role.reporter
    resolved_tier = tier
    region_geojson: Optional[str] = None
    if db is not None:
        account = await lookup_analyst_account(id_hash, db)
        if account is not None:
            role = Role(account.role)
            resolved_tier = 0  # tier is a reporter concept; not used for analysts
            region_geojson = account.region_geojson

    access_token = _build_access_token(
        sub=id_hash, role=role, tier=resolved_tier, region_geojson=region_geojson
    )
    refresh_token = await _issue_refresh_token(
        sub=id_hash,
        role=role,
        tier=resolved_tier,
        redis=redis,
        region_geojson=region_geojson,
    )

    logger.info(
        "OTP verified; tokens issued role=%s (identifier: %s…)",
        role.value,
        id_hash[:8],
    )
    return access_token, refresh_token, role


# ---------------------------------------------------------------------------
# Privy email OTP flow
# ---------------------------------------------------------------------------


async def verify_privy_and_issue_tokens(
    privy_token: str,
    redis: Redis,
    identity_token: Optional[str] = None,
    db: Optional["AsyncSession"] = None,
) -> tuple[str, str, Role]:
    """Verify Privy tokens from an email OTP login and issue our token pair.

    The Privy **access token** proves the user completed Privy's email OTP
    login.  The optional Privy **identity token** carries the verified email
    address (Privy access tokens do not); when supplied and it matches a
    provisioned ``analyst_accounts`` row, the caller receives that stored
    elevated role.  Everyone else receives ``role=reporter``.

    The returned pair is identical in shape to ``verify_otp`` — downstream
    refresh rotation, logout, and the denylist are unchanged.

    Args:
        privy_token:    Raw Privy access token JWT.
        redis:          Async Redis client.
        identity_token: Optional raw Privy identity token JWT.
        db:             Optional async DB session for analyst account lookup.

    Returns:
        Tuple of (access_token, refresh_token, role).

    Raises:
        InvalidTokenError: If either Privy token fails verification, the access
                           token has no subject, or the two tokens disagree on
                           the subject.
    """
    claims = verify_privy_token(privy_token)
    privy_did = claims.get("sub")
    if not privy_did:
        raise InvalidTokenError("Privy token is missing the subject claim.")

    email: Optional[str] = None
    if identity_token:
        identity_claims = verify_privy_token(identity_token)
        if identity_claims.get("sub") != privy_did:
            raise InvalidTokenError(
                "Privy identity token does not match the access token."
            )
        email = _extract_email_from_identity_claims(identity_claims)

    # Default: a plain reporter, keyed by a hash of the stable Privy DID.
    role = Role.reporter
    resolved_tier = 1
    sub = hash_identifier(f"privy:{privy_did}")
    region_geojson: Optional[str] = None

    # Provisioned analysts/responders/admins are keyed by their email hash,
    # matching register_analyst_account and the CLI provisioning tool.
    if email and db is not None:
        email_hash = hash_identifier(email)
        account = await lookup_analyst_account(email_hash, db)
        if account is not None:
            role = Role(account.role)
            resolved_tier = 0
            sub = email_hash
            region_geojson = account.region_geojson

    access_token = _build_access_token(
        sub=sub, role=role, tier=resolved_tier, region_geojson=region_geojson
    )
    refresh_token = await _issue_refresh_token(
        sub=sub,
        role=role,
        tier=resolved_tier,
        redis=redis,
        region_geojson=region_geojson,
    )

    logger.info(
        "Privy login verified; tokens issued role=%s (identifier: %s…)",
        role.value,
        sub[:8],
    )
    return access_token, refresh_token, role


# ---------------------------------------------------------------------------
# Analyst account provisioning
# ---------------------------------------------------------------------------


async def lookup_analyst_account(
    email_hash: str,
    db: "AsyncSession",
) -> Optional["AnalystAccount"]:
    """Return the active AnalystAccount for *email_hash*, or None.

    Args:
        email_hash: SHA-256 hex digest of the salted, normalised email address.
        db:         Async SQLAlchemy session.

    Returns:
        ``AnalystAccount`` instance, or ``None`` if no active account exists.
    """
    import sqlalchemy as sa

    from app.models.analyst_account import AnalystAccount

    result = await db.execute(
        sa.select(AnalystAccount).where(
            AnalystAccount.email_hash == email_hash,
            AnalystAccount.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def register_analyst_account(
    email: str,
    role: Role,
    created_by_sub: str,
    db: "AsyncSession",
    region_geojson: Optional[str] = None,
) -> "AnalystAccount":
    """Provision an analyst/responder/admin account by email address.

    The plaintext email is normalised and hashed immediately and never stored.
    Raises ``ValueError`` if *role* is not an elevated role, or if the email
    is already registered and active.

    Args:
        email:          Email address of the account to provision.
        role:           Must be analyst, responder, or admin.
        created_by_sub: JWT ``sub`` claim of the provisioning admin.
        db:             Async SQLAlchemy session.
        region_geojson: GeoJSON string for responder geographic scope.

    Returns:
        The created ``AnalystAccount`` instance.

    Raises:
        ValueError: If role is not elevated, or email is already registered.
    """
    import sqlalchemy as sa

    from app.models.analyst_account import AnalystAccount

    if role not in (Role.analyst, Role.responder, Role.admin):
        raise ValueError(
            f"register_analyst_account requires an elevated role, got: {role}"
        )

    email_hash = hash_identifier(_normalise_email(email))

    existing = await db.execute(
        sa.select(AnalystAccount).where(
            AnalystAccount.email_hash == email_hash,
            AnalystAccount.is_active.is_(True),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise ValueError(
            "This email address is already registered as an analyst account."
        )

    account = AnalystAccount(
        id=uuid.uuid4(),
        email_hash=email_hash,
        role=role.value,
        region_geojson=region_geojson,
        created_by_sub=created_by_sub,
        is_active=True,
    )
    db.add(account)
    await db.flush()
    await db.commit()

    logger.info(
        "Analyst account registered role=%s (identifier: %s…)",
        role.value,
        email_hash[:8],
    )
    return account


async def deactivate_analyst_account(
    account_id: str,
    db: "AsyncSession",
) -> bool:
    """Deactivate an analyst account by its UUID.

    Returns ``True`` if the account was found and deactivated, ``False`` if
    no active account with that ID exists.
    """
    import sqlalchemy as sa

    from app.models.analyst_account import AnalystAccount

    result = await db.execute(
        sa.select(AnalystAccount).where(
            AnalystAccount.id == account_id,
            AnalystAccount.is_active.is_(True),
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        return False

    account.is_active = False
    await db.flush()
    await db.commit()
    logger.info("Analyst account deactivated (id: %s…)", str(account_id)[:8])
    return True


# ---------------------------------------------------------------------------
# Refresh token rotation
# ---------------------------------------------------------------------------

_REFRESH_TOKEN_BYTES = 32


async def _issue_refresh_token(
    sub: str,
    role: Role,
    tier: int,
    redis: Redis,
    region_geojson: str | None = None,
) -> str:
    """Generate and store a new opaque refresh token in Redis.

    The token is a 64-character hex string (32 random bytes).  It maps to a
    small JSON payload in Redis so that rotation can re-issue a valid access
    token without a database round-trip.

    Args:
        sub:   Subject (hashed identifier).
        role:  Role for the new access token.
        tier:  Reporter tier.
        redis: Async Redis client.
        region_geojson: Optional responder scope, persisted so it survives
            refresh-token rotation (the DB is not consulted on rotation).

    Returns:
        Opaque refresh token string.
    """
    import json

    token_id = secrets.token_hex(_REFRESH_TOKEN_BYTES)
    expire_seconds = settings.REFRESH_TOKEN_EXPIRE_DAYS * 86_400
    data: dict[str, object] = {"sub": sub, "role": role.value, "tier": tier}
    if region_geojson:
        data["region_geojson"] = region_geojson
    await redis.set(_refresh_key(token_id), json.dumps(data), ex=expire_seconds)
    return token_id


async def rotate_refresh_token(refresh_token: str, redis: Redis) -> tuple[str, str]:
    """Invalidate the supplied refresh token and return a new token pair.

    Implements single-use refresh-token rotation.  The read-and-delete is done
    in one atomic ``GETDEL`` (audit M-6): two concurrent refreshes of the same
    token can no longer both succeed — exactly one gets the value, the other
    gets ``None`` and is rejected.  Presenting a token that no longer exists
    (after logout, a previous rotation, or a replay attempt) raises
    ``InvalidTokenError`` and logs a security signal.

    Args:
        refresh_token: The opaque token issued during OTP / Privy verification
                       or a previous rotation.
        redis:         Async Redis client.

    Returns:
        Tuple of (new_access_token, new_refresh_token).

    Raises:
        InvalidTokenError: If the token does not exist in Redis.
    """
    import json

    key = _refresh_key(refresh_token)
    # Atomic read+delete — the old token is consumed exactly once.
    try:
        raw = await redis.getdel(key)
    except AttributeError:  # pragma: no cover - very old redis-py / test stub
        raw = await redis.get(key)
        if raw is not None:
            await redis.delete(key)
    if raw is None:
        logger.warning(
            "Refresh token rejected: not found (logout, prior rotation, or "
            "replay of an already-used token)."
        )
        raise InvalidTokenError("Refresh token is invalid or has expired.")

    payload = json.loads(raw)

    sub = payload["sub"]
    role = Role(payload["role"])
    tier = int(payload.get("tier", 0))
    region_geojson = payload.get("region_geojson")

    new_access = _build_access_token(
        sub=sub, role=role, tier=tier, region_geojson=region_geojson
    )
    new_refresh = await _issue_refresh_token(
        sub=sub, role=role, tier=tier, redis=redis, region_geojson=region_geojson
    )

    logger.info("Refresh token rotated (sub: %s…)", sub[:8])
    return new_access, new_refresh


# ---------------------------------------------------------------------------
# Logout / token denylist
# ---------------------------------------------------------------------------


async def logout(token: str, redis: Redis) -> None:
    """Add the current access token to the Redis denylist.

    The denylist entry TTL matches the token's remaining lifetime so that
    Redis memory is not consumed beyond the point the token would have expired
    anyway.

    Args:
        token: Raw JWT access token from the Authorization header.
        redis: Async Redis client.

    Raises:
        InvalidTokenError: If the token cannot be decoded.
    """
    payload = decode_access_token(token)
    jti = payload.get("jti")
    exp = payload.get("exp")

    if not jti or not exp:
        raise InvalidTokenError("Token is missing required claims.")

    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    remaining_ttl = max(int(exp) - now_ts, 1)

    await redis.set(_denylist_key(jti), "1", ex=remaining_ttl)
    logger.info("Token added to denylist (jti: %s…)", jti[:8])


# ---------------------------------------------------------------------------
# Token verification (with denylist check)
# ---------------------------------------------------------------------------


async def verify_access_token(token: str, redis: Redis) -> dict:
    """Decode and validate an access token, checking the denylist.

    This is the function called by the ``get_current_user`` dependency on
    every authenticated request.

    Args:
        token: Raw JWT string.
        redis: Async Redis client.

    Returns:
        Decoded JWT payload dictionary.

    Raises:
        InvalidTokenError: If the token is invalid, expired, or revoked.
    """
    payload = decode_access_token(token)
    jti = payload.get("jti")

    if jti and await redis.exists(_denylist_key(jti)):
        raise InvalidTokenError("Token has been revoked.")

    return payload


# ---------------------------------------------------------------------------
# Analyst / admin token issuance (used by the CLI management command)
# ---------------------------------------------------------------------------


async def issue_analyst_token(
    email: str,
    role: Role,
    redis: Redis,
    region_geojson: str | None = None,
) -> tuple[str, str]:
    """Issue an access + refresh token pair for a provisioned analyst account.

    The ``email`` is hashed before use so that no plaintext address appears
    in JWT payloads or Redis keys.

    Args:
        email: Analyst's email address (used only for hashing; never stored).
        role:  Must be ``analyst``, ``responder``, or ``admin``.
        redis: Async Redis client.

    Returns:
        Tuple of (access_token, refresh_token).

    Raises:
        ValueError: If ``role`` is not an elevated role.
    """
    if role not in (Role.analyst, Role.responder, Role.admin):
        raise ValueError(f"issue_analyst_token requires an elevated role, got: {role}")

    # One hash function, one place — the same digest verify_privy_and_issue_tokens
    # and register_analyst_account use, so a CLI-provisioned analyst resolves to
    # the same ``sub`` when they later log in via Privy.
    id_hash = hash_identifier(_normalise_email(email))

    access_token = _build_access_token(
        sub=id_hash, role=role, tier=0, region_geojson=region_geojson
    )
    refresh_token = await _issue_refresh_token(
        sub=id_hash, role=role, tier=0, redis=redis, region_geojson=region_geojson
    )

    logger.info(
        "Analyst/admin token issued for role '%s' (identifier: %s…)",
        role.value,
        id_hash[:8],
    )
    return access_token, refresh_token
