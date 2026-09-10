"""Tests for the Privy email OTP login path.

Covers:
* auth_service.verify_privy_token — signature, issuer, audience, expiry checks
* auth_service._extract_email_from_identity_claims — linked_accounts parsing
* auth_service.verify_privy_and_issue_tokens — reporter vs provisioned analyst
* auth_service.check_rate_limit — fixed-window counter
* POST /api/v1/auth/privy/verify — happy path, 401 on bad token, 429 when the
  per-IP rate limit is exceeded

Privy tokens are ES256 JWTs.  A throwaway ES256 keypair is generated per test
session; the public key is patched onto ``settings.PRIVY_VERIFICATION_KEY`` and
tokens are signed with the matching private key, so nothing here touches Privy's
real infrastructure.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jose import jwt

_APP_ID = "test-privy-app-id"
_PRIVY_ISS = "privy.io"
_DID = "did:privy:cltest0000000000000000000"
_EMAIL = "analyst@example.org"


# ---------------------------------------------------------------------------
# Key material + token builders
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _es256_keys() -> tuple[str, str]:
    """Return (private_pem, public_pem) for a fresh ES256 (P-256) keypair."""
    key = ec.generate_private_key(ec.SECP256R1())
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def privy_settings(monkeypatch, _es256_keys):
    """Point auth_service at the test keypair + app id."""
    from app.core.config import settings

    _, public_pem = _es256_keys
    monkeypatch.setattr(settings, "PRIVY_APP_ID", _APP_ID, raising=False)
    monkeypatch.setattr(settings, "PRIVY_VERIFICATION_KEY", public_pem, raising=False)
    return settings


def _sign(private_pem: str, **claims) -> str:
    payload = {
        "sid": "session-" + uuid.uuid4().hex,
        "iss": _PRIVY_ISS,
        "aud": _APP_ID,
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
        "sub": _DID,
    }
    payload.update(claims)
    return jwt.encode(payload, private_pem, algorithm="ES256")


def _access_token(private_pem: str, **claims) -> str:
    return _sign(private_pem, **claims)


def _identity_token(private_pem: str, email: str | None = _EMAIL, **claims) -> str:
    linked = []
    if email is not None:
        linked.append({"type": "email", "address": email})
    return _sign(private_pem, linked_accounts=json.dumps(linked), **claims)


# ---------------------------------------------------------------------------
# Minimal in-memory Redis
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        self._store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
        return 1

    async def exists(self, key):
        return 1 if key in self._store else 0

    async def incr(self, key):
        new = int(self._store.get(key, 0)) + 1
        self._store[key] = str(new)
        return new

    async def expire(self, key, seconds):
        return True


# ---------------------------------------------------------------------------
# verify_privy_token
# ---------------------------------------------------------------------------


class TestVerifyPrivyToken:
    def test_valid_token_returns_claims(self, privy_settings, _es256_keys):
        from app.services.auth_service import verify_privy_token

        priv, _ = _es256_keys
        claims = verify_privy_token(_access_token(priv))
        assert claims["sub"] == _DID
        assert claims["aud"] == _APP_ID

    def test_unconfigured_raises(self, monkeypatch, _es256_keys):
        from app.core.config import settings
        from app.services.auth_service import InvalidTokenError, verify_privy_token

        monkeypatch.setattr(settings, "PRIVY_APP_ID", "", raising=False)
        monkeypatch.setattr(settings, "PRIVY_VERIFICATION_KEY", "", raising=False)
        priv, _ = _es256_keys
        with pytest.raises(InvalidTokenError):
            verify_privy_token(_access_token(priv))

    def test_expired_token_raises(self, privy_settings, _es256_keys):
        from app.services.auth_service import InvalidTokenError, verify_privy_token

        priv, _ = _es256_keys
        token = _access_token(priv, exp=int(time.time()) - 10)
        with pytest.raises(InvalidTokenError):
            verify_privy_token(token)

    def test_tampered_signature_raises(self, privy_settings, _es256_keys):
        from app.services.auth_service import InvalidTokenError, verify_privy_token

        priv, _ = _es256_keys
        token = _access_token(priv)
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        with pytest.raises(InvalidTokenError):
            verify_privy_token(tampered)

    def test_wrong_audience_raises(self, privy_settings, _es256_keys):
        from app.services.auth_service import InvalidTokenError, verify_privy_token

        priv, _ = _es256_keys
        with pytest.raises(InvalidTokenError):
            verify_privy_token(_access_token(priv, aud="some-other-app"))

    def test_wrong_issuer_raises(self, privy_settings, _es256_keys):
        from app.services.auth_service import InvalidTokenError, verify_privy_token

        priv, _ = _es256_keys
        with pytest.raises(InvalidTokenError):
            verify_privy_token(_access_token(priv, iss="evil.example"))

    def test_foreign_key_raises(self, privy_settings):
        from app.services.auth_service import InvalidTokenError, verify_privy_token

        other = (
            ec.generate_private_key(ec.SECP256R1())
            .private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
            .decode()
        )
        with pytest.raises(InvalidTokenError):
            verify_privy_token(_access_token(other))


# ---------------------------------------------------------------------------
# _extract_email_from_identity_claims
# ---------------------------------------------------------------------------


class TestExtractEmail:
    def test_extracts_and_normalises_email(self):
        from app.services.auth_service import _extract_email_from_identity_claims

        claims = {
            "linked_accounts": json.dumps(
                [
                    {"type": "wallet", "address": "0xabc"},
                    {"type": "email", "address": "  Analyst@Example.ORG "},
                ]
            )
        }
        assert _extract_email_from_identity_claims(claims) == "analyst@example.org"

    def test_missing_claim_returns_none(self):
        from app.services.auth_service import _extract_email_from_identity_claims

        assert _extract_email_from_identity_claims({}) is None

    def test_unparseable_claim_returns_none(self):
        from app.services.auth_service import _extract_email_from_identity_claims

        assert _extract_email_from_identity_claims({"linked_accounts": "{bad"}) is None

    def test_no_email_account_returns_none(self):
        from app.services.auth_service import _extract_email_from_identity_claims

        claims = {"linked_accounts": json.dumps([{"type": "wallet", "address": "0x"}])}
        assert _extract_email_from_identity_claims(claims) is None


# ---------------------------------------------------------------------------
# verify_privy_and_issue_tokens
# ---------------------------------------------------------------------------


def _db_returning(account):
    """AsyncMock DB whose single execute() resolves to *account*."""
    from unittest.mock import AsyncMock, MagicMock

    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=account))
    )
    return db


class TestVerifyPrivyAndIssueTokens:
    @pytest.mark.asyncio
    async def test_access_only_is_reporter(self, privy_settings, _es256_keys):
        from app.services.auth_service import Role, verify_privy_and_issue_tokens

        priv, _ = _es256_keys
        access, refresh, role = await verify_privy_and_issue_tokens(
            privy_token=_access_token(priv), redis=_FakeRedis()
        )
        assert role == Role.reporter
        assert access and refresh

    @pytest.mark.asyncio
    async def test_provisioned_email_gets_stored_role(
        self, privy_settings, _es256_keys
    ):
        from unittest.mock import MagicMock

        from app.services.auth_service import Role, verify_privy_and_issue_tokens

        priv, _ = _es256_keys
        account = MagicMock(role="analyst", is_active=True, region_geojson=None)
        _, _, role = await verify_privy_and_issue_tokens(
            privy_token=_access_token(priv),
            identity_token=_identity_token(priv, email=_EMAIL),
            redis=_FakeRedis(),
            db=_db_returning(account),
        )
        assert role == Role.analyst

    @pytest.mark.asyncio
    async def test_provisioned_responder_region_lands_in_jwt(
        self, privy_settings, _es256_keys
    ):
        """A provisioned responder's region_geojson is carried as a JWT claim.

        Without this the analyst routes' ST_Within filter never fires and a
        regional responder can read every report in the system (audit H-1).
        """
        from unittest.mock import MagicMock

        from app.services.auth_service import (
            Role,
            decode_access_token,
            rotate_refresh_token,
            verify_privy_and_issue_tokens,
        )

        region = (
            '{"type":"Polygon","coordinates":'
            "[[[36.79,-1.33],[36.87,-1.33],[36.87,-1.25],[36.79,-1.25],"
            "[36.79,-1.33]]]}"
        )
        priv, _ = _es256_keys
        account = MagicMock(role="responder", is_active=True, region_geojson=region)
        redis = _FakeRedis()
        access, refresh, role = await verify_privy_and_issue_tokens(
            privy_token=_access_token(priv),
            identity_token=_identity_token(priv, email=_EMAIL),
            redis=redis,
            db=_db_returning(account),
        )
        assert role == Role.responder
        assert decode_access_token(access)["region_geojson"] == region

        # ...and it survives refresh-token rotation (DB is not re-consulted).
        new_access, _ = await rotate_refresh_token(refresh, redis)
        assert decode_access_token(new_access)["region_geojson"] == region

    @pytest.mark.asyncio
    async def test_reporter_token_has_no_region_claim(
        self, privy_settings, _es256_keys
    ):
        from app.services.auth_service import (
            decode_access_token,
            verify_privy_and_issue_tokens,
        )

        priv, _ = _es256_keys
        access, _, _ = await verify_privy_and_issue_tokens(
            privy_token=_access_token(priv), redis=_FakeRedis()
        )
        assert "region_geojson" not in decode_access_token(access)

    @pytest.mark.asyncio
    async def test_unprovisioned_email_is_reporter(self, privy_settings, _es256_keys):
        from app.services.auth_service import Role, verify_privy_and_issue_tokens

        priv, _ = _es256_keys
        _, _, role = await verify_privy_and_issue_tokens(
            privy_token=_access_token(priv),
            identity_token=_identity_token(priv, email="nobody@example.org"),
            redis=_FakeRedis(),
            db=_db_returning(None),
        )
        assert role == Role.reporter

    @pytest.mark.asyncio
    async def test_identity_token_subject_mismatch_raises(
        self, privy_settings, _es256_keys
    ):
        from app.services.auth_service import (
            InvalidTokenError,
            verify_privy_and_issue_tokens,
        )

        priv, _ = _es256_keys
        with pytest.raises(InvalidTokenError):
            await verify_privy_and_issue_tokens(
                privy_token=_access_token(priv, sub="did:privy:aaa"),
                identity_token=_identity_token(priv, sub="did:privy:bbb"),
                redis=_FakeRedis(),
                db=_db_returning(None),
            )

    @pytest.mark.asyncio
    async def test_access_token_without_subject_raises(
        self, privy_settings, _es256_keys
    ):
        from app.services.auth_service import (
            InvalidTokenError,
            verify_privy_and_issue_tokens,
        )

        priv, _ = _es256_keys
        with pytest.raises(InvalidTokenError):
            await verify_privy_and_issue_tokens(
                privy_token=_access_token(priv, sub=""), redis=_FakeRedis()
            )

    @pytest.mark.asyncio
    async def test_provisioned_sub_matches_cli_issue_analyst_token(
        self, privy_settings, _es256_keys
    ):
        """A Privy login and a CLI-issued token for the same email share ``sub``."""
        from unittest.mock import MagicMock

        from jose import jwt as _jwt

        from app.core.config import settings
        from app.services.auth_service import (
            Role,
            issue_analyst_token,
            verify_privy_and_issue_tokens,
        )

        priv, _ = _es256_keys
        account = MagicMock(role="analyst", is_active=True, region_geojson=None)
        privy_access, _, _ = await verify_privy_and_issue_tokens(
            privy_token=_access_token(priv),
            identity_token=_identity_token(priv, email=_EMAIL),
            redis=_FakeRedis(),
            db=_db_returning(account),
        )
        cli_access, _ = await issue_analyst_token(
            email=_EMAIL, role=Role.analyst, redis=_FakeRedis()
        )
        privy_sub = _jwt.decode(
            privy_access, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )["sub"]
        cli_sub = _jwt.decode(
            cli_access, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )["sub"]
        assert privy_sub == cli_sub


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    @pytest.mark.asyncio
    async def test_allows_up_to_limit_then_raises(self):
        from app.services.auth_service import RateLimitedError, check_rate_limit

        redis = _FakeRedis()
        for _ in range(3):
            await check_rate_limit(redis, "b", limit=3, window_seconds=60)
        with pytest.raises(RateLimitedError):
            await check_rate_limit(redis, "b", limit=3, window_seconds=60)


# ---------------------------------------------------------------------------
# POST /api/v1/auth/privy/verify
# ---------------------------------------------------------------------------


def _make_client(redis=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.routes.auth import router as auth_router
    from app.core.dependencies import get_db, get_redis

    shared_redis = redis or _FakeRedis()
    app = FastAPI()

    async def override_redis():
        return shared_redis

    async def override_db():
        yield _db_returning(None)

    app.dependency_overrides[get_redis] = override_redis
    app.dependency_overrides[get_db] = override_db
    app.include_router(auth_router, prefix="/api/v1")
    return TestClient(app)


class TestPrivyVerifyRoute:
    def test_happy_path_returns_token_shape(self, privy_settings, _es256_keys):
        priv, _ = _es256_keys
        client = _make_client()
        resp = client.post(
            "/api/v1/auth/privy/verify",
            json={
                "privy_token": _access_token(priv),
                "identity_token": _identity_token(priv, email="someone@example.org"),
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"token", "refresh_token", "role"}
        assert body["role"] == "reporter"

    def test_garbage_token_returns_401(self, privy_settings):
        client = _make_client()
        resp = client.post(
            "/api/v1/auth/privy/verify", json={"privy_token": "not-a-jwt"}
        )
        assert resp.status_code == 401

    def test_rate_limited_after_burst(self, privy_settings, _es256_keys):
        from app.core.rate_limits import RATE_PRIVY_VERIFY_LIMIT

        priv, _ = _es256_keys
        client = _make_client()
        payload = {"privy_token": _access_token(priv)}

        last = None
        for _ in range(RATE_PRIVY_VERIFY_LIMIT + 1):
            last = client.post("/api/v1/auth/privy/verify", json=payload)
        assert last.status_code == 429
        assert last.headers.get("Retry-After")
