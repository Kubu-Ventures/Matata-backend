"""Observability test suite.

Covers:
- X-Request-ID header is echoed in all responses
- Log lines never contain E.164 phone numbers
- GET /health (liveness) always returns 200
- GET /health/ready returns 503 when Postgres is down
- GET /metrics returns 401 without credentials
- GET /metrics returns valid Prometheus text with all five custom metrics
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from fastapi.testclient import TestClient

from app.main import app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    """Synchronous test client (no auth by default)."""
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def metrics_client(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    """Test client with METRICS_TOKEN configured."""
    monkeypatch.setattr("app.core.config.settings.METRICS_TOKEN", "test-metrics-token")
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Request ID middleware
# ---------------------------------------------------------------------------


class TestRequestIDMiddleware:
    def test_generates_request_id_when_absent(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert "x-request-id" in resp.headers
        # Should be a valid UUID4
        request_id = resp.headers["x-request-id"]
        uuid.UUID(request_id, version=4)  # raises if invalid

    def test_echoes_client_supplied_request_id(self, client: TestClient) -> None:
        supplied = "my-trace-id-12345"
        resp = client.get("/health", headers={"X-Request-ID": supplied})
        assert resp.headers["x-request-id"] == supplied

    def test_truncates_oversized_request_id(self, client: TestClient) -> None:
        oversized = "x" * 300
        resp = client.get("/health", headers={"X-Request-ID": oversized})
        assert len(resp.headers["x-request-id"]) <= 128

    def test_request_id_present_on_non_health_routes(self, client: TestClient) -> None:
        resp = client.get("/api/v1/stats/summary")
        assert "x-request-id" in resp.headers


# ---------------------------------------------------------------------------
# Sensitive field scrubbing
# ---------------------------------------------------------------------------

_E164_RE = re.compile(r"\+[1-9]\d{6,14}")


class TestSensitiveFieldScrubbing:
    def test_phone_number_not_in_log_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Emit a log event containing an E.164 number; assert it is redacted."""
        log = structlog.get_logger("test")
        log.info("otp_sent", phone="+254700123456")
        captured = capsys.readouterr()
        assert not _E164_RE.search(
            captured.out
        ), "E.164 phone number leaked into log output"

    def test_phone_in_message_string_is_scrubbed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        log = structlog.get_logger("test")
        log.info("Sending OTP to +254700123456")
        captured = capsys.readouterr()
        assert not _E164_RE.search(
            captured.out
        ), "E.164 phone number leaked in log message string"

    def test_jwt_token_not_in_log_output(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake_jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiJ1c2VyIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        log = structlog.get_logger("test")
        log.info("token_issued", access_token=fake_jwt)
        captured = capsys.readouterr()
        assert fake_jwt not in captured.out


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------


class TestLiveness:
    def test_returns_200_ok(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["version"] == "0.1.0"

    def test_no_downstream_io(self, client: TestClient) -> None:
        """Liveness must not be blocked by DB/Redis — patch and assert not called."""
        with (
            patch("app.api.v1.routes.health._check_postgres") as mock_pg,
            patch("app.api.v1.routes.health._check_redis") as mock_redis,
        ):
            resp = client.get("/health")
        assert resp.status_code == 200
        mock_pg.assert_not_called()
        mock_redis.assert_not_called()


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------


class TestReadiness:
    def test_returns_200_when_all_checks_pass(self, client: TestClient) -> None:
        with (
            patch(
                "app.api.v1.routes.health._check_postgres",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "app.api.v1.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "app.api.v1.routes.health._check_storage",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["postgres"] == "ok"
        assert body["checks"]["redis"] == "ok"
        assert body["checks"]["storage"] == "ok"

    def test_returns_503_when_postgres_fails(self, client: TestClient) -> None:
        with (
            patch(
                "app.api.v1.routes.health._check_postgres",
                new_callable=AsyncMock,
                return_value="error: OperationalError",
            ),
            patch(
                "app.api.v1.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "app.api.v1.routes.health._check_storage",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert "error" in body["checks"]["postgres"]

    def test_returns_503_when_redis_fails(self, client: TestClient) -> None:
        with (
            patch(
                "app.api.v1.routes.health._check_postgres",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "app.api.v1.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="error: ConnectionError",
            ),
            patch(
                "app.api.v1.routes.health._check_storage",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            resp = client.get("/health/ready")
        assert resp.status_code == 503

    def test_response_has_no_cache_header(self, client: TestClient) -> None:
        with (
            patch(
                "app.api.v1.routes.health._check_postgres",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "app.api.v1.routes.health._check_redis",
                new_callable=AsyncMock,
                return_value="ok",
            ),
            patch(
                "app.api.v1.routes.health._check_storage",
                new_callable=AsyncMock,
                return_value="ok",
            ),
        ):
            resp = client.get("/health/ready")
        assert resp.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# Worker health
# ---------------------------------------------------------------------------


class TestWorkerHealth:
    def _mock_inspect(self, active: Any, reserved: Any) -> MagicMock:
        inspect = MagicMock()
        inspect.active.return_value = active
        inspect.reserved.return_value = reserved
        return inspect

    def test_returns_200_when_queues_normal(self, client: TestClient) -> None:
        inspect = self._mock_inspect(
            active={"worker@host": [{"delivery_info": {"routing_key": "ai"}}]},
            reserved={},
        )
        with patch(
            "app.api.v1.routes.health._get_celery_inspect", return_value=inspect
        ):
            resp = client.get("/health/worker")
        assert resp.status_code == 200
        body = resp.json()
        assert body["alert"] is False

    def test_returns_503_when_queue_exceeds_threshold(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "app.core.config.settings.AI_PROCESSING_QUEUE_ALERT_DEPTH", 2
        )
        tasks = [{"delivery_info": {"routing_key": "ai"}} for _ in range(5)]
        inspect = self._mock_inspect(
            active={"worker@host": tasks},
            reserved={},
        )
        with patch(
            "app.api.v1.routes.health._get_celery_inspect", return_value=inspect
        ):
            resp = client.get("/health/worker")
        assert resp.status_code == 503
        assert resp.json()["alert"] is True

    def test_returns_503_when_celery_unreachable(self, client: TestClient) -> None:
        inspect = MagicMock()
        inspect.active.side_effect = Exception("broker unavailable")
        with patch(
            "app.api.v1.routes.health._get_celery_inspect", return_value=inspect
        ):
            resp = client.get("/health/worker")
        assert resp.status_code == 503
        assert resp.json()["status"] == "unavailable"


# ---------------------------------------------------------------------------
# Prometheus metrics endpoint
# ---------------------------------------------------------------------------

_REQUIRED_METRICS = [
    "crisismap_reports_submitted_total",
    "crisismap_moderation_rejections_total",
    "crisismap_ai_queue_depth",
    "crisismap_gis_match_confidence",
    "crisismap_export_duration_seconds",
]


class TestMetricsEndpoint:
    def test_returns_401_without_credentials(self, client: TestClient) -> None:
        resp = client.get("/metrics")
        assert resp.status_code == 401

    def test_returns_403_with_wrong_token(self, metrics_client: TestClient) -> None:
        resp = metrics_client.get(
            "/metrics", headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code == 403

    def test_returns_200_with_valid_metrics_token(
        self, metrics_client: TestClient
    ) -> None:
        resp = metrics_client.get(
            "/metrics", headers={"Authorization": "Bearer test-metrics-token"}
        )
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    def test_response_is_valid_prometheus_text_format(
        self, metrics_client: TestClient
    ) -> None:
        resp = metrics_client.get(
            "/metrics", headers={"Authorization": "Bearer test-metrics-token"}
        )
        assert resp.status_code == 200
        body = resp.text
        # Prometheus text format lines start with metric name or # HELP / # TYPE
        lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
        assert len(lines) > 0, "Expected metric sample lines in response"

    def test_all_custom_metrics_present(self, metrics_client: TestClient) -> None:
        # Ensure custom metrics are registered by importing them
        import app.core.metrics  # noqa: F401, PLC0415

        resp = metrics_client.get(
            "/metrics", headers={"Authorization": "Bearer test-metrics-token"}
        )
        body = resp.text
        for metric_name in _REQUIRED_METRICS:
            assert (
                metric_name in body
            ), f"Expected custom metric '{metric_name}' in /metrics output"
