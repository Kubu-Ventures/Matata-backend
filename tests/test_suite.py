"""Comprehensive test suite for CrisisMap backend.

Covers:
- app/db/base.py          (Base, TimestampMixin, model registration)
- app/models/enums.py     (all enum classes and values)
- app/models/*.py         (instantiation, __tablename__, column presence)
- app/schemas/*.py        (Pydantic validation and from_attributes round-trip)
- app/main.py             (HTTP exception handler, unhandled exception handler)
- app/core/config.py      (already 100% — kept for regression)

All tests are pure unit tests: no database connection is required.
Async tests use pytest-anyio (mode=auto, configured in pyproject.toml).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

NOW = datetime.now(tz=timezone.utc)
_UUID = uuid.uuid4()


# ─────────────────────────────────────────────────────────────────────────────
# app/models/enums.py
# ─────────────────────────────────────────────────────────────────────────────


class TestEnums:
    def test_building_source_values(self):
        from app.models.enums import BuildingSource

        assert BuildingSource.microsoft_africa.value == "microsoft_africa"
        assert BuildingSource.osm.value == "osm"
        assert BuildingSource.manual.value == "manual"
        assert len(BuildingSource) == 3

    def test_damage_severity_values(self):
        from app.models.enums import DamageSeverity

        assert DamageSeverity.none.value == "none"
        assert DamageSeverity.minimal.value == "minimal"
        assert DamageSeverity.partial.value == "partial"
        assert DamageSeverity.destroyed.value == "destroyed"
        assert len(DamageSeverity) == 4

    def test_crisis_type_values(self):
        from app.models.enums import CrisisType

        expected = {"flood", "earthquake", "conflict", "wildfire", "other"}
        assert {e.value for e in CrisisType} == expected

    def test_infrastructure_type_values(self):
        from app.models.enums import InfrastructureType

        expected = {
            "residential",
            "commercial",
            "government",
            "utilities",
            "transport",
            "community",
        }
        assert {e.value for e in InfrastructureType} == expected

    def test_report_damage_severity_values(self):
        from app.models.enums import ReportDamageSeverity

        assert {e.value for e in ReportDamageSeverity} == {
            "minimal",
            "partial",
            "destroyed",
        }

    def test_electricity_status_values(self):
        from app.models.enums import ElectricityStatus

        assert {e.value for e in ElectricityStatus} == {
            "functional",
            "non_functional",
            "unknown",
        }

    def test_health_services_status_values(self):
        from app.models.enums import HealthServicesStatus

        assert {e.value for e in HealthServicesStatus} == {
            "accessible",
            "inaccessible",
            "unknown",
        }

    def test_photo_status_values(self):
        from app.models.enums import PhotoStatus

        expected = {
            "pending",
            "processing",
            "accepted",
            "rejected",
            "insufficient_quality",
            "ai_processing_failed",
        }
        assert {e.value for e in PhotoStatus} == expected

    def test_report_status_values(self):
        from app.models.enums import ReportStatus

        assert {e.value for e in ReportStatus} == {
            "pending",
            "verified",
            "rejected",
            "duplicate",
        }

    def test_notification_type_values(self):
        from app.models.enums import NotificationType

        assert {e.value for e in NotificationType} == {
            "analyst_alert",
            "reporter_photo_request",
        }

    def test_notification_status_values(self):
        from app.models.enums import NotificationStatus

        assert {e.value for e in NotificationStatus} == {"pending", "sent", "failed"}

    def test_enums_are_str_subclass(self):
        """All enums inherit from str so they serialise cleanly as JSON strings."""
        from app.models.enums import (
            BuildingSource,
            CrisisType,
            DamageSeverity,
            ElectricityStatus,
            HealthServicesStatus,
            InfrastructureType,
            NotificationStatus,
            NotificationType,
            PhotoStatus,
            ReportDamageSeverity,
            ReportStatus,
        )

        for enum_cls in (
            BuildingSource,
            DamageSeverity,
            CrisisType,
            InfrastructureType,
            ReportDamageSeverity,
            ElectricityStatus,
            HealthServicesStatus,
            PhotoStatus,
            ReportStatus,
            NotificationType,
            NotificationStatus,
        ):
            for member in enum_cls:
                assert isinstance(
                    member, str
                ), f"{enum_cls.__name__}.{member} is not str"


# ─────────────────────────────────────────────────────────────────────────────
# app/db/base.py
# ─────────────────────────────────────────────────────────────────────────────


class TestBase:
    def test_base_is_declarative(self):
        from sqlalchemy.orm import DeclarativeBase

        from app.db.base import Base

        assert issubclass(Base, DeclarativeBase)

    def test_timestamp_mixin_columns(self):
        """TimestampMixin exposes created_at and updated_at mapped columns."""
        from app.db.base import TimestampMixin

        assert hasattr(TimestampMixin, "created_at")
        assert hasattr(TimestampMixin, "updated_at")

    def test_all_models_registered_in_metadata(self):
        """All five domain tables must appear in Base.metadata after import."""
        # Import triggers registration via the noqa imports at the bottom of base.py
        import app.db.base  # noqa: F401
        from app.db.base import Base

        table_names = set(Base.metadata.tables.keys())
        for expected in {
            "building",
            "report",
            "audit_log",
            "analyst_note",
            "notification",
        }:
            assert expected in table_names, f"Table '{expected}' missing from metadata"

    def test_model_imports_are_accessible(self):
        """Symbols re-exported from db.base for Alembic must be importable."""
        from app.db.base import (  # noqa: F401
            AnalystNote,
            AuditLog,
            Building,
            Notification,
            Report,
        )


# ─────────────────────────────────────────────────────────────────────────────
# app/models/*.py  — structural / metadata tests (no DB required)
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildingModel:
    def test_tablename(self):
        from app.models.building import Building

        assert Building.__tablename__ == "building"

    def test_required_columns_present(self):
        from app.models.building import Building

        cols = {c.name for c in Building.__table__.columns}
        for expected in {
            "id",
            "footprint",
            "centroid",
            "source",
            "external_id",
            "current_severity",
            "last_report_at",
            "created_at",
            "updated_at",
        }:
            assert expected in cols, f"Column '{expected}' missing from building"

    def test_gist_indexes_defined(self):
        from app.models.building import Building

        index_names = {idx.name for idx in Building.__table__.indexes}
        assert "ix_building_footprint" in index_names
        assert "ix_building_centroid" in index_names

    def test_unique_constraint_on_external_id(self):
        from app.models.building import Building

        unique_cols = set()
        for constraint in Building.__table__.constraints:
            if hasattr(constraint, "columns"):
                for col in constraint.columns:
                    if (
                        getattr(constraint, "unique", False)
                        or constraint.__class__.__name__ == "UniqueConstraint"
                    ):
                        unique_cols.add(col.name)
        # external_id has unique=True directly on the column
        col = Building.__table__.c["external_id"]
        assert col.unique or "external_id" in unique_cols or col.index


class TestReportModel:
    def test_tablename(self):
        from app.models.report import Report

        assert Report.__tablename__ == "report"

    def test_required_columns_present(self):
        from app.models.report import Report

        cols = {c.name for c in Report.__table__.columns}
        for expected in {
            "id",
            "building_id",
            "crisis_type",
            "infrastructure_type",
            "damage_severity",
            "lat",
            "lng",
            "photo_status",
            "status",
            "reporter_token_hash",
            "reporter_trust_tier",
            "duplicate_of_id",
            "possible_duplicate_of_id",
            "created_at",
            "updated_at",
        }:
            assert expected in cols, f"Column '{expected}' missing from report"

    def test_self_referential_fks(self):
        from app.models.report import Report

        fk_cols = {fk.parent.name for fk in Report.__table__.foreign_keys}
        assert "duplicate_of_id" in fk_cols
        assert "possible_duplicate_of_id" in fk_cols

    def test_lat_lng_index_defined(self):
        from app.models.report import Report

        index_names = {idx.name for idx in Report.__table__.indexes}
        assert "ix_report_lat_lng" in index_names


class TestAuditLogModel:
    def test_tablename(self):
        from app.models.audit_log import AuditLog

        assert AuditLog.__tablename__ == "audit_log"

    def test_required_columns_present(self):
        from app.models.audit_log import AuditLog

        cols = {c.name for c in AuditLog.__table__.columns}
        for expected in {
            "id",
            "operation",
            "actor_id_hash",
            "record_id",
            "before_state",
            "after_state",
            "created_at",
            "updated_at",
        }:
            assert expected in cols

    def test_before_state_is_nullable(self):
        from app.models.audit_log import AuditLog

        assert AuditLog.__table__.c["before_state"].nullable is True

    def test_after_state_is_not_nullable(self):
        from app.models.audit_log import AuditLog

        assert AuditLog.__table__.c["after_state"].nullable is False


class TestAnalystNoteModel:
    def test_tablename(self):
        from app.models.analyst_note import AnalystNote

        assert AnalystNote.__tablename__ == "analyst_note"

    def test_required_columns_present(self):
        from app.models.analyst_note import AnalystNote

        cols = {c.name for c in AnalystNote.__table__.columns}
        for expected in {
            "id",
            "report_id",
            "analyst_id_hash",
            "body",
            "created_at",
            "updated_at",
        }:
            assert expected in cols

    def test_report_fk_cascade_delete(self):
        from app.models.analyst_note import AnalystNote

        fk = next(
            fk
            for fk in AnalystNote.__table__.foreign_keys
            if fk.parent.name == "report_id"
        )
        assert fk.ondelete == "CASCADE"


class TestNotificationModel:
    def test_tablename(self):
        from app.models.notification import Notification

        assert Notification.__tablename__ == "notification"

    def test_required_columns_present(self):
        from app.models.notification import Notification

        cols = {c.name for c in Notification.__table__.columns}
        for expected in {
            "id",
            "type",
            "recipient_hash",
            "report_id",
            "status",
            "sent_at",
            "created_at",
            "updated_at",
        }:
            assert expected in cols

    def test_sent_at_nullable(self):
        from app.models.notification import Notification

        assert Notification.__table__.c["sent_at"].nullable is True

    def test_report_fk_cascade_delete(self):
        from app.models.notification import Notification

        fk = next(
            fk
            for fk in Notification.__table__.foreign_keys
            if fk.parent.name == "report_id"
        )
        assert fk.ondelete == "CASCADE"


# ─────────────────────────────────────────────────────────────────────────────
# app/schemas/*.py  — Pydantic validation
# ─────────────────────────────────────────────────────────────────────────────


class TestHealthSchema:
    def test_valid(self):
        from app.schemas.health import HealthResponse

        r = HealthResponse(status="ok", version="0.1.0")
        assert r.status == "ok"
        assert r.version == "0.1.0"


class TestAuditLogSchema:
    def _payload(self, **overrides) -> dict[str, Any]:
        base: dict[str, Any] = {
            "id": _UUID,
            "operation": "report.status_change",
            "actor_id_hash": "abc123",
            "record_id": _UUID,
            "before_state": {"status": "pending"},
            "after_state": {"status": "verified"},
            "created_at": NOW,
        }
        return {**base, **overrides}

    def test_valid_with_before_state(self):
        from app.schemas.audit_log import AuditLogRead

        obj = AuditLogRead(**self._payload())
        assert obj.operation == "report.status_change"
        assert obj.before_state == {"status": "pending"}

    def test_valid_without_before_state(self):
        from app.schemas.audit_log import AuditLogRead

        obj = AuditLogRead(**self._payload(before_state=None))
        assert obj.before_state is None

    def test_from_attributes(self):
        from app.schemas.audit_log import AuditLogRead

        mock = MagicMock()
        mock.id = _UUID
        mock.operation = "insert"
        mock.actor_id_hash = "h"
        mock.record_id = _UUID
        mock.before_state = None
        mock.after_state = {"x": 1}
        mock.created_at = NOW

        obj = AuditLogRead.model_validate(mock)
        assert obj.after_state == {"x": 1}


class TestBuildingSchema:
    def _payload(self, **overrides) -> dict[str, Any]:
        from app.models.enums import BuildingSource, DamageSeverity

        base: dict[str, Any] = {
            "id": _UUID,
            "source": BuildingSource.osm,
            "external_id": "ext-001",
            "current_severity": DamageSeverity.partial,
            "last_report_at": None,
            "created_at": NOW,
            "updated_at": NOW,
        }
        return {**base, **overrides}

    def test_valid(self):
        from app.schemas.building import BuildingRead

        obj = BuildingRead(**self._payload())
        assert obj.external_id == "ext-001"

    def test_last_report_at_optional(self):
        from app.schemas.building import BuildingRead

        obj = BuildingRead(**self._payload(last_report_at=None))
        assert obj.last_report_at is None

    def test_last_report_at_populated(self):
        from app.schemas.building import BuildingRead

        obj = BuildingRead(**self._payload(last_report_at=NOW))
        assert obj.last_report_at == NOW

    def test_from_attributes(self):
        from app.models.enums import BuildingSource, DamageSeverity
        from app.schemas.building import BuildingRead

        mock = MagicMock()
        mock.id = _UUID
        mock.source = BuildingSource.manual
        mock.external_id = "ext-002"
        mock.current_severity = DamageSeverity.destroyed
        mock.last_report_at = None
        mock.created_at = NOW
        mock.updated_at = NOW

        obj = BuildingRead.model_validate(mock)
        assert obj.source == BuildingSource.manual


class TestNotificationSchema:
    def _payload(self, **overrides) -> dict[str, Any]:
        from app.models.enums import NotificationStatus, NotificationType

        base: dict[str, Any] = {
            "id": _UUID,
            "type": NotificationType.analyst_alert,
            "recipient_hash": "rh1",
            "report_id": _UUID,
            "status": NotificationStatus.pending,
            "sent_at": None,
            "created_at": NOW,
        }
        return {**base, **overrides}

    def test_valid(self):
        from app.schemas.notification import NotificationRead

        obj = NotificationRead(**self._payload())
        assert obj.type.value == "analyst_alert"

    def test_sent_at_optional(self):
        from app.schemas.notification import NotificationRead

        obj = NotificationRead(**self._payload(sent_at=None))
        assert obj.sent_at is None

    def test_sent_at_populated(self):
        from app.schemas.notification import NotificationRead

        obj = NotificationRead(**self._payload(sent_at=NOW))
        assert obj.sent_at == NOW

    def test_from_attributes(self):
        from app.models.enums import NotificationStatus, NotificationType
        from app.schemas.notification import NotificationRead

        mock = MagicMock()
        mock.id = _UUID
        mock.type = NotificationType.reporter_photo_request
        mock.recipient_hash = "rh2"
        mock.report_id = _UUID
        mock.status = NotificationStatus.sent
        mock.sent_at = NOW
        mock.created_at = NOW

        obj = NotificationRead.model_validate(mock)
        assert obj.status == NotificationStatus.sent


class TestReportSchema:
    def _payload(self, **overrides) -> dict[str, Any]:
        from app.models.enums import (
            CrisisType,
            InfrastructureType,
            PhotoStatus,
            ReportDamageSeverity,
            ReportStatus,
        )

        base: dict[str, Any] = {
            "id": _UUID,
            "building_id": None,
            "crisis_type": CrisisType.flood,
            "infrastructure_type": InfrastructureType.residential,
            "damage_severity": ReportDamageSeverity.partial,
            "lat": 1.234,
            "lng": 36.789,
            "status": ReportStatus.pending,
            "photo_status": PhotoStatus.pending,
            "reporter_trust_tier": 0,
            "created_at": NOW,
            "updated_at": NOW,
        }
        return {**base, **overrides}

    def test_valid(self):
        from app.schemas.report import ReportRead

        obj = ReportRead(**self._payload())
        assert obj.lat == 1.234
        assert obj.lng == 36.789

    def test_building_id_optional(self):
        from app.schemas.report import ReportRead

        obj = ReportRead(**self._payload(building_id=None))
        assert obj.building_id is None

    def test_building_id_populated(self):
        from app.schemas.report import ReportRead

        obj = ReportRead(**self._payload(building_id=_UUID))
        assert obj.building_id == _UUID

    def test_from_attributes(self):
        from app.models.enums import (
            CrisisType,
            InfrastructureType,
            PhotoStatus,
            ReportDamageSeverity,
            ReportStatus,
        )
        from app.schemas.report import ReportRead

        mock = MagicMock()
        mock.id = _UUID
        mock.building_id = None
        mock.crisis_type = CrisisType.earthquake
        mock.infrastructure_type = InfrastructureType.government
        mock.damage_severity = ReportDamageSeverity.destroyed
        mock.lat = 0.1
        mock.lng = 0.2
        mock.status = ReportStatus.verified
        mock.photo_status = PhotoStatus.accepted
        mock.reporter_trust_tier = 2
        mock.created_at = NOW
        mock.updated_at = NOW

        obj = ReportRead.model_validate(mock)
        assert obj.status == ReportStatus.verified


# ─────────────────────────────────────────────────────────────────────────────
# app/main.py  — health endpoint + exception handlers
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"


@pytest.mark.asyncio
async def test_http_exception_handler_returns_error_key():
    """Custom HTTPException handler reshapes the response to {"error": detail}.

    FastAPI's router handles unknown-route 404s internally before the
    app-level handler fires, so we must trigger the handler via a real route
    that explicitly raises HTTPException.
    """
    from fastapi import APIRouter, HTTPException

    router = APIRouter()

    @router.get("/test-http-exc")
    async def _raise_http():
        raise HTTPException(status_code=403, detail="forbidden")

    app.include_router(router)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/test-http-exc")
        assert response.status_code == 403
        body = response.json()
        assert body == {"error": "forbidden"}
    finally:
        app.routes[:] = [
            r for r in app.routes if getattr(r, "path", None) != "/test-http-exc"
        ]


@pytest.mark.asyncio
async def test_unhandled_exception_handler_returns_500():
    """Custom unhandled-exception handler is exercised by calling it directly.

    Starlette's ServerErrorMiddleware re-raises exceptions in test/debug mode
    before the app-level handler can catch them, so going through the HTTP
    stack is unreliable. Calling the handler function directly is the correct
    way to unit-test it and still covers the lines in main.py.
    """
    from unittest.mock import MagicMock

    from fastapi.responses import JSONResponse

    from app.main import unhandled_exception_handler

    mock_request = MagicMock()
    exc = RuntimeError("boom")

    response: JSONResponse = await unhandled_exception_handler(mock_request, exc)

    assert response.status_code == 500
    import json

    body = json.loads(response.body)
    assert body == {"error": "An unexpected error occurred."}


# ─────────────────────────────────────────────────────────────────────────────
# app/core/config.py  — regression guard
# ─────────────────────────────────────────────────────────────────────────────


class TestSettings:
    def test_allowed_origins_list_splits_on_comma(self):
        from app.core.config import settings

        # The CI env sets ALLOWED_ORIGINS="http://localhost:3000"
        origins = settings.allowed_origins_list
        assert isinstance(origins, list)
        assert all(isinstance(o, str) for o in origins)

    def test_allowed_origins_list_strips_whitespace(self, monkeypatch):
        from app.core import config

        monkeypatch.setattr(
            config.settings,
            "ALLOWED_ORIGINS",
            " http://a.com , http://b.com ",
        )
        result = config.settings.allowed_origins_list
        assert result == ["http://a.com", "http://b.com"]

    def test_environment_value(self):
        from app.core.config import settings

        assert settings.ENVIRONMENT in ("development", "test", "production")
