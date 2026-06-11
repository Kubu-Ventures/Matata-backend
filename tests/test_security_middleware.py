"""Tests for cross-cutting security middleware.

Covers:
- SanitisationMiddleware strips XSS payloads from JSON bodies.
- SanitisationMiddleware leaves multipart bodies untouched.
- SecurityHeadersMiddleware attaches the required headers to every response.
- RateLimitHeaderMiddleware injects rate-limit headers on 429 responses.
- RequestIDMiddleware propagates and generates X-Request-ID.
"""

from __future__ import annotations

import json

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.core.middleware import (
    RateLimitHeaderMiddleware,
    RequestIDMiddleware,
    SanitisationMiddleware,
    SecurityHeadersMiddleware,
)

# ---------------------------------------------------------------------------
# Minimal Starlette apps for isolated middleware tests
# ---------------------------------------------------------------------------


async def _echo_body(request: Request) -> JSONResponse:
    """Return the parsed JSON body so tests can inspect sanitised values."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(body)


async def _always_ok(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def _always_429(request: Request) -> JSONResponse:
    return JSONResponse(
        {"detail": "5 per minute"},
        status_code=429,
    )


def _make_app(*middleware_classes) -> TestClient:
    middlewares = [Middleware(cls) for cls in middleware_classes]
    app = Starlette(
        routes=[
            Route("/echo", _echo_body, methods=["POST"]),
            Route("/ok", _always_ok, methods=["GET"]),
            Route("/limited", _always_429, methods=["GET"]),
        ],
        middleware=middlewares,
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# SanitisationMiddleware
# ---------------------------------------------------------------------------


class TestSanitisationMiddleware:
    def setup_method(self):
        self.client = _make_app(SanitisationMiddleware)

    def test_strips_script_tag_from_string_field(self):
        payload = {"name": "<script>alert(1)</script>Hello"}
        response = self.client.post(
            "/echo",
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "<script>" not in data["name"]
        assert "Hello" in data["name"]

    def test_strips_html_tags_from_nested_dict(self):
        payload = {"outer": {"inner": "<b>bold</b> text"}}
        response = self.client.post(
            "/echo",
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        data = response.json()
        assert "<b>" not in data["outer"]["inner"]
        assert "bold" in data["outer"]["inner"]

    def test_strips_tags_from_list_items(self):
        payload = {"items": ["<em>one</em>", "<strong>two</strong>"]}
        response = self.client.post(
            "/echo",
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        data = response.json()
        assert "<em>" not in data["items"][0]
        assert "<strong>" not in data["items"][1]

    def test_numeric_fields_are_unchanged(self):
        payload = {"count": 42, "ratio": 3.14}
        response = self.client.post(
            "/echo",
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        data = response.json()
        assert data["count"] == 42
        assert abs(data["ratio"] - 3.14) < 0.001

    def test_plain_text_body_passes_through(self):
        """Non-JSON content-type bodies are not touched."""
        response = self.client.post(
            "/echo",
            content="hello world",
            headers={"content-type": "text/plain"},
        )
        # Route returns empty dict for non-JSON; just assert no server error.
        assert response.status_code == 200

    def test_multipart_body_is_not_processed(self):
        """Multipart form data must not be read and re-encoded."""
        response = self.client.post(
            "/echo",
            files={"file": ("test.txt", b"binary content", "text/plain")},
        )
        # Should not crash; body may be empty JSON since _echo_body expects JSON.
        assert response.status_code == 200

    def test_malformed_json_passes_through(self):
        response = self.client.post(
            "/echo",
            content=b"not valid json {{{",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200

    def test_truncates_excessively_long_string(self):
        long_string = "A" * 20_000
        payload = {"field": long_string}
        response = self.client.post(
            "/echo",
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        data = response.json()
        assert len(data["field"]) <= 10_000

    def test_xss_img_onerror_stripped(self):
        payload = {"desc": '<img src=x onerror="alert(1)">text'}
        response = self.client.post(
            "/echo",
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        data = response.json()
        assert "<img" not in data["desc"]
        assert "text" in data["desc"]


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware
# ---------------------------------------------------------------------------


class TestSecurityHeadersMiddleware:
    def setup_method(self):
        self.client = _make_app(SecurityHeadersMiddleware)

    def _get_headers(self):
        response = self.client.get("/ok")
        assert response.status_code == 200
        return response.headers

    def test_x_content_type_options(self):
        assert self._get_headers()["x-content-type-options"] == "nosniff"

    def test_x_frame_options(self):
        assert self._get_headers()["x-frame-options"] == "DENY"

    def test_strict_transport_security(self):
        hsts = self._get_headers()["strict-transport-security"]
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_referrer_policy(self):
        assert (
            self._get_headers()["referrer-policy"] == "strict-origin-when-cross-origin"
        )

    def test_permissions_policy(self):
        policy = self._get_headers()["permissions-policy"]
        assert "geolocation=(self)" in policy
        assert "camera=(self)" in policy

    def test_content_security_policy(self):
        csp = self._get_headers()["content-security-policy"]
        assert "default-src 'none'" in csp

    def test_headers_present_on_error_response(self):
        """Security headers must be added even to error responses."""
        response = self.client.get("/nonexistent")
        assert "x-content-type-options" in response.headers


# ---------------------------------------------------------------------------
# RateLimitHeaderMiddleware
# ---------------------------------------------------------------------------


class TestRateLimitHeaderMiddleware:
    def setup_method(self):
        self.client = _make_app(RateLimitHeaderMiddleware)

    def test_429_includes_rate_limit_headers(self):
        response = self.client.get("/limited")
        assert response.status_code == 429
        assert "x-ratelimit-limit" in response.headers
        assert "x-ratelimit-remaining" in response.headers
        assert "retry-after" in response.headers

    def test_remaining_is_zero_on_429(self):
        response = self.client.get("/limited")
        assert response.headers["x-ratelimit-remaining"] == "0"

    def test_retry_after_is_set_on_429(self):
        response = self.client.get("/limited")
        assert int(response.headers["retry-after"]) > 0

    def test_200_response_has_no_rate_limit_headers(self):
        response = self.client.get("/ok")
        assert response.status_code == 200
        assert "x-ratelimit-remaining" not in response.headers


# ---------------------------------------------------------------------------
# RequestIDMiddleware
# ---------------------------------------------------------------------------


class TestRequestIDMiddleware:
    def setup_method(self):
        self.client = _make_app(RequestIDMiddleware)

    def test_response_contains_request_id_header(self):
        response = self.client.get("/ok")
        assert "x-request-id" in response.headers

    def test_client_supplied_id_is_echoed(self):
        response = self.client.get("/ok", headers={"X-Request-ID": "my-trace-id"})
        assert response.headers["x-request-id"] == "my-trace-id"

    def test_oversized_id_is_truncated(self):
        long_id = "X" * 300
        response = self.client.get("/ok", headers={"X-Request-ID": long_id})
        assert len(response.headers["x-request-id"]) <= 128

    def test_generated_id_is_valid_uuid(self):
        import re

        response = self.client.get("/ok")
        rid = response.headers["x-request-id"]
        uuid_pattern = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
        assert uuid_pattern.match(rid), f"Not a valid UUID4: {rid}"
