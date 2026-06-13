"""Tests for OTP-based analyst account provisioning.

Coverage
--------
auth_service:
  lookup_analyst_account  — found, not found, inactive ignored
  register_analyst_account — success, invalid role, duplicate phone
  deactivate_analyst_account — success, not found

analyst_auth routes:
  POST   /auth/analyst/register  — admin success, non-admin 403, bad role 422,
                                   bad phone 422, duplicate 400
  DELETE /auth/analyst/accounts/{id} — admin success, not found 404, non-admin 403
  GET    /auth/analyst/accounts  — admin lists accounts

auth_service.verify_otp (analyst elevation):
  — reporter phone returns reporter role
  — analyst phone returns analyst role

All tests are pure unit tests — no real database, Redis, or network.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

_PHONE = "+254700000001"
_OTP = "123456"
_ACCOUNT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_token() -> str:
    from app.services.auth_service import Role, _build_access_token

    return _build_access_token(sub="admin-sub-hash", role=Role.admin)


def _analyst_token() -> str:
    from app.services.auth_service import Role, _build_access_token

    return _build_access_token(sub="analyst-sub-hash", role=Role.analyst)


def _make_account(
    role: str = "analyst",
    is_active: bool = True,
    region_geojson: str | None = None,
) -> MagicMock:
    acc = MagicMock()
    acc.id = _ACCOUNT_ID
    acc.phone_hash = "aabbcc"
    acc.role = role
    acc.region_geojson = region_geojson
    acc.is_active = is_active
    acc.created_by_sub = "admin-sub-hash"
    return acc


def _make_app():
    from fastapi import FastAPI

    from app.api.v1.routes.analyst_auth import router
    from app.api.v1.routes.auth import router as auth_router
    from app.core.dependencies import get_db, get_redis

    app = FastAPI()

    async def override_redis():
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        redis.delete = AsyncMock(return_value=1)
        redis.exists = AsyncMock(return_value=0)
        return redis

    async def override_db():
        yield AsyncMock()

    app.dependency_overrides[get_redis] = override_redis
    app.dependency_overrides[get_db] = override_db
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1")
    return app


# ===========================================================================
# auth_service — lookup_analyst_account
# ===========================================================================


class TestLookupAnalystAccount:
    @pytest.mark.asyncio
    async def test_returns_account_when_found(self):
        from app.services.auth_service import lookup_analyst_account

        account = _make_account()
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=account))
        )

        result = await lookup_analyst_account("aabbcc", db)
        assert result is account

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        from app.services.auth_service import lookup_analyst_account

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        result = await lookup_analyst_account("not-a-hash", db)
        assert result is None


# ===========================================================================
# auth_service — register_analyst_account
# ===========================================================================


class TestRegisterAnalystAccount:
    @pytest.mark.asyncio
    async def test_registers_analyst_successfully(self):
        from app.services.auth_service import Role, register_analyst_account

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        account = await register_analyst_account(
            phone_number=_PHONE,
            role=Role.analyst,
            created_by_sub="admin-hash",
            db=db,
        )

        db.add.assert_called_once()
        db.commit.assert_awaited_once()
        assert account.role == "analyst"

    @pytest.mark.asyncio
    async def test_raises_for_reporter_role(self):
        from app.services.auth_service import Role, register_analyst_account

        db = AsyncMock()
        with pytest.raises(ValueError, match="elevated role"):
            await register_analyst_account(
                phone_number=_PHONE,
                role=Role.reporter,
                created_by_sub="admin-hash",
                db=db,
            )

    @pytest.mark.asyncio
    async def test_raises_for_duplicate_phone(self):
        from app.services.auth_service import Role, register_analyst_account

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=_make_account())
            )
        )

        with pytest.raises(ValueError, match="already registered"):
            await register_analyst_account(
                phone_number=_PHONE,
                role=Role.analyst,
                created_by_sub="admin-hash",
                db=db,
            )


# ===========================================================================
# auth_service — deactivate_analyst_account
# ===========================================================================


class TestDeactivateAnalystAccount:
    @pytest.mark.asyncio
    async def test_deactivates_existing_account(self):
        from app.services.auth_service import deactivate_analyst_account

        account = _make_account()
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=account))
        )
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        result = await deactivate_analyst_account(str(_ACCOUNT_ID), db)

        assert result is True
        assert account.is_active is False

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self):
        from app.services.auth_service import deactivate_analyst_account

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        result = await deactivate_analyst_account(str(_ACCOUNT_ID), db)
        assert result is False


# ===========================================================================
# auth_service — verify_otp role elevation
# ===========================================================================


class TestVerifyOtpRoleElevation:
    def _make_redis(self, otp: str = _OTP) -> AsyncMock:
        redis = AsyncMock()
        redis.get = AsyncMock(
            side_effect=lambda key: (b"0" if "attempts" in key else otp.encode())
        )
        redis.delete = AsyncMock()
        redis.set = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_reporter_phone_gets_reporter_role(self):
        from app.services.auth_service import Role, verify_otp

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        _, _, role = await verify_otp(_PHONE, _OTP, self._make_redis(), db=db)
        assert role == Role.reporter

    @pytest.mark.asyncio
    async def test_analyst_phone_gets_analyst_role(self):
        from app.services.auth_service import Role, verify_otp

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=_make_account(role="analyst"))
            )
        )

        _, _, role = await verify_otp(_PHONE, _OTP, self._make_redis(), db=db)
        assert role == Role.analyst

    @pytest.mark.asyncio
    async def test_responder_phone_gets_responder_role(self):
        from app.services.auth_service import Role, verify_otp

        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(
                    return_value=_make_account(role="responder")
                )
            )
        )

        _, _, role = await verify_otp(_PHONE, _OTP, self._make_redis(), db=db)
        assert role == Role.responder

    @pytest.mark.asyncio
    async def test_no_db_always_returns_reporter_role(self):
        from app.services.auth_service import Role, verify_otp

        _, _, role = await verify_otp(_PHONE, _OTP, self._make_redis(), db=None)
        assert role == Role.reporter


# ===========================================================================
# Analyst register route
# ===========================================================================


class TestRegisterRoute:
    def test_admin_can_register_analyst(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.auth_service.register_analyst_account",
            new=AsyncMock(return_value=_make_account()),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/register",
                json={"phone": _PHONE, "role": "analyst"},
                headers={"Authorization": f"Bearer {_admin_token()}"},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert "provisioned" in data["message"].lower() or "Account" in data["message"]
        assert data["account"]["role"] == "analyst"

    def test_non_admin_returns_403(self):
        app = _make_app()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/register",
            json={"phone": _PHONE, "role": "analyst"},
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )

        assert resp.status_code == 403

    def test_no_token_returns_401(self):
        app = _make_app()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/register",
            json={"phone": _PHONE, "role": "analyst"},
        )

        assert resp.status_code == 401

    def test_invalid_role_returns_422(self):
        app = _make_app()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/register",
            json={"phone": _PHONE, "role": "superuser"},
            headers={"Authorization": f"Bearer {_admin_token()}"},
        )

        assert resp.status_code == 422

    def test_invalid_phone_returns_422(self):
        app = _make_app()
        client = TestClient(app)

        resp = client.post(
            "/api/v1/auth/analyst/register",
            json={"phone": "not-a-phone", "role": "analyst"},
            headers={"Authorization": f"Bearer {_admin_token()}"},
        )

        assert resp.status_code == 422

    def test_duplicate_phone_returns_400(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.auth_service.register_analyst_account",
            new=AsyncMock(side_effect=ValueError("already registered")),
        ):
            resp = client.post(
                "/api/v1/auth/analyst/register",
                json={"phone": _PHONE, "role": "analyst"},
                headers={"Authorization": f"Bearer {_admin_token()}"},
            )

        assert resp.status_code == 400

    def test_responder_with_region_geojson(self):
        app = _make_app()
        client = TestClient(app)
        region = '{"type":"Polygon","coordinates":[]}'

        with patch(
            "app.api.v1.routes.analyst_auth.auth_service.register_analyst_account",
            new=AsyncMock(
                return_value=_make_account(role="responder", region_geojson=region)
            ),
        ) as mock_reg:
            resp = client.post(
                "/api/v1/auth/analyst/register",
                json={"phone": _PHONE, "role": "responder", "region_geojson": region},
                headers={"Authorization": f"Bearer {_admin_token()}"},
            )

        assert resp.status_code == 201
        mock_reg.assert_awaited_once()
        _, kwargs = mock_reg.call_args
        assert kwargs["region_geojson"] == region


# ===========================================================================
# Deactivate route
# ===========================================================================


class TestDeactivateRoute:
    def test_admin_can_deactivate(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.auth_service.deactivate_analyst_account",
            new=AsyncMock(return_value=True),
        ):
            resp = client.delete(
                f"/api/v1/auth/analyst/accounts/{_ACCOUNT_ID}",
                headers={"Authorization": f"Bearer {_admin_token()}"},
            )

        assert resp.status_code == 200

    def test_not_found_returns_404(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.analyst_auth.auth_service.deactivate_analyst_account",
            new=AsyncMock(return_value=False),
        ):
            resp = client.delete(
                f"/api/v1/auth/analyst/accounts/{_ACCOUNT_ID}",
                headers={"Authorization": f"Bearer {_admin_token()}"},
            )

        assert resp.status_code == 404

    def test_non_admin_returns_403(self):
        app = _make_app()
        client = TestClient(app)

        resp = client.delete(
            f"/api/v1/auth/analyst/accounts/{_ACCOUNT_ID}",
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )

        assert resp.status_code == 403


# ===========================================================================
# List route
# ===========================================================================


class TestListAccountsRoute:
    def test_admin_can_list(self):
        app = _make_app()
        client = TestClient(app)

        accounts = [_make_account("analyst"), _make_account("responder")]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = accounts

        with patch(
            "app.api.v1.routes.analyst_auth.AsyncSession",
            new_callable=MagicMock,
        ):
            with patch(
                "app.api.v1.routes.analyst_auth.auth_service",
            ):
                # Directly override the DB to return our mock accounts
                from app.core.dependencies import get_db

                async def override_db_with_accounts():
                    db = AsyncMock()
                    db.execute = AsyncMock(return_value=mock_result)
                    yield db

                app.dependency_overrides[get_db] = override_db_with_accounts

                resp = client.get(
                    "/api/v1/auth/analyst/accounts",
                    headers={"Authorization": f"Bearer {_admin_token()}"},
                )

        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_non_admin_returns_403(self):
        app = _make_app()
        client = TestClient(app)

        resp = client.get(
            "/api/v1/auth/analyst/accounts",
            headers={"Authorization": f"Bearer {_analyst_token()}"},
        )

        assert resp.status_code == 403
