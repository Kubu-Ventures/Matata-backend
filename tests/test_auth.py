"""Tests for auth_service, auth routes, and sms gateway.

All tests are pure unit tests — no real Redis, SMS gateway, or HTTP server
is needed.  External dependencies are mocked at the boundary.

Coverage targets
----------------
* auth_service: hash_phone, OTP send/verify, JWT build/decode/verify,
  anonymous token, refresh rotation, logout, denylist, issue_analyst_token
* auth routes: all endpoints, error mapping, role enforcement, get_current_user
* sms: ConsoleSMSGateway, AfricasTalkingSMSGateway, get_sms_gateway factory
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_PHONE = "+254700123456"
_EMAIL = "analyst@example.com"
_OTP = "123456"
_ALGORITHM = "HS256"
_SECRET = "x" * 64
_SALT = "x" * 32


# ===========================================================================
# auth_service tests
# ===========================================================================


class TestHashPhone:
    def test_returns_64_char_hex(self):
        from app.services.auth_service import hash_phone

        result = hash_phone(_PHONE)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_input_same_output(self):
        from app.services.auth_service import hash_phone

        assert hash_phone(_PHONE) == hash_phone(_PHONE)

    def test_different_inputs_different_outputs(self):
        from app.services.auth_service import hash_phone

        assert hash_phone(_PHONE) != hash_phone("+254700000000")


class TestBuildAccessToken:
    def test_returns_string(self):
        from app.services.auth_service import Role, _build_access_token

        token = _build_access_token(sub="abc", role=Role.reporter)
        assert isinstance(token, str)

    def test_payload_contains_expected_claims(self):
        from app.services.auth_service import Role, _build_access_token
        from app.core.config import settings

        token = _build_access_token(sub="abc123", role=Role.analyst, tier=0)
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        assert payload["sub"] == "abc123"
        assert payload["role"] == "analyst"
        assert "jti" in payload
        assert "exp" in payload

    def test_explicit_jti_used(self):
        from app.services.auth_service import Role, _build_access_token
        from app.core.config import settings

        token = _build_access_token(sub="x", role=Role.reporter, jti="my-jti")
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        assert payload["jti"] == "my-jti"


class TestDecodeAccessToken:
    def test_valid_token_decoded(self):
        from app.services.auth_service import Role, _build_access_token, decode_access_token

        token = _build_access_token(sub="abc", role=Role.reporter)
        payload = decode_access_token(token)
        assert payload["sub"] == "abc"

    def test_invalid_token_raises(self):
        from app.services.auth_service import InvalidTokenError, decode_access_token

        with pytest.raises(InvalidTokenError):
            decode_access_token("not.a.token")

    def test_tampered_token_raises(self):
        from app.services.auth_service import Role, _build_access_token, InvalidTokenError, decode_access_token

        token = _build_access_token(sub="abc", role=Role.reporter)
        tampered = token[:-4] + "xxxx"
        with pytest.raises(InvalidTokenError):
            decode_access_token(tampered)


class TestIssueAnonymousToken:
    @pytest.mark.asyncio
    async def test_returns_string(self):
        from app.services.auth_service import issue_anonymous_token

        token = await issue_anonymous_token()
        assert isinstance(token, str)

    @pytest.mark.asyncio
    async def test_role_is_anonymous_reporter(self):
        from app.services.auth_service import issue_anonymous_token, decode_access_token

        token = await issue_anonymous_token()
        payload = decode_access_token(token)
        assert payload["role"] == "anonymous_reporter"

    @pytest.mark.asyncio
    async def test_unique_tokens(self):
        from app.services.auth_service import issue_anonymous_token

        t1 = await issue_anonymous_token()
        t2 = await issue_anonymous_token()
        assert t1 != t2


class TestSendOtp:
    @pytest.mark.asyncio
    async def test_stores_otp_in_redis(self, mock_redis):
        from app.services.auth_service import send_otp

        with patch("app.services.auth_service.get_sms_gateway") as mock_gw:
            mock_gw.return_value.send_otp = MagicMock()
            await send_otp(_PHONE, mock_redis)

        mock_redis.set.assert_awaited_once()
        call_args = mock_redis.set.call_args
        assert call_args.kwargs.get("ex") == 300 or call_args.args[2] == 300

    @pytest.mark.asyncio
    async def test_calls_sms_gateway(self, mock_redis):
        from app.services.auth_service import send_otp

        with patch("app.services.auth_service.get_sms_gateway") as mock_gw:
            send_mock = MagicMock()
            mock_gw.return_value.send_otp = send_mock
            await send_otp(_PHONE, mock_redis)

        send_mock.assert_called_once()
        args = send_mock.call_args[0]
        assert args[0] == _PHONE

    @pytest.mark.asyncio
    async def test_deletes_otp_on_sms_failure(self, mock_redis):
        from app.services.auth_service import send_otp
        from app.services.sms import SMSDeliveryError

        with patch("app.services.auth_service.get_sms_gateway") as mock_gw:
            mock_gw.return_value.send_otp = MagicMock(side_effect=SMSDeliveryError("fail"))
            with pytest.raises(SMSDeliveryError):
                await send_otp(_PHONE, mock_redis)

        mock_redis.delete.assert_awaited_once()


class TestVerifyOtp:
    def _make_redis(self, stored_otp=_OTP, attempts=0, locked=False):
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: (
            str(_OTP_MAX_ATTEMPTS if locked else attempts).encode() if "attempts" in key
            else stored_otp.encode() if stored_otp else None
        ))
        redis.set = AsyncMock()
        redis.delete = AsyncMock()
        redis.incr = AsyncMock(return_value=attempts + 1)
        redis.expire = AsyncMock()
        return redis

    @pytest.mark.asyncio
    async def test_valid_otp_returns_tokens(self):
        from app.services.auth_service import verify_otp

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: (
            b"0" if "attempts" in key else _OTP.encode()
        ))
        redis.delete = AsyncMock()
        redis.set = AsyncMock()

        access, refresh = await verify_otp(_PHONE, _OTP, redis)
        assert isinstance(access, str)
        assert isinstance(refresh, str)

    @pytest.mark.asyncio
    async def test_wrong_otp_raises_invalid(self):
        from app.services.auth_service import verify_otp, InvalidOTPError

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: (
            b"0" if "attempts" in key else b"999999"
        ))
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock()

        with pytest.raises(InvalidOTPError):
            await verify_otp(_PHONE, "000000", redis)

    @pytest.mark.asyncio
    async def test_no_otp_raises_not_found(self):
        from app.services.auth_service import verify_otp, OTPNotFoundError

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        with pytest.raises(OTPNotFoundError):
            await verify_otp(_PHONE, _OTP, redis)

    @pytest.mark.asyncio
    async def test_locked_out_raises(self):
        from app.services.auth_service import verify_otp, OTPLockedOutError

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: (
            b"5" if "attempts" in key else _OTP.encode()
        ))

        with pytest.raises(OTPLockedOutError):
            await verify_otp(_PHONE, _OTP, redis)

    @pytest.mark.asyncio
    async def test_success_cleans_up_redis_keys(self):
        from app.services.auth_service import verify_otp

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=lambda key: (
            b"0" if "attempts" in key else _OTP.encode()
        ))
        redis.delete = AsyncMock()
        redis.set = AsyncMock()

        await verify_otp(_PHONE, _OTP, redis)
        assert redis.delete.await_count == 2


# Import for test above
_OTP_MAX_ATTEMPTS = 5


class TestRotateRefreshToken:
    @pytest.mark.asyncio
    async def test_valid_token_returns_new_pair(self):
        from app.services.auth_service import rotate_refresh_token, Role

        payload = json.dumps({"sub": "abc", "role": "reporter", "tier": 1})
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=payload)
        redis.delete = AsyncMock()
        redis.set = AsyncMock()

        access, refresh = await rotate_refresh_token("old-token", redis)
        assert isinstance(access, str)
        assert isinstance(refresh, str)

    @pytest.mark.asyncio
    async def test_invalid_token_raises(self):
        from app.services.auth_service import rotate_refresh_token, InvalidTokenError

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        with pytest.raises(InvalidTokenError):
            await rotate_refresh_token("bad-token", redis)

    @pytest.mark.asyncio
    async def test_old_token_deleted(self):
        from app.services.auth_service import rotate_refresh_token

        payload = json.dumps({"sub": "abc", "role": "reporter", "tier": 1})
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=payload)
        redis.delete = AsyncMock()
        redis.set = AsyncMock()

        await rotate_refresh_token("old-token", redis)
        redis.delete.assert_awaited_once()


class TestLogout:
    @pytest.mark.asyncio
    async def test_adds_jti_to_denylist(self):
        from app.services.auth_service import logout, Role, _build_access_token

        token = _build_access_token(sub="abc", role=Role.reporter)
        redis = AsyncMock()
        redis.set = AsyncMock()

        await logout(token, redis)
        redis.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_token_raises(self):
        from app.services.auth_service import logout, InvalidTokenError

        redis = AsyncMock()
        with pytest.raises(InvalidTokenError):
            await logout("bad.token.here", redis)


class TestVerifyAccessToken:
    @pytest.mark.asyncio
    async def test_valid_token_returns_payload(self):
        from app.services.auth_service import verify_access_token, Role, _build_access_token

        token = _build_access_token(sub="abc", role=Role.analyst)
        redis = AsyncMock()
        redis.exists = AsyncMock(return_value=0)

        payload = await verify_access_token(token, redis)
        assert payload["sub"] == "abc"

    @pytest.mark.asyncio
    async def test_denylisted_token_raises(self):
        from app.services.auth_service import verify_access_token, Role, _build_access_token, InvalidTokenError

        token = _build_access_token(sub="abc", role=Role.analyst)
        redis = AsyncMock()
        redis.exists = AsyncMock(return_value=1)

        with pytest.raises(InvalidTokenError):
            await verify_access_token(token, redis)


class TestIssueAnalystToken:
    @pytest.mark.asyncio
    async def test_returns_token_pair(self):
        from app.services.auth_service import issue_analyst_token, Role

        redis = AsyncMock()
        redis.set = AsyncMock()

        access, refresh = await issue_analyst_token(_EMAIL, Role.analyst, redis)
        assert isinstance(access, str)
        assert isinstance(refresh, str)

    @pytest.mark.asyncio
    async def test_role_embedded_in_access_token(self):
        from app.services.auth_service import issue_analyst_token, Role, decode_access_token

        redis = AsyncMock()
        redis.set = AsyncMock()

        access, _ = await issue_analyst_token(_EMAIL, Role.admin, redis)
        payload = decode_access_token(access)
        assert payload["role"] == "admin"

    @pytest.mark.asyncio
    async def test_reporter_role_raises(self):
        from app.services.auth_service import issue_analyst_token, Role

        redis = AsyncMock()
        with pytest.raises(ValueError):
            await issue_analyst_token(_EMAIL, Role.reporter, redis)

    @pytest.mark.asyncio
    async def test_email_not_in_access_token(self):
        from app.services.auth_service import issue_analyst_token, Role, decode_access_token

        redis = AsyncMock()
        redis.set = AsyncMock()

        access, _ = await issue_analyst_token(_EMAIL, Role.analyst, redis)
        payload = decode_access_token(access)
        assert _EMAIL not in str(payload)


# ===========================================================================
# SMS gateway tests
# ===========================================================================


class TestConsoleSMSGateway:
    def test_send_otp_does_not_raise(self, capsys):
        from app.services.sms import ConsoleSMSGateway

        gw = ConsoleSMSGateway()
        gw.send_otp(_PHONE, "123456")
        out = capsys.readouterr().out
        assert "123456" in out

    def test_phone_not_in_output(self, capsys):
        from app.services.sms import ConsoleSMSGateway

        gw = ConsoleSMSGateway()
        gw.send_otp(_PHONE, "123456")
        out = capsys.readouterr().out
        assert _PHONE not in out

    def test_satisfies_protocol(self):
        from app.services.sms import ConsoleSMSGateway, SMSGateway

        assert isinstance(ConsoleSMSGateway(), SMSGateway)


class TestAfricasTalkingSMSGateway:
    def test_raises_if_no_credentials(self):
        from app.services.sms import AfricasTalkingSMSGateway, SMSDeliveryError

        with patch("app.services.sms.settings") as mock_settings:
            mock_settings.AFRICASTALKING_API_KEY = ""
            mock_settings.AFRICASTALKING_USERNAME = ""
            with pytest.raises(SMSDeliveryError):
                AfricasTalkingSMSGateway()

    def test_send_otp_raises_on_http_error(self):
        import httpx
        from app.services.sms import AfricasTalkingSMSGateway, SMSDeliveryError

        with patch("app.services.sms.settings") as mock_settings:
            mock_settings.AFRICASTALKING_API_KEY = "key"
            mock_settings.AFRICASTALKING_USERNAME = "user"
            gw = AfricasTalkingSMSGateway()

        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=MagicMock(status_code=500, text="err")
            )
            mock_post.return_value = mock_response
            with pytest.raises(SMSDeliveryError):
                gw.send_otp(_PHONE, "123456")

    def test_send_otp_raises_on_network_error(self):
        import httpx
        from app.services.sms import AfricasTalkingSMSGateway, SMSDeliveryError

        with patch("app.services.sms.settings") as mock_settings:
            mock_settings.AFRICASTALKING_API_KEY = "key"
            mock_settings.AFRICASTALKING_USERNAME = "user"
            gw = AfricasTalkingSMSGateway()

        with patch("httpx.post", side_effect=httpx.RequestError("network fail")):
            with pytest.raises(SMSDeliveryError):
                gw.send_otp(_PHONE, "123456")

    def test_send_otp_raises_on_empty_recipients(self):
        from app.services.sms import AfricasTalkingSMSGateway, SMSDeliveryError

        with patch("app.services.sms.settings") as mock_settings:
            mock_settings.AFRICASTALKING_API_KEY = "key"
            mock_settings.AFRICASTALKING_USERNAME = "user"
            gw = AfricasTalkingSMSGateway()

        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {"SMSMessageData": {"Recipients": []}}
            mock_post.return_value = mock_response
            with pytest.raises(SMSDeliveryError):
                gw.send_otp(_PHONE, "123456")

    def test_send_otp_success(self):
        from app.services.sms import AfricasTalkingSMSGateway

        with patch("app.services.sms.settings") as mock_settings:
            mock_settings.AFRICASTALKING_API_KEY = "key"
            mock_settings.AFRICASTALKING_USERNAME = "user"
            gw = AfricasTalkingSMSGateway()

        with patch("httpx.post") as mock_post:
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.json.return_value = {
                "SMSMessageData": {"Recipients": [{"status": "Success"}]}
            }
            mock_post.return_value = mock_response
            gw.send_otp(_PHONE, "123456")  # should not raise


class TestGetSmsGateway:
    def test_console_gateway(self):
        from app.services.sms import get_sms_gateway, ConsoleSMSGateway

        with patch("app.services.sms.settings") as s:
            s.SMS_GATEWAY = "console"
            gw = get_sms_gateway()
        assert isinstance(gw, ConsoleSMSGateway)

    def test_africastalking_gateway(self):
        from app.services.sms import get_sms_gateway, AfricasTalkingSMSGateway

        with patch("app.services.sms.settings") as s:
            s.SMS_GATEWAY = "africastalking"
            s.AFRICASTALKING_API_KEY = "key"
            s.AFRICASTALKING_USERNAME = "user"
            gw = get_sms_gateway()
        assert isinstance(gw, AfricasTalkingSMSGateway)

    def test_unknown_gateway_raises(self):
        from app.services.sms import get_sms_gateway

        with patch("app.services.sms.settings") as s:
            s.SMS_GATEWAY = "twilio"
            with pytest.raises(ValueError):
                get_sms_gateway()


# ===========================================================================
# Auth route tests
# ===========================================================================


def _make_app():
    """Build a minimal FastAPI app with the auth router mounted."""
    from fastapi import FastAPI
    from app.api.v1.routes.auth import router
    from app.core.dependencies import get_redis

    app = FastAPI()

    async def override_redis():
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock(return_value=True)
        redis.delete = AsyncMock(return_value=1)
        redis.exists = AsyncMock(return_value=0)
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock(return_value=True)
        return redis

    app.dependency_overrides[get_redis] = override_redis
    app.include_router(router, prefix="/api/v1")
    return app


class TestAnonymousEndpoint:
    def test_returns_session_token(self):
        client = TestClient(_make_app())
        resp = client.post("/api/v1/auth/anonymous")
        assert resp.status_code == 200
        assert "session_token" in resp.json()


class TestSendOtpEndpoint:
    def test_valid_phone_returns_200(self):
        app = _make_app()
        client = TestClient(app)

        with patch("app.api.v1.routes.auth.auth_service.send_otp", new=AsyncMock()):
            resp = client.post("/api/v1/auth/otp/send", json={"phone": _PHONE})

        assert resp.status_code == 200
        assert resp.json()["message"] == "OTP sent successfully."

    def test_invalid_phone_returns_422(self):
        client = TestClient(_make_app())
        resp = client.post("/api/v1/auth/otp/send", json={"phone": "not-a-phone"})
        assert resp.status_code == 422

    def test_sms_failure_returns_503(self):
        from app.services.sms import SMSDeliveryError

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.send_otp",
            new=AsyncMock(side_effect=SMSDeliveryError("fail")),
        ):
            resp = client.post("/api/v1/auth/otp/send", json={"phone": _PHONE})

        assert resp.status_code == 503


class TestVerifyOtpEndpoint:
    def test_valid_otp_returns_tokens(self):
        from app.services.auth_service import Role

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.verify_otp",
            new=AsyncMock(return_value=("access.token.here", "refresh-token")),
        ):
            resp = client.post(
                "/api/v1/auth/otp/verify",
                json={"phone": _PHONE, "otp": "123456"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert "refresh_token" in data

    def test_invalid_otp_returns_400(self):
        from app.services.auth_service import InvalidOTPError

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.verify_otp",
            new=AsyncMock(side_effect=InvalidOTPError("bad otp")),
        ):
            resp = client.post(
                "/api/v1/auth/otp/verify",
                json={"phone": _PHONE, "otp": "000000"},
            )

        assert resp.status_code == 400

    def test_lockout_returns_429(self):
        from app.services.auth_service import OTPLockedOutError

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.verify_otp",
            new=AsyncMock(side_effect=OTPLockedOutError("locked")),
        ):
            resp = client.post(
                "/api/v1/auth/otp/verify",
                json={"phone": _PHONE, "otp": "000000"},
            )

        assert resp.status_code == 429

    def test_otp_not_found_returns_400(self):
        from app.services.auth_service import OTPNotFoundError

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.verify_otp",
            new=AsyncMock(side_effect=OTPNotFoundError("not found")),
        ):
            resp = client.post(
                "/api/v1/auth/otp/verify",
                json={"phone": _PHONE, "otp": "000000"},
            )

        assert resp.status_code == 400

    def test_short_otp_returns_422(self):
        client = TestClient(_make_app())
        resp = client.post(
            "/api/v1/auth/otp/verify",
            json={"phone": _PHONE, "otp": "123"},
        )
        assert resp.status_code == 422


class TestRefreshEndpoint:
    def test_valid_refresh_returns_new_tokens(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.rotate_refresh_token",
            new=AsyncMock(return_value=("new.access.token", "new-refresh")),
        ):
            resp = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": "old-refresh-token"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert "refresh_token" in data

    def test_invalid_refresh_returns_401(self):
        from app.services.auth_service import InvalidTokenError

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.rotate_refresh_token",
            new=AsyncMock(side_effect=InvalidTokenError("bad")),
        ):
            resp = client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": "bad-token"},
            )

        assert resp.status_code == 401


class TestLogoutEndpoint:
    def _valid_bearer(self):
        from app.services.auth_service import Role, _build_access_token
        return _build_access_token(sub="abc", role=Role.reporter)

    def test_valid_token_returns_200(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.logout",
            new=AsyncMock(),
        ):
            resp = client.delete(
                "/api/v1/auth/logout",
                headers={"Authorization": f"Bearer {self._valid_bearer()}"},
            )

        assert resp.status_code == 200

    def test_no_token_returns_401(self):
        client = TestClient(_make_app())
        resp = client.delete("/api/v1/auth/logout")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        from app.services.auth_service import InvalidTokenError

        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.logout",
            new=AsyncMock(side_effect=InvalidTokenError("bad")),
        ):
            resp = client.delete(
                "/api/v1/auth/logout",
                headers={"Authorization": "Bearer bad.token.value"},
            )

        assert resp.status_code == 401

    def test_session_token_header_accepted(self):
        app = _make_app()
        client = TestClient(app)

        with patch(
            "app.api.v1.routes.auth.auth_service.logout",
            new=AsyncMock(),
        ):
            resp = client.delete(
                "/api/v1/auth/logout",
                headers={"X-Session-Token": self._valid_bearer()},
            )

        assert resp.status_code == 200


class TestGetCurrentUser:
    def _make_protected_app(self):
        from fastapi import FastAPI, Depends
        from app.api.v1.routes.auth import get_current_user, router
        from app.core.dependencies import get_redis

        app = FastAPI()

        async def override_redis():
            redis = AsyncMock()
            redis.exists = AsyncMock(return_value=0)
            return redis

        app.dependency_overrides[get_redis] = override_redis

        @app.get("/protected")
        async def protected(user=Depends(get_current_user)):
            return {"role": user["role"]}

        return app

    def test_valid_bearer_succeeds(self):
        from app.services.auth_service import Role, _build_access_token

        app = self._make_protected_app()
        client = TestClient(app)
        token = _build_access_token(sub="abc", role=Role.analyst)

        resp = client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["role"] == "analyst"

    def test_missing_token_returns_401(self):
        client = TestClient(self._make_protected_app())
        resp = client.get("/protected")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self):
        client = TestClient(self._make_protected_app())
        resp = client.get("/protected", headers={"Authorization": "Bearer bad.token"})
        assert resp.status_code == 401

    def test_session_token_header_works(self):
        from app.services.auth_service import Role, _build_access_token

        app = self._make_protected_app()
        client = TestClient(app)
        token = _build_access_token(sub="abc", role=Role.anonymous_reporter)

        resp = client.get("/protected", headers={"X-Session-Token": token})
        assert resp.status_code == 200


class TestRequireRole:
    def _make_role_app(self, *permitted_roles):
        from fastapi import FastAPI, Depends
        from app.api.v1.routes.auth import require_role, router
        from app.core.dependencies import get_redis
        from app.services.auth_service import Role

        app = FastAPI()

        async def override_redis():
            redis = AsyncMock()
            redis.exists = AsyncMock(return_value=0)
            return redis

        app.dependency_overrides[get_redis] = override_redis

        @app.get("/analyst-only")
        async def analyst_only(user=Depends(require_role(*permitted_roles))):
            return {"ok": True}

        return app

    def test_correct_role_allowed(self):
        from app.services.auth_service import Role, _build_access_token

        app = self._make_role_app(Role.analyst)
        client = TestClient(app)
        token = _build_access_token(sub="abc", role=Role.analyst)

        resp = client.get(
            "/analyst-only", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200

    def test_wrong_role_returns_403(self):
        from app.services.auth_service import Role, _build_access_token

        app = self._make_role_app(Role.admin)
        client = TestClient(app)
        token = _build_access_token(sub="abc", role=Role.reporter)

        resp = client.get(
            "/analyst-only", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 403


class TestAuthErrorMapping:
    """Verify _auth_error_to_http maps every exception subclass correctly."""

    def test_lockout_maps_to_429(self):
        from app.api.v1.routes.auth import _auth_error_to_http
        from app.services.auth_service import OTPLockedOutError

        exc = _auth_error_to_http(OTPLockedOutError("locked"))
        assert exc.status_code == 429

    def test_otp_not_found_maps_to_400(self):
        from app.api.v1.routes.auth import _auth_error_to_http
        from app.services.auth_service import OTPNotFoundError

        exc = _auth_error_to_http(OTPNotFoundError("not found"))
        assert exc.status_code == 400

    def test_invalid_otp_maps_to_400(self):
        from app.api.v1.routes.auth import _auth_error_to_http
        from app.services.auth_service import InvalidOTPError

        exc = _auth_error_to_http(InvalidOTPError("bad"))
        assert exc.status_code == 400

    def test_invalid_token_maps_to_401(self):
        from app.api.v1.routes.auth import _auth_error_to_http
        from app.services.auth_service import InvalidTokenError

        exc = _auth_error_to_http(InvalidTokenError("bad token"))
        assert exc.status_code == 401

    def test_generic_auth_error_maps_to_401(self):
        from app.api.v1.routes.auth import _auth_error_to_http
        from app.services.auth_service import AuthError

        exc = _auth_error_to_http(AuthError("generic"))
        assert exc.status_code == 401