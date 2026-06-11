"""Tests for Supabase analyst authentication — service and routes.

Coverage
--------
supabase_service:
  sign_in              — success, not configured, invalid credentials, server error
  invite_analyst       — success, not configured, supabase error
  extract_crisismap_claims — success, not configured, invalid token, missing role,
                             region_geojson propagated

analyst_auth routes:
  POST /auth/analyst/login  — success, invalid creds, not configured, no role,
                               unknown role, supabase unavailable
  POST /auth/analyst/invite — admin success, non-admin 403, invalid role 422,
                               not configured 503, supabase error 503

All tests are pure unit tests — no real HTTP, Redis, or Supabase connection.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from jose import jwt

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_EMAIL = "analyst.c@matata.org"
_PASSWORD = "securepassword123"
_TEST_JWT_SECRET = "test-supabase-jwt-secret-for-testing-only"

# A minimal Supabase-like JWT payload with crisismap_role set
_SUPABASE_PAYLOAD = {
    "sub": "uuid-1234",
    "email": _EMAIL,
    "role": "authenticated",
    "user_metadata": {
        "crisismap_role": "analyst",
        "region_geojson": None,
    },
}

_SUPABASE_RESPONSE = {
    "access_token": jwt.encode(_SUPABASE_PAYLOAD, _TEST_JWT_SECRET, algorithm="HS256"),
    "refresh_token": "supabase-refresh-token",
    "user": {"id": "uuid-1234", "email": _EMAIL},
}


# ===========================================================================
# supabase_service tests
# ===========================================================================


class TestSignIn:
    @pytest.mark.asyncio
    async def test_returns_response_on_success(self):
        from app.services.supabase_service import sign_in

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_success = True
        mock_response.json.return_value = _SUPABASE_RESPONSE

        with (
            patch("app.services.supabase_service.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_ANON_KEY = "anon-key"
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(return_value=mock_response))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await sign_in(_EMAIL, _PASSWORD)

        assert result["access_token"] == _SUPABASE_RESPONSE["access_token"]

    @pytest.mark.asyncio
    async def test_raises_not_configured_when_url_empty(self):
        from app.services.supabase_service import (
            SupabaseNotConfiguredError,
            sign_in,
        )

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_URL = ""
            mock_settings.SUPABASE_ANON_KEY = ""
            with pytest.raises(SupabaseNotConfiguredError):
                await sign_in(_EMAIL, _PASSWORD)

    @pytest.mark.asyncio
    async def test_raises_invalid_credentials_on_400(self):
        from app.services.supabase_service import (
            SupabaseInvalidCredentialsError,
            sign_in,
        )

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.is_success = False

        with (
            patch("app.services.supabase_service.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_ANON_KEY = "anon-key"
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(return_value=mock_response))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(SupabaseInvalidCredentialsError):
                await sign_in(_EMAIL, "wrongpassword")

    @pytest.mark.asyncio
    async def test_raises_supabase_error_on_server_error(self):
        from app.services.supabase_service import SupabaseAuthError, sign_in

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.is_success = False

        with (
            patch("app.services.supabase_service.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_ANON_KEY = "anon-key"
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(return_value=mock_response))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(SupabaseAuthError):
                await sign_in(_EMAIL, _PASSWORD)


class TestInviteAnalyst:
    @pytest.mark.asyncio
    async def test_sends_invite_successfully(self):
        from app.services.supabase_service import invite_analyst

        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"id": "uuid-1234", "email": _EMAIL}

        with (
            patch("app.services.supabase_service.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_SERVICE_ROLE_KEY = "service-key"
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(return_value=mock_response))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await invite_analyst(_EMAIL, "analyst")

        assert result["email"] == _EMAIL

    @pytest.mark.asyncio
    async def test_raises_not_configured_when_service_key_empty(self):
        from app.services.supabase_service import (
            SupabaseNotConfiguredError,
            invite_analyst,
        )

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_URL = ""
            mock_settings.SUPABASE_SERVICE_ROLE_KEY = ""
            with pytest.raises(SupabaseNotConfiguredError):
                await invite_analyst(_EMAIL, "analyst")

    @pytest.mark.asyncio
    async def test_raises_supabase_error_on_failure(self):
        from app.services.supabase_service import SupabaseAuthError, invite_analyst

        mock_response = MagicMock()
        mock_response.is_success = False
        mock_response.json.return_value = {"msg": "Email already registered"}

        with (
            patch("app.services.supabase_service.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_SERVICE_ROLE_KEY = "service-key"
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(return_value=mock_response))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(SupabaseAuthError):
                await invite_analyst(_EMAIL, "analyst")

    @pytest.mark.asyncio
    async def test_region_geojson_included_in_payload(self):
        from app.services.supabase_service import invite_analyst

        region = '{"type":"Polygon","coordinates":[]}'
        mock_response = MagicMock()
        mock_response.is_success = True
        mock_response.json.return_value = {"id": "uuid-1234"}

        captured = {}

        async def fake_post(url, json=None, headers=None):
            captured["payload"] = json
            return mock_response

        with (
            patch("app.services.supabase_service.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_client,
        ):
            mock_settings.SUPABASE_URL = "https://test.supabase.co"
            mock_settings.SUPABASE_SERVICE_ROLE_KEY = "service-key"
            mock_client.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(post=AsyncMock(side_effect=fake_post))
            )
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await invite_analyst(_EMAIL, "responder", region_geojson=region)

        assert captured["payload"]["data"]["region_geojson"] == region


class TestExtractCrisisMapClaims:
    def _make_token(self, payload: dict, secret: str = _TEST_JWT_SECRET) -> str:
        return jwt.encode(payload, secret, algorithm="HS256")

    def test_extracts_role_from_valid_token(self):
        from app.services.supabase_service import extract_crisismap_claims

        token = self._make_token(_SUPABASE_PAYLOAD)
        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_JWT_SECRET = _TEST_JWT_SECRET
            role, region = extract_crisismap_claims(token)

        assert role == "analyst"
        assert region is None

    def test_extracts_region_geojson_when_present(self):
        from app.services.supabase_service import extract_crisismap_claims

        payload = {
            **_SUPABASE_PAYLOAD,
            "user_metadata": {
                "crisismap_role": "responder",
                "region_geojson": '{"type":"Polygon"}',
            },
        }
        token = self._make_token(payload)

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_JWT_SECRET = _TEST_JWT_SECRET
            role, region = extract_crisismap_claims(token)

        assert role == "responder"
        assert region == '{"type":"Polygon"}'

    def test_raises_not_configured_when_jwt_secret_empty(self):
        from app.services.supabase_service import (
            SupabaseNotConfiguredError,
            extract_crisismap_claims,
        )

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_JWT_SECRET = ""
            with pytest.raises(SupabaseNotConfiguredError):
                extract_crisismap_claims("any.token.here")

    def test_raises_on_invalid_token(self):
        from app.services.supabase_service import (
            SupabaseAuthError,
            extract_crisismap_claims,
        )

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_JWT_SECRET = _TEST_JWT_SECRET
            with pytest.raises(SupabaseAuthError):
                extract_crisismap_claims("not.a.valid.token")

    def test_raises_when_no_role_in_metadata(self):
        from app.services.supabase_service import (
            SupabaseAuthError,
            extract_crisismap_claims,
        )

        payload = {
            "sub": "uuid-1234",
            "role": "authenticated",
            "user_metadata": {},
        }
        token = self._make_token(payload)

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_JWT_SECRET = _TEST_JWT_SECRET
            with pytest.raises(SupabaseAuthError, match="no CrisisMap role"):
                extract_crisismap_claims(token)

    def test_raises_on_wrong_secret(self):
        from app.services.supabase_service import (
            SupabaseAuthError,
            extract_crisismap_claims,
        )

        token = self._make_token(_SUPABASE_PAYLOAD, secret="correct-secret")

        with patch("app.services.supabase_service.settings") as mock_settings:
            mock_settings.SUPABASE_JWT_SECRET = "wrong-secret"
            with pytest.raises(SupabaseAuthError):
                extract_crisismap_claims(token)


# ===========================================================================
# Analyst auth route tests
# ===========================================================================


def _make_analyst_auth_app():
    """Build a minimal FastAPI app with the analyst_auth router mounted."""
    from fastapi import FastAPI

    from app.api.v1.routes.analyst_auth import router
    from app.core.dependencies import get_redis

    app = FastAPI()

    async def override_redis():
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        redis.delete = AsyncMock(return_value=1)
        redis.exists = AsyncMock(return_value=0)
        return redis

    app.dependency_overrides[get_redis] = override_redis
    app.include_router(router, prefix="/api/v1")
    return app


def _admin_bearer() -> str:
    """Return a valid CrisisMap JWT with admin role for protected endpoints."""
    from app.services.auth_service import Role, _build_access_token

    return _build_access_token(sub="admin-hash", role=Role.admin)


def _analyst_bearer() -> str:
    """Return a valid CrisisMap JWT with analyst role."""
    from app.services.auth_service import Role, _build_access_token

    return _build_access_token(sub="analyst-hash", role=Role.analyst)


class TestAnalystLoginRoute:
    def test_successful_login_returns_crisismap_tokens(self):
        # Mount the auth router too so require_role dependency resolves
        from fastapi import FastAPI

        from app.api.v1.routes.analyst_auth import router
        from app.api.v1.routes.auth import router as auth_router
        from app.core.dependencies import get_redis
        from app.services.auth_service import Role, _build_access_token

        app = FastAPI()

        async def override_redis():
            redis = AsyncMock()
            redis.set = AsyncMock(return_value=True)
            redis.exists = AsyncMock(return_value=0)
            return redis

        app.dependency_overrides[get_redis] = override_redis
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(router, prefix="/api/v1")
        client = TestClient(app)

        fake_supabase_token = jwt.encode(
            _SUPABASE_PAYLOAD, _TEST_JWT_SECRET, algorithm="HS256"
        )
        fake_crisismap_token = _build_access_token(sub="hash", role=Role.analyst)

        with (
            patch(
                "app.api.v1.routes.analyst_auth.sign_in",
                new=AsyncMock(return_value={"access_token": fake_supabase_token}),
            ),
            patch(
                "app.api.v1.routes.analyst_auth.extract_crisismap_claims",
                return_value=("analyst", None),
            ),
            patch(
                "app.api.v1.routes.analyst_auth.auth_service.issue_analyst_token",
                new=AsyncMock(return_value=(fake_crisismap_token, "refresh-tok")),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/login",
                json={"email": _EMAIL, "password": _PASSWORD},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert "refresh_token" in data
        assert data["role"] == "analyst"

    def test_invalid_credentials_returns_401(self):
        from app.services.supabase_service import SupabaseInvalidCredentialsError

        app = _make_analyst_auth_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.sign_in",
            new=AsyncMock(side_effect=SupabaseInvalidCredentialsError("bad creds")),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/login",
                json={"email": _EMAIL, "password": "wrongpass"},
            )

        assert resp.status_code == 401

    def test_supabase_not_configured_returns_503(self):
        from app.services.supabase_service import SupabaseNotConfiguredError

        app = _make_analyst_auth_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.sign_in",
            new=AsyncMock(side_effect=SupabaseNotConfiguredError("not configured")),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/login",
                json={"email": _EMAIL, "password": _PASSWORD},
            )

        assert resp.status_code == 503

    def test_no_role_in_metadata_returns_403(self):
        from app.services.supabase_service import SupabaseAuthError

        app = _make_analyst_auth_app()
        client = TestClient(app)

        with (
            patch(
                "app.api.v1.routes.analyst_auth.sign_in",
                new=AsyncMock(return_value={"access_token": "tok"}),
            ),
            patch(
                "app.api.v1.routes.analyst_auth.extract_crisismap_claims",
                side_effect=SupabaseAuthError("no CrisisMap role"),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/login",
                json={"email": _EMAIL, "password": _PASSWORD},
            )

        assert resp.status_code == 403

    def test_unknown_role_string_returns_403(self):
        app = _make_analyst_auth_app()
        client = TestClient(app)

        with (
            patch(
                "app.api.v1.routes.analyst_auth.sign_in",
                new=AsyncMock(return_value={"access_token": "tok"}),
            ),
            patch(
                "app.api.v1.routes.analyst_auth.extract_crisismap_claims",
                return_value=("superuser", None),
            ),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/login",
                json={"email": _EMAIL, "password": _PASSWORD},
            )

        assert resp.status_code == 403

    def test_supabase_service_unavailable_returns_503(self):
        from app.services.supabase_service import SupabaseAuthError

        app = _make_analyst_auth_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.sign_in",
            new=AsyncMock(side_effect=SupabaseAuthError("timeout")),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/login",
                json={"email": _EMAIL, "password": _PASSWORD},
            )

        assert resp.status_code == 503

    def test_short_password_returns_422(self):
        app = _make_analyst_auth_app()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/login",
            json={"email": _EMAIL, "password": "short"},
        )

        assert resp.status_code == 422


class TestAnalystInviteRoute:
    def _make_app_with_auth(self):
        """App with both auth and analyst_auth routers for role enforcement."""
        from fastapi import FastAPI

        from app.api.v1.routes.analyst_auth import router
        from app.api.v1.routes.auth import router as auth_router
        from app.core.dependencies import get_redis

        app = FastAPI()

        async def override_redis():
            redis = AsyncMock()
            redis.exists = AsyncMock(return_value=0)
            redis.set = AsyncMock(return_value=True)
            return redis

        app.dependency_overrides[get_redis] = override_redis
        app.include_router(auth_router, prefix="/api/v1")
        app.include_router(router, prefix="/api/v1")
        return app

    def test_admin_can_invite_analyst(self):
        app = self._make_app_with_auth()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.invite_analyst",
            new=AsyncMock(return_value={"id": "uuid-1234"}),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/invite",
                json={"email": _EMAIL, "role": "analyst"},
                headers={"Authorization": f"Bearer {_admin_bearer()}"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == _EMAIL
        assert data["role"] == "analyst"
        assert "Invite email sent" in data["message"]

    def test_non_admin_returns_403(self):
        app = self._make_app_with_auth()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/invite",
            json={"email": _EMAIL, "role": "analyst"},
            headers={"Authorization": f"Bearer {_analyst_bearer()}"},
        )

        assert resp.status_code == 403

    def test_no_token_returns_401(self):
        app = self._make_app_with_auth()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/invite",
            json={"email": _EMAIL, "role": "analyst"},
        )

        assert resp.status_code == 401

    def test_invalid_role_returns_422(self):
        app = self._make_app_with_auth()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/invite",
            json={"email": _EMAIL, "role": "superuser"},
            headers={"Authorization": f"Bearer {_admin_bearer()}"},
        )

        assert resp.status_code == 422

    def test_supabase_not_configured_returns_503(self):
        from app.services.supabase_service import SupabaseNotConfiguredError

        app = self._make_app_with_auth()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.invite_analyst",
            new=AsyncMock(side_effect=SupabaseNotConfiguredError("not configured")),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/invite",
                json={"email": _EMAIL, "role": "analyst"},
                headers={"Authorization": f"Bearer {_admin_bearer()}"},
            )

        assert resp.status_code == 503

    def test_supabase_error_returns_503(self):
        from app.services.supabase_service import SupabaseAuthError

        app = self._make_app_with_auth()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.invite_analyst",
            new=AsyncMock(side_effect=SupabaseAuthError("email already registered")),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/invite",
                json={"email": _EMAIL, "role": "analyst"},
                headers={"Authorization": f"Bearer {_admin_bearer()}"},
            )

        assert resp.status_code == 503

    def test_responder_invite_includes_region(self):
        app = self._make_app_with_auth()
        client = TestClient(app)
        region = '{"type":"Polygon","coordinates":[]}'

        with patch(
            "app.api.v1.routes.analyst_auth.invite_analyst",
            new=AsyncMock(return_value={"id": "uuid-5678"}),
        ) as mock_invite:
            resp = client.post(
                "/api/v1/auth/analyst/invite",
                json={
                    "email": _EMAIL,
                    "role": "responder",
                    "region_geojson": region,
                },
                headers={"Authorization": f"Bearer {_admin_bearer()}"},
            )

        assert resp.status_code == 201
        mock_invite.assert_awaited_once_with(
            email=_EMAIL,
            crisismap_role="responder",
            region_geojson=region,
        )
