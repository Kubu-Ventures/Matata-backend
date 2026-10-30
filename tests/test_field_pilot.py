"""Tests for the field pilot features.

Covers:
- Field reporter codes: code validation, tagging reports, the per-code
  payment summary (JSON and CSV) and its analyst endpoint.
- Fire as a crisis type.
- Medical help question: urgent reports go to the top of the queue, and the
  AI worker never lowers that priority.
- Nearest health facilities: OSM import parsing and the nearby query.
- Request more info: analyst asks, reporter answers, rules on who and when.

Service tests run against an in-memory SQLite database holding only the
tables they need.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.models.enums import ReportStatus


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - test shim
    return "JSON"


@pytest_asyncio.fixture
async def db():
    """Async SQLite session with the tables these features touch."""
    from sqlalchemy import event

    from app.db.base_class import Base
    from app.models.analyst_note import AnalystNote
    from app.models.audit_log import AuditLog
    from app.models.field_reporter import FieldReporter
    from app.models.health_facility import HealthFacility
    from app.models.report import Report

    engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def _add_pg_functions(dbapi_conn, _record):
        # audit_log ids use Postgres's gen_random_uuid() as server default.
        dbapi_conn.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(
                c,
                tables=[
                    Report.__table__,
                    FieldReporter.__table__,
                    HealthFacility.__table__,
                    AnalystNote.__table__,
                    AuditLog.__table__,
                ],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _report(**overrides):
    from app.models.report import Report

    fields = dict(
        id=uuid.uuid4(),
        crisis_type="flood",
        infrastructure_type="residential",
        damage_severity="partial",
        lat=-1.3133,
        lng=36.7870,
        status=ReportStatus.pending,
        reporter_token_hash="token-hash",
        reporter_trust_tier=0,
    )
    fields.update(overrides)
    return Report(**fields)


def _redis():
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    return redis


async def _create(db, **overrides):
    from app.services.moderation_service import MockModerationProvider
    from app.services.storage_service import MockStorageService
    from app.services.submission_service import create_report

    kwargs = dict(
        crisis_type="flood",
        infrastructure_type="residential",
        damage_severity="partial",
        lat=-1.3133,
        lng=36.7870,
        gps_accuracy_m=10.0,
        landmark_description=None,
        electricity_status=None,
        health_services_status=None,
        most_pressing_needs=None,
        debris_clearing_needed=None,
        offline_queued_at=None,
        image_bytes=None,
        reporter_token="some-subject",
        db=db,
        redis=_redis(),
        moderation_provider=MockModerationProvider(),
        storage_service=MockStorageService(),
    )
    kwargs.update(overrides)
    report = await create_report(**kwargs)
    await db.commit()
    return report


# ===========================================================================
# Field reporter codes
# ===========================================================================


class TestNormaliseCode:
    @pytest.mark.parametrize(
        "raw,expected",
        [("kib-01", "KIB-01"), (" MAT-3 ", "MAT-3"), ("MUK-10", "MUK-10")],
    )
    def test_valid(self, raw, expected):
        from app.services.field_reporter_service import normalise_code

        assert normalise_code(raw) == expected

    @pytest.mark.parametrize("raw", ["", "KIB01", "Jane Doe", "K-1", "KIB-ABC"])
    def test_invalid(self, raw):
        from app.services.field_reporter_service import normalise_code

        with pytest.raises(ValueError):
            normalise_code(raw)


class TestReportTagging:
    @pytest.mark.asyncio
    async def test_field_reporter_report_gets_code(self, db):
        from app.models.field_reporter import FieldReporter

        db.add(FieldReporter(code="KIB-01", email_hash="email-hash-1"))
        await db.commit()

        report = await _create(db, reporter_token="email-hash-1")
        assert report.field_reporter_code == "KIB-01"

    @pytest.mark.asyncio
    async def test_public_report_has_no_code(self, db):
        report = await _create(db, reporter_token="privy-did-hash")
        assert report.field_reporter_code is None

    @pytest.mark.asyncio
    async def test_inactive_reporter_not_tagged(self, db):
        from app.models.field_reporter import FieldReporter

        db.add(FieldReporter(code="KIB-02", email_hash="email-hash-2", is_active=False))
        await db.commit()

        report = await _create(db, reporter_token="email-hash-2")
        assert report.field_reporter_code is None


class TestPaymentSummary:
    @pytest.mark.asyncio
    async def test_counts_by_status_and_lists_idle_codes(self, db):
        from app.models.field_reporter import FieldReporter
        from app.services.field_reporter_service import (
            payment_summary,
            summary_to_csv,
        )

        db.add_all(
            [
                FieldReporter(code="KIB-01", email_hash="a"),
                FieldReporter(code="MAT-01", email_hash="b"),
            ]
        )
        for status in [
            ReportStatus.verified,
            ReportStatus.verified,
            ReportStatus.rejected,
            ReportStatus.duplicate,
            ReportStatus.pending,
            ReportStatus.pending_merge_review,
        ]:
            db.add(_report(field_reporter_code="KIB-01", status=status))
        db.add(_report())  # public report, not counted
        await db.commit()

        rows = await payment_summary(db)
        by_code = {r.code: r for r in rows}

        kib = by_code["KIB-01"]
        assert (kib.total, kib.verified, kib.rejected, kib.duplicate, kib.pending) == (
            6,
            2,
            1,
            1,
            2,
        )
        assert by_code["MAT-01"].total == 0

        csv_text = summary_to_csv(rows)
        assert csv_text.splitlines()[0] == (
            "code,total,verified,pending,rejected,duplicate"
        )
        assert "KIB-01,6,2,2,1,1" in csv_text

    @pytest.mark.asyncio
    async def test_date_range(self, db):
        from app.services.field_reporter_service import payment_summary

        now = datetime.now(timezone.utc)
        db.add(
            _report(field_reporter_code="KIB-01", created_at=now - timedelta(days=10))
        )
        db.add(_report(field_reporter_code="KIB-01", created_at=now))
        await db.commit()

        rows = await payment_summary(db, date_from=now - timedelta(days=1))
        assert rows[0].total == 1


# ===========================================================================
# Fire and medical help
# ===========================================================================


class TestFireAndMedicalHelp:
    @pytest.mark.asyncio
    async def test_fire_crisis_type_accepted(self, db):
        report = await _create(db, crisis_type="fire")
        assert getattr(report.crisis_type, "value", report.crisis_type) == "fire"

    def test_schema_accepts_fire_and_medical_help(self):
        from app.schemas.report_submission import ReportCreateSchema

        meta = ReportCreateSchema.model_validate(
            {
                "crisis_type": "fire",
                "infrastructure_type": "residential",
                "damage_severity": "destroyed",
                "lat": -1.31,
                "lng": 36.78,
                "medical_help_needed": "urgent",
            }
        )
        assert meta.crisis_type.value == "fire"
        assert meta.medical_help_needed is not None
        assert meta.medical_help_needed.value == "urgent"

    @pytest.mark.asyncio
    async def test_urgent_medical_help_is_critical(self, db):
        report = await _create(db, medical_help_needed="urgent")
        assert report.review_priority.value == "critical"

    @pytest.mark.asyncio
    async def test_non_urgent_stays_normal(self, db):
        report = await _create(db, medical_help_needed="not_urgent")
        assert report.review_priority.value == "normal"

    @pytest.mark.parametrize(
        "computed,medical,responded,expected",
        [
            ("low", "urgent", None, "critical"),
            ("high", "urgent", None, "critical"),
            ("low", None, "2026-11-01", "high"),
            ("critical", None, "2026-11-01", "critical"),
            ("low", "none", None, "low"),
            ("normal", None, None, "normal"),
        ],
    )
    def test_ai_never_lowers_priority(self, computed, medical, responded, expected):
        from app.workers.ai_tasks import _apply_priority_floor

        assert (
            _apply_priority_floor(
                computed, medical_help_needed=medical, info_responded_at=responded
            )
            == expected
        )


# ===========================================================================
# Health facilities
# ===========================================================================


class TestHealthFacilityImport:
    def test_overpass_parsing_skips_pharmacies(self):
        from app.cli.import_health_facilities import parse_features

        data = {
            "elements": [
                {
                    "type": "node",
                    "id": 1,
                    "lat": -1.31,
                    "lon": 36.78,
                    "tags": {"amenity": "clinic", "name": "Kibera Clinic"},
                },
                {
                    "type": "way",
                    "id": 2,
                    "center": {"lat": -1.30, "lon": 36.79},
                    "tags": {"healthcare": "centre", "phone": "+254700000000"},
                },
                {
                    "type": "node",
                    "id": 3,
                    "lat": -1.3,
                    "lon": 36.7,
                    "tags": {"amenity": "pharmacy"},
                },
            ]
        }
        rows = list(parse_features(data))
        assert [r["external_id"] for r in rows] == ["osm:node/1", "osm:way/2"]
        assert rows[0]["facility_type"] == "clinic"
        assert rows[1]["facility_type"] == "health_centre"
        assert rows[1]["phone"] == "+254700000000"

    def test_geojson_polygon_uses_centre(self):
        from app.cli.import_health_facilities import parse_features

        data = {
            "features": [
                {
                    "type": "Feature",
                    "id": "way/9",
                    "properties": {"amenity": "hospital", "name": "Mbagathi"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[36.0, -1.0], [36.2, -1.0], [36.2, -1.2]]],
                    },
                }
            ]
        }
        (row,) = list(parse_features(data))
        assert row["external_id"] == "osm:way/9"
        assert row["lat"] == pytest.approx(-1.0667, abs=1e-3)
        assert row["lng"] == pytest.approx(36.1333, abs=1e-3)


class TestNearestFacilities:
    @pytest.mark.asyncio
    async def test_nearest_first_within_radius(self, db):
        from app.models.health_facility import HealthFacility
        from app.services.health_facility_service import nearest_facilities

        lat, lng = -1.3133, 36.7870
        db.add_all(
            [
                HealthFacility(
                    external_id="osm:node/1",
                    name="Far",
                    facility_type="hospital",
                    lat=lat + 0.02,
                    lng=lng,
                ),
                HealthFacility(
                    external_id="osm:node/2",
                    name="Near",
                    facility_type="clinic",
                    lat=lat + 0.002,
                    lng=lng,
                ),
                HealthFacility(
                    external_id="osm:node/3",
                    name="Too far",
                    facility_type="hospital",
                    lat=lat + 0.2,
                    lng=lng,
                ),
            ]
        )
        await db.commit()

        found = await nearest_facilities(db, lat=lat, lng=lng, limit=3)
        assert [f.name for f in found] == ["Near", "Far"]
        assert found[0].distance_m == pytest.approx(222.6, abs=2)


# ===========================================================================
# Request more info
# ===========================================================================


class TestRequestMoreInfo:
    @pytest.mark.asyncio
    async def test_full_round_trip(self, db):
        import sqlalchemy as sa

        from app.models.analyst_note import AnalystNote
        from app.models.audit_log import AuditLog
        from app.services.analyst_service import request_more_info
        from app.services.submission_service import (
            _hash_token,
            add_reporter_followup,
        )

        report = _report(reporter_token_hash=_hash_token("reporter-sub"))
        db.add(report)
        await db.commit()

        await request_more_info(
            db,
            report.id,
            message="Please add a photo of the <b>roof</b>.",
            analyst_id_hash="analyst",
        )
        await db.commit()
        assert report.info_request_message == "Please add a photo of the roof."
        assert report.info_requested_at is not None

        updated = await add_reporter_followup(
            report_id=report.id,
            reporter_token="reporter-sub",
            text_body="The roof is gone on the left side.",
            db=db,
        )
        await db.commit()
        assert updated.reporter_followup == "The roof is gone on the left side."
        assert updated.info_responded_at is not None
        assert updated.review_priority.value == "high"
        # The original report fields are untouched.
        assert getattr(updated.damage_severity, "value", None) == "partial"

        notes = (await db.execute(sa.select(AnalystNote.body))).scalars().all()
        assert any(n.startswith("Asked the reporter") for n in notes)
        assert any(n.startswith("Reporter replied") for n in notes)
        ops = (await db.execute(sa.select(AuditLog.operation))).scalars().all()
        assert {"report.info_requested", "report.reporter_followup"} <= set(ops)

    @pytest.mark.asyncio
    async def test_cannot_answer_twice_or_without_request(self, db):
        from app.services.analyst_service import request_more_info
        from app.services.submission_service import (
            NoInfoRequestError,
            _hash_token,
            add_reporter_followup,
        )

        report = _report(reporter_token_hash=_hash_token("me"))
        db.add(report)
        await db.commit()

        with pytest.raises(NoInfoRequestError):
            await add_reporter_followup(
                report_id=report.id, reporter_token="me", text_body="hi", db=db
            )

        await request_more_info(
            db, report.id, message="Which street?", analyst_id_hash="a"
        )
        await add_reporter_followup(
            report_id=report.id, reporter_token="me", text_body="Olympic", db=db
        )
        with pytest.raises(NoInfoRequestError):
            await add_reporter_followup(
                report_id=report.id, reporter_token="me", text_body="again", db=db
            )

    @pytest.mark.asyncio
    async def test_other_reporter_cannot_answer(self, db):
        from app.services.analyst_service import request_more_info
        from app.services.submission_service import (
            ReportOwnershipError,
            _hash_token,
            add_reporter_followup,
        )

        report = _report(reporter_token_hash=_hash_token("owner"))
        db.add(report)
        await db.commit()
        await request_more_info(db, report.id, message="More?", analyst_id_hash="a")

        with pytest.raises(ReportOwnershipError):
            await add_reporter_followup(
                report_id=report.id, reporter_token="someone-else", text_body="x", db=db
            )

    @pytest.mark.asyncio
    async def test_decided_report_cannot_be_sent_back(self, db):
        from app.services.analyst_service import request_more_info

        report = _report(status=ReportStatus.verified)
        db.add(report)
        await db.commit()

        with pytest.raises(ValueError):
            await request_more_info(db, report.id, message="More?", analyst_id_hash="a")

    @pytest.mark.asyncio
    async def test_missing_report(self, db):
        from app.services.analyst_service import request_more_info

        with pytest.raises(LookupError):
            await request_more_info(
                db, uuid.uuid4(), message="More?", analyst_id_hash="a"
            )

    @pytest.mark.asyncio
    async def test_new_request_clears_previous_answer(self, db):
        from app.services.analyst_service import request_more_info
        from app.services.submission_service import _hash_token, add_reporter_followup

        report = _report(reporter_token_hash=_hash_token("me"))
        db.add(report)
        await db.commit()
        await request_more_info(db, report.id, message="One?", analyst_id_hash="a")
        await add_reporter_followup(
            report_id=report.id, reporter_token="me", text_body="Answer one", db=db
        )
        await request_more_info(db, report.id, message="Two?", analyst_id_hash="a")
        assert report.reporter_followup is None
        assert report.info_responded_at is None
        assert report.info_request_message == "Two?"


# ===========================================================================
# Routes
# ===========================================================================


def _app_with(user: dict, db_session):
    from app.api.v1.routes.auth import get_current_user
    from app.core.dependencies import get_db, get_redis
    from app.main import app

    async def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_redis] = lambda: _redis()
    app.dependency_overrides[get_current_user] = lambda: user
    return app


class TestRoutes:
    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        yield
        from app.main import app

        app.dependency_overrides.clear()

    def test_summary_requires_analyst(self):
        from fastapi.testclient import TestClient

        db = AsyncMock()
        app = _app_with({"sub": "x", "role": "reporter"}, db)
        resp = TestClient(app).get("/api/v1/analyst/field-reporters/summary")
        assert resp.status_code == 403

    def test_summary_csv(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.services import field_reporter_service

        monkeypatch.setattr(
            field_reporter_service,
            "payment_summary",
            AsyncMock(
                return_value=[
                    field_reporter_service.FieldReporterSummaryRow(
                        code="KIB-01", total=3, verified=2, rejected=1
                    )
                ]
            ),
        )
        app = _app_with({"sub": "x", "role": "analyst"}, AsyncMock())
        client = TestClient(app)

        resp = client.get("/api/v1/analyst/field-reporters/summary?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "KIB-01,3,2,0,1,0" in resp.text

        resp = client.get("/api/v1/analyst/field-reporters/summary")
        assert resp.json()["items"][0]["code"] == "KIB-01"

    def test_request_info_on_decided_report_is_409(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.services import analyst_service

        monkeypatch.setattr(
            analyst_service,
            "request_more_info",
            AsyncMock(side_effect=ValueError("already decided")),
        )
        app = _app_with({"sub": "x", "role": "analyst"}, AsyncMock())
        resp = TestClient(app).post(
            f"/api/v1/analyst/reports/{uuid.uuid4()}/request-info",
            json={"message": "Please add a photo."},
        )
        assert resp.status_code == 409

    def test_reporter_cannot_request_info(self):
        from fastapi.testclient import TestClient

        app = _app_with({"sub": "x", "role": "reporter"}, AsyncMock())
        resp = TestClient(app).post(
            f"/api/v1/analyst/reports/{uuid.uuid4()}/request-info",
            json={"message": "Please add a photo."},
        )
        assert resp.status_code == 403

    def test_additional_info_without_request_is_409(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.api.v1.routes import reports as reports_routes
        from app.services.submission_service import NoInfoRequestError

        monkeypatch.setattr(
            reports_routes,
            "add_reporter_followup",
            AsyncMock(side_effect=NoInfoRequestError("none")),
        )
        app = _app_with({"sub": "x", "role": "reporter"}, AsyncMock())
        resp = TestClient(app).post(
            f"/api/v1/reports/{uuid.uuid4()}/additional-info", json={"text": "hi"}
        )
        assert resp.status_code == 409

    def test_nearby_facilities(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.services import health_facility_service

        monkeypatch.setattr(
            health_facility_service,
            "nearest_facilities",
            AsyncMock(
                return_value=[
                    health_facility_service.NearbyFacility(
                        name="Kibera Clinic",
                        facility_type="clinic",
                        lat=-1.31,
                        lng=36.78,
                        distance_m=210.0,
                    )
                ]
            ),
        )
        app = _app_with({"sub": "x", "role": "reporter"}, AsyncMock())
        resp = TestClient(app).get(
            "/api/v1/gis/health-facilities/nearby?lat=-1.31&lng=36.78"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0]["name"] == "Kibera Clinic"
        assert "OpenStreetMap" in body["attribution"]


def test_field_reporter_code_not_in_public_export():
    """The code must never leave the analyst view."""
    import dataclasses

    from app.services import export_service

    assert "field_reporter_code" not in export_service.DBF_FIELD_MAP
    row_fields = {f.name for f in dataclasses.fields(export_service.ExportRecord)}
    assert "field_reporter_code" not in row_fields
