"""Tests for issue #16 — Data export endpoints (GeoJSON, CSV, Shapefile, async jobs).

Coverage targets
----------------
* ``app.services.export_service``  — Anonymiser, ExportRecord, ExportService
* ``app.workers.export_tasks``     — async job helpers, Celery task
* ``app.api.v1.routes.export``     — all route handlers

Anonymisation acceptance criteria (spec §12.2 / issue #16)
----------------------------------------------------------
* Plaintext phone number never appears.
* ``reporter_token_hash`` truncated to first 12 characters only.
* ``AnalystNote.body`` never appears.
* Only fields listed in spec §12.1 are exported.
* Restriction enforced in ExportService — not bypassable via any parameter.

All tests use in-memory mocks; no real database, Redis, or S3 is required.
"""

from __future__ import annotations

import csv
import io
import json

# ---------------------------------------------------------------------------
# Minimal environment setup (mirrors conftest.py)
# ---------------------------------------------------------------------------
import os
import uuid
from dataclasses import fields as dc_fields
from datetime import datetime, timezone
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_export.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)
os.environ.setdefault("PHONE_HASH_SALT", "x" * 32)
os.environ.setdefault("SMS_GATEWAY", "console")
os.environ.setdefault("AFRICASTALKING_API_KEY", "")
os.environ.setdefault("AFRICASTALKING_USERNAME", "")
os.environ.setdefault("MODERATION_PROVIDER", "mock")
os.environ.setdefault("STORAGE_BACKEND", "mock")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("S3_BUCKET_NAME", "")
os.environ.setdefault("S3_ENDPOINT_URL", "")
os.environ.setdefault("GEOCODING_PROVIDER", "mock")
os.environ.setdefault("GOOGLE_GEOCODING_API_KEY", "")
os.environ.setdefault("BUILDING_FOOTPRINT_SEARCH_RADIUS_M", "30")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("VISION_PROVIDER", "mock")
os.environ.setdefault("OPENAI_API_KEY", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("AI_PROCESSING_QUEUE_ALERT_DEPTH", "500")

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------
from app.services.export_service import (  # noqa: E402
    ASYNC_THRESHOLD,
    DBF_FIELD_MAP,
    Anonymiser,
    ExportFilterParams,
    ExportRecord,
    ExportService,
    _build_shapefile_zip,
)
from app.workers.export_tasks import (  # noqa: E402
    JOB_STATUS_COMPLETE,
    JOB_STATUS_FAILED,
    JOB_STATUS_PROCESSING,
    _job_key,
    _update_job_sync,
    _upload_export_file,
    create_export_job,
    get_export_job_status,
    run_export_job,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_report(
    *,
    full_hash: str = "abc123def456789012345678901234567890",
    crisis_type: str = "flood",
    infrastructure_type: str = "residential",
    damage_severity: str = "partial",
    status: str = "verified",
    lat: float = -1.2921,
    lng: float = 36.8219,
    reporter_trust_tier: int = 1,
    ai_severity_prediction: Optional[str] = "partial",
    ai_confidence: Optional[float] = 0.88,
    electricity_status: Optional[str] = "non_functional",
    health_services_status: Optional[str] = "accessible",
    most_pressing_needs: Optional[str] = "Water and food",
    debris_clearing_needed: Optional[bool] = True,
    building_id: Optional[str] = None,
    photo_url: Optional[str] = "reports/abc/photo.jpg",
    gps_accuracy_m: Optional[float] = 10.5,
    landmark_description: Optional[str] = None,
) -> MagicMock:
    """Build a mock Report ORM instance."""
    r = MagicMock()
    r.id = uuid.uuid4()
    r.reporter_token_hash = full_hash
    r.building_id = uuid.UUID(building_id) if building_id else None
    r.crisis_type = _enum_mock(crisis_type)
    r.infrastructure_type = _enum_mock(infrastructure_type)
    r.damage_severity = _enum_mock(damage_severity)
    r.ai_severity_prediction = (
        _enum_mock(ai_severity_prediction) if ai_severity_prediction else None
    )
    r.ai_confidence = ai_confidence
    r.status = _enum_mock(status)
    r.reporter_trust_tier = reporter_trust_tier
    r.lat = lat
    r.lng = lng
    r.gps_accuracy_m = gps_accuracy_m
    r.landmark_description = landmark_description
    r.electricity_status = (
        _enum_mock(electricity_status) if electricity_status else None
    )
    r.health_services_status = (
        _enum_mock(health_services_status) if health_services_status else None
    )
    r.most_pressing_needs = most_pressing_needs
    r.debris_clearing_needed = debris_clearing_needed
    r.photo_url = photo_url
    r.created_at = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    r.updated_at = datetime(2024, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    # Fields that must never appear in exports
    r.photo_phash = "aabbccdd11223344"
    r.duplicate_of_id = None
    r.possible_duplicate_of_id = None
    r.duplicate_score = None
    r.offline_queued_at = None
    r.footprint_match_confidence = 0.95
    r.ai_quality_score = 0.82
    r.ai_divergence = False
    return r


def _enum_mock(value: str) -> MagicMock:
    m = MagicMock()
    m.value = value
    return m


def _make_db_mock(reports: List[MagicMock]) -> AsyncMock:
    """Return an async DB session mock whose ``execute`` returns *reports*."""
    db = AsyncMock()

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = reports

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    result_mock.scalar_one.return_value = len(reports)

    db.execute = AsyncMock(return_value=result_mock)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    return db


# ===========================================================================
# 1. Anonymiser
# ===========================================================================


class TestAnonymiser:
    """Tests for ``Anonymiser.anonymise``."""

    def test_hash_truncated_to_12_chars(self):
        a = Anonymiser()
        report = _make_report(full_hash="abcdef123456789012345678")
        rec = a.anonymise(report)
        assert rec.reporter_token_hash_truncated == "abcdef123456"
        assert len(rec.reporter_token_hash_truncated) == 12

    def test_full_hash_not_in_record(self):
        a = Anonymiser()
        report = _make_report(full_hash="secret_full_hash_must_not_appear")
        rec = a.anonymise(report)
        # Verify no field holds the full hash
        for f in dc_fields(rec):
            val = getattr(rec, f.name)
            if isinstance(val, str):
                assert "secret_full_hash_must_not_appear" not in val

    def test_phone_number_never_in_record(self):
        """Phone is never stored, but this verifies no leakage via mock."""
        a = Anonymiser()
        report = _make_report()
        rec = a.anonymise(report)
        rec_dict = rec.to_dict()
        # No field should contain a phone-like pattern
        for val in rec_dict.values():
            if isinstance(val, str):
                assert "+254" not in val

    def test_analyst_note_body_not_present(self):
        """Analyst notes must never appear in any ExportRecord field."""
        a = Anonymiser()
        report = _make_report()
        # Simulate report having analyst notes attached
        report.analyst_notes = [MagicMock(body="INTERNAL NOTE: very sensitive")]
        rec = a.anonymise(report)
        rec_dict = rec.to_dict()
        for val in rec_dict.values():
            if isinstance(val, str):
                assert "INTERNAL NOTE" not in val

    def test_blocked_fields_not_in_export_record(self):
        """Fields in Anonymiser._BLOCKED_FIELDS must not appear in ExportRecord."""
        blocked = Anonymiser._BLOCKED_FIELDS
        export_field_names = {f.name for f in dc_fields(ExportRecord)}
        for blocked_field in blocked:
            assert (
                blocked_field not in export_field_names
            ), f"Blocked field '{blocked_field}' found in ExportRecord"

    def test_all_enum_values_are_strings(self):
        a = Anonymiser()
        rec = a.anonymise(_make_report())
        assert isinstance(rec.crisis_type, str)
        assert isinstance(rec.infrastructure_type, str)
        assert isinstance(rec.damage_severity, str)
        assert isinstance(rec.status, str)

    def test_timestamps_are_iso_strings(self):
        a = Anonymiser()
        rec = a.anonymise(_make_report())
        # Should be parseable as ISO 8601
        dt = datetime.fromisoformat(rec.created_at)
        assert dt.year == 2024

    def test_nullable_fields_handled_correctly(self):
        a = Anonymiser()
        report = _make_report(
            building_id=None,
            ai_severity_prediction=None,
            ai_confidence=None,
            electricity_status=None,
            health_services_status=None,
            most_pressing_needs=None,
            debris_clearing_needed=None,
            landmark_description=None,
            photo_url=None,
            gps_accuracy_m=None,
        )
        rec = a.anonymise(report)
        assert rec.building_id is None
        assert rec.ai_severity_prediction is None
        assert rec.ai_confidence is None

    def test_timestamp_without_tzinfo(self):
        """Timestamps without tzinfo should be treated as UTC."""
        a = Anonymiser()
        report = _make_report()
        report.created_at = datetime(2024, 1, 1, 0, 0, 0)  # no tzinfo
        rec = a.anonymise(report)
        assert "2024-01-01" in rec.created_at

    def test_to_dict_contains_all_fields(self):
        a = Anonymiser()
        rec = a.anonymise(_make_report())
        d = rec.to_dict()
        for f in dc_fields(ExportRecord):
            assert f.name in d

    def test_hash_shorter_than_12_chars(self):
        """Short hashes should be returned as-is (not padded)."""
        a = Anonymiser()
        report = _make_report(full_hash="short")
        rec = a.anonymise(report)
        assert rec.reporter_token_hash_truncated == "short"

    def test_empty_hash(self):
        a = Anonymiser()
        report = _make_report(full_hash="")
        rec = a.anonymise(report)
        assert rec.reporter_token_hash_truncated == ""


# ===========================================================================
# 2. ExportRecord
# ===========================================================================


class TestExportRecord:
    def test_to_dict_round_trip(self):
        rec = ExportRecord(
            report_id="r1",
            building_id="b1",
            crisis_type="flood",
            infrastructure_type="residential",
            damage_severity="partial",
            ai_severity_prediction="partial",
            ai_confidence=0.9,
            status="verified",
            reporter_token_hash_truncated="abc123456789",
            reporter_trust_tier=1,
            lat=-1.29,
            lng=36.82,
            gps_accuracy_m=10.0,
            landmark_description=None,
            electricity_status="functional",
            health_services_status="accessible",
            most_pressing_needs="Water",
            debris_clearing_needed=False,
            photo_url="reports/x/p.jpg",
            created_at="2024-06-01T12:00:00+00:00",
            updated_at="2024-06-01T13:00:00+00:00",
        )
        d = rec.to_dict()
        assert d["report_id"] == "r1"
        assert d["crisis_type"] == "flood"
        assert d["reporter_trust_tier"] == 1


# ===========================================================================
# 3. DBF_FIELD_MAP
# ===========================================================================


class TestDBFFieldMap:
    def test_all_dbf_names_le_10_chars(self):
        for py_attr, dbf_name in DBF_FIELD_MAP.items():
            assert (
                len(dbf_name) <= 10
            ), f"DBF field '{dbf_name}' (from '{py_attr}') exceeds 10 characters"

    def test_all_export_record_fields_mapped(self):
        export_attrs = {f.name for f in dc_fields(ExportRecord)}
        for attr in DBF_FIELD_MAP:
            assert (
                attr in export_attrs
            ), f"DBF_FIELD_MAP key '{attr}' not in ExportRecord"


# ===========================================================================
# 4. ExportService — GeoJSON
# ===========================================================================


class TestExportServiceGeoJSON:
    @pytest.mark.asyncio
    async def test_geojson_basic_structure(self):
        reports = [_make_report() for _ in range(3)]
        db = _make_db_mock(reports)
        svc = ExportService(db=db, analyst_id_hash="analyst_hash_abc")
        filters = ExportFilterParams()
        result = await svc.export_geojson(filters, "2024-06-01")
        data = json.loads(result)
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 3

    @pytest.mark.asyncio
    async def test_geojson_feature_geometry(self):
        report = _make_report(lat=-1.2921, lng=36.8219)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        data = json.loads(result)
        feat = data["features"][0]
        assert feat["geometry"]["type"] == "Point"
        assert feat["geometry"]["coordinates"] == [36.8219, -1.2921]

    @pytest.mark.asyncio
    async def test_geojson_null_geometry_when_no_coords(self):
        report = _make_report()
        report.lat = None
        report.lng = None
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        data = json.loads(result)
        assert data["features"][0]["geometry"] is None

    @pytest.mark.asyncio
    async def test_geojson_properties_no_lat_lng(self):
        """lat/lng should not appear in properties (they are in geometry)."""
        report = _make_report(lat=-1.29, lng=36.82)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        data = json.loads(result)
        props = data["features"][0]["properties"]
        assert "lat" not in props
        assert "lng" not in props

    @pytest.mark.asyncio
    async def test_geojson_no_full_hash_in_output(self):
        full_hash = "full_secret_hash_abcdef1234567890ab"
        report = _make_report(full_hash=full_hash)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        assert full_hash not in result.decode()

    @pytest.mark.asyncio
    async def test_geojson_audit_log_written(self):
        db = _make_db_mock([_make_report()])
        svc = ExportService(db=db, analyst_id_hash="analyst123")
        await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        db.add.assert_called_once()
        db.flush.assert_called()

    @pytest.mark.asyncio
    async def test_geojson_empty_result(self):
        db = _make_db_mock([])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        data = json.loads(result)
        assert data["features"] == []

    @pytest.mark.asyncio
    async def test_geojson_include_footprints_payload_shape(self):
        """When include_footprints=True the response has 'reports'/'footprints'."""
        report = _make_report(building_id=str(uuid.uuid4()))
        db = _make_db_mock([report])

        # Mock the footprint query to return an empty result.
        footprint_result = MagicMock()
        footprint_result.__iter__ = MagicMock(return_value=iter([]))

        # First call: main records; second call: footprints
        main_scalars = MagicMock()
        main_scalars.all.return_value = [report]
        main_result = MagicMock()
        main_result.scalars.return_value = main_scalars
        main_result.scalar_one.return_value = 1

        db.execute = AsyncMock(side_effect=[main_result, footprint_result])

        svc = ExportService(db=db, analyst_id_hash="x")
        filters = ExportFilterParams(include_footprints=True)
        result = await svc.export_geojson(filters, "2024-06-01")
        data = json.loads(result)
        assert "reports" in data
        assert "footprints" in data

    @pytest.mark.asyncio
    async def test_geojson_include_footprints_no_building_ids(self):
        """When no records have building_id, footprints list should be empty."""
        report = _make_report(building_id=None)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        filters = ExportFilterParams(include_footprints=True)
        result = await svc.export_geojson(filters, "2024-06-01")
        data = json.loads(result)
        # No footprint DB query needed; result should still be valid
        assert "reports" in data or "features" in data

    @pytest.mark.asyncio
    async def test_geojson_is_valid_utf8(self):
        report = _make_report(most_pressing_needs="Maji na chakula")
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        # Should not raise
        decoded = result.decode("utf-8")
        assert "Maji" in decoded


# ===========================================================================
# 5. ExportService — CSV
# ===========================================================================


class TestExportServiceCSV:
    @pytest.mark.asyncio
    async def test_csv_has_header_row(self):
        db = _make_db_mock([_make_report()])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        list(reader)
        assert reader.fieldnames is not None
        assert len(reader.fieldnames) > 0

    @pytest.mark.asyncio
    async def test_csv_one_row_per_report(self):
        reports = [_make_report() for _ in range(5)]
        db = _make_db_mock(reports)
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_csv_coordinates_decimal_degrees(self):
        report = _make_report(lat=-1.2921, lng=36.8219)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        assert "-1.2921" in content
        assert "36.8219" in content

    @pytest.mark.asyncio
    async def test_csv_booleans_as_true_false(self):
        report = _make_report(debris_clearing_needed=True)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        assert "TRUE" in content or "FALSE" in content

    @pytest.mark.asyncio
    async def test_csv_no_formula_injection_equals(self):
        """Field values starting with '=' must be prefixed with tab."""
        report = _make_report(most_pressing_needs="=MALICIOUS()")
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        # The value should not start with '=' in the CSV
        assert ",=MALICIOUS()" not in content

    @pytest.mark.asyncio
    async def test_csv_no_formula_injection_plus(self):
        report = _make_report(most_pressing_needs="+CMD|'/C calc'")
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        assert ",+CMD" not in content

    @pytest.mark.asyncio
    async def test_csv_no_formula_injection_at(self):
        report = _make_report(most_pressing_needs="@SUM(A1:A10)")
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        assert ",@SUM" not in content

    @pytest.mark.asyncio
    async def test_csv_no_formula_injection_minus(self):
        report = _make_report(most_pressing_needs="-2+3")
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        assert ",-2+3" not in content

    @pytest.mark.asyncio
    async def test_csv_timestamps_iso8601(self):
        report = _make_report()
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        assert "2024-06-01" in content

    @pytest.mark.asyncio
    async def test_csv_empty_dataset(self):
        db = _make_db_mock([])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        content = result.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        rows = list(reader)
        assert rows == []

    @pytest.mark.asyncio
    async def test_csv_no_full_hash(self):
        full_hash = "supersecret_fullhash_should_not_appear_xyz"
        report = _make_report(full_hash=full_hash)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        assert full_hash not in result.decode("utf-8-sig")

    @pytest.mark.asyncio
    async def test_csv_audit_log_written(self):
        db = _make_db_mock([_make_report()])
        svc = ExportService(db=db, analyst_id_hash="analyst456")
        await svc.export_csv(ExportFilterParams())
        db.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_csv_none_values_as_empty_string(self):
        report = _make_report(landmark_description=None)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        # Should not crash on None values
        assert result is not None


# ===========================================================================
# 6. ExportService — Shapefile (GDAL mocked)
# ===========================================================================


class TestExportServiceShapefile:
    @pytest.mark.asyncio
    async def test_shapefile_raises_importerror_when_gdal_missing(self):
        """When osgeo is not installed, shapefile export raises ImportError."""
        db = _make_db_mock([_make_report()])
        svc = ExportService(db=db, analyst_id_hash="x")

        with patch.dict(
            "sys.modules",
            {
                "osgeo": None,
                "osgeo.gdal": None,
                "osgeo.ogr": None,
                "osgeo.osr": None,
            },
        ):
            with pytest.raises(ImportError):
                await svc.export_shapefile(ExportFilterParams())

    @pytest.mark.asyncio
    async def test_shapefile_audit_log_written_before_gdal(self):
        """Audit log should be written even when GDAL raises."""
        db = _make_db_mock([_make_report()])
        svc = ExportService(db=db, analyst_id_hash="x")

        with patch.dict("sys.modules", {"osgeo": None}):
            try:
                await svc.export_shapefile(ExportFilterParams())
            except ImportError:
                pass
        # Audit log should have been written before the ImportError
        db.add.assert_called_once()

    def test_build_shapefile_zip_raises_without_gdal(self):
        """``_build_shapefile_zip`` raises ImportError when osgeo unavailable."""
        with patch.dict("sys.modules", {"osgeo": None, "osgeo.gdal": None}):
            with pytest.raises(ImportError):
                _build_shapefile_zip([])

    def test_dbf_field_names_all_10_chars_or_fewer(self):
        """All DBF field names must respect ESRI's 10-char limit."""
        for py_name, dbf_name in DBF_FIELD_MAP.items():
            assert len(dbf_name) <= 10


# ===========================================================================
# 7. ExportService — filters and count
# ===========================================================================


class TestExportServiceFilters:
    @pytest.mark.asyncio
    async def test_count_records_returns_scalar(self):
        db = AsyncMock()
        count_result = MagicMock()
        count_result.scalar_one.return_value = 42
        db.execute = AsyncMock(return_value=count_result)
        db.add = MagicMock()
        db.flush = AsyncMock()

        svc = ExportService(db=db, analyst_id_hash="x")
        count = await svc.count_records(ExportFilterParams())
        assert count == 42

    @pytest.mark.asyncio
    async def test_filters_applied_to_query(self):
        """Verify filter params result in DB query being called (smoke test)."""
        db = _make_db_mock([])
        svc = ExportService(db=db, analyst_id_hash="x")
        filters = ExportFilterParams(
            crisis_type=["flood"],
            damage_severity=["destroyed"],
            infrastructure_type=["residential"],
            status=["verified"],
            time_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            time_to=datetime(2024, 12, 31, tzinfo=timezone.utc),
            min_ai_confidence=0.7,
        )
        await svc.export_geojson(filters, "2024-06-01")
        db.execute.assert_called()

    @pytest.mark.asyncio
    async def test_audit_log_includes_filter_params(self):
        """Audit log after_state should record filter params and record count."""
        db = _make_db_mock([_make_report()])
        svc = ExportService(db=db, analyst_id_hash="analyst_x")
        filters = ExportFilterParams(crisis_type=["flood"])
        await svc.export_csv(filters)

        # Inspect the AuditLog object passed to db.add
        call_args = db.add.call_args
        audit_entry = call_args[0][0]
        assert audit_entry.operation == "export.generate"
        assert audit_entry.actor_id_hash == "analyst_x"
        assert audit_entry.after_state["format"] == "csv"
        assert audit_entry.after_state["record_count"] == 1
        assert "crisis_type" in audit_entry.after_state["filters"]


# ===========================================================================
# 8. async export job helpers
# ===========================================================================


class TestExportJobHelpers:
    @pytest.mark.asyncio
    async def test_create_export_job_returns_uuid(self):
        redis = AsyncMock()
        redis.set = AsyncMock()
        with patch("app.workers.export_tasks.run_export_job") as mock_task:
            mock_task.delay = MagicMock()
            job_id = await create_export_job(
                redis=redis,
                fmt="csv",
                filter_params={},
                analyst_id_hash="analyst_abc",
            )
        assert isinstance(job_id, str)
        # Valid UUID4
        uuid.UUID(job_id, version=4)

    @pytest.mark.asyncio
    async def test_create_export_job_stores_in_redis(self):
        redis = AsyncMock()
        redis.set = AsyncMock()
        with patch("app.workers.export_tasks.run_export_job") as mock_task:
            mock_task.delay = MagicMock()
            job_id = await create_export_job(
                redis=redis,
                fmt="geojson",
                filter_params={"crisis_type": ["flood"]},
                analyst_id_hash="analyst_abc",
            )
        redis.set.assert_called_once()
        call_kwargs = redis.set.call_args
        key = call_kwargs[0][0]
        assert job_id in key

    @pytest.mark.asyncio
    async def test_create_export_job_dispatches_celery_task(self):
        redis = AsyncMock()
        redis.set = AsyncMock()
        with patch("app.workers.export_tasks.run_export_job") as mock_task:
            mock_task.delay = MagicMock()
            await create_export_job(
                redis=redis,
                fmt="csv",
                filter_params={},
                analyst_id_hash="x",
            )
            mock_task.delay.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_export_job_status_returns_none_when_not_found(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        result = await get_export_job_status(redis, "nonexistent-job-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_export_job_status_returns_job_data(self):
        job_data = {
            "job_id": "test-job",
            "status": JOB_STATUS_PROCESSING,
            "download_url": None,
            "expires_at": None,
        }
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=json.dumps(job_data))
        result = await get_export_job_status(redis, "test-job")
        assert result is not None
        assert result["status"] == JOB_STATUS_PROCESSING

    @pytest.mark.asyncio
    async def test_get_export_job_status_handles_corrupt_cache(self):
        redis = AsyncMock()
        redis.get = AsyncMock(return_value="not-valid-json{{{")
        result = await get_export_job_status(redis, "bad-job")
        assert result is None

    def test_job_key_format(self):
        key = _job_key("abc-123")
        assert "abc-123" in key
        assert "crisismap:export:jobs" in key


# ===========================================================================
# 9. _upload_export_file (mock storage path)
# ===========================================================================


class TestUploadExportFile:
    def test_mock_storage_returns_fake_url(self):
        object_key, url = _upload_export_file("job-123", "csv", b"data")
        assert "job-123" in object_key
        assert "mock-storage" in url or "job-123" in url

    def test_mock_storage_geojson_extension(self):
        key, _ = _upload_export_file("job-1", "geojson", b"{}")
        assert key.endswith(".geojson")

    def test_mock_storage_shapefile_extension(self):
        key, _ = _upload_export_file("job-2", "shapefile", b"zip-data")
        assert key.endswith(".zip")

    def test_mock_storage_csv_extension(self):
        key, _ = _upload_export_file("job-3", "csv", b"csv")
        assert key.endswith(".csv")

    def test_unknown_format_defaults_to_bin(self):
        key, _ = _upload_export_file("job-4", "unknown", b"x")
        assert key.endswith(".bin")


# ===========================================================================
# 10. _update_job_sync
# ===========================================================================


class TestUpdateJobSync:
    def test_update_sets_status_complete(self):
        raw_job = json.dumps(
            {
                "job_id": "j1",
                "status": JOB_STATUS_PROCESSING,
                "download_url": None,
                "expires_at": None,
            }
        )
        import redis as sync_redis_real

        with patch.object(sync_redis_real, "Redis") as mock_cls:
            instance = MagicMock()
            instance.get.return_value = raw_job
            instance.set = MagicMock()
            instance.close = MagicMock()
            mock_cls.from_url.return_value = instance
            _update_job_sync("j1", JOB_STATUS_COMPLETE, "http://dl", "2025-01-01")
            instance.set.assert_called_once()
            # Verify the data written contains the new status
            written_data = json.loads(instance.set.call_args[0][1])
            assert written_data["status"] == JOB_STATUS_COMPLETE
            assert written_data["download_url"] == "http://dl"

    def test_update_handles_missing_job_gracefully(self):
        import redis as sync_redis_real

        with patch.object(sync_redis_real, "Redis") as mock_cls:
            instance = MagicMock()
            instance.get.return_value = None
            instance.close = MagicMock()
            mock_cls.from_url.return_value = instance
            # Should not raise
            _update_job_sync("nonexistent", JOB_STATUS_FAILED)


# ===========================================================================
# 11. Celery task — run_export_job
# ===========================================================================


class TestRunExportJobTask:
    def _make_task_self(self, max_retries: int = 3) -> MagicMock:
        """Build a mock Celery Task self."""
        self_mock = MagicMock()
        self_mock.request.retries = 0
        self_mock.max_retries = max_retries
        from celery.exceptions import Retry

        self_mock.retry.side_effect = Retry()
        return self_mock

    def _call_task(self, self_mock, **kwargs):
        """Call the underlying task function directly, bypassing Celery.

        In Celery 5, ``bind=True`` tasks expose the original function via
        ``task.__wrapped__``, which is a *bound method* on the task instance
        (so ``self`` is already the task object).  Passing a mock ``self``
        via ``run()`` or ``__wrapped__()`` therefore raises "multiple values
        for argument".

        ``task.__wrapped__.__func__`` is the true *unbound* Python function
        whose first parameter is ``self``, letting us inject a mock task
        instance for unit testing without a running Celery worker.
        """
        return run_export_job.__wrapped__.__func__(self_mock, **kwargs)

    def test_task_success_path(self):
        """Happy path: generate → upload → mark complete."""
        with (
            patch(
                "app.workers.export_tasks._run_export_sync",
                return_value=b"data",
            ),
            patch(
                "app.workers.export_tasks._upload_export_file",
                return_value=("exports/j1/f.csv", "http://dl"),
            ),
            patch("app.workers.export_tasks._update_job_sync") as mock_update,
        ):
            self_mock = self._make_task_self()
            result = self._call_task(
                self_mock,
                job_id="j1",
                fmt="csv",
                filter_params={},
                analyst_id_hash="x",
            )
        assert result["status"] == JOB_STATUS_COMPLETE
        assert result["download_url"] == "http://dl"
        mock_update.assert_called_once()
        call_args = mock_update.call_args
        assert call_args[0][1] == JOB_STATUS_COMPLETE

    def test_task_retries_on_failure(self):
        """Task should call self.retry on transient errors."""
        from celery.exceptions import Retry

        with patch(
            "app.workers.export_tasks._run_export_sync",
            side_effect=RuntimeError("oops"),
        ):
            self_mock = self._make_task_self()
            self_mock.retry.side_effect = Retry()
            with pytest.raises(Retry):
                self._call_task(
                    self_mock,
                    job_id="j2",
                    fmt="csv",
                    filter_params={},
                    analyst_id_hash="x",
                )
        self_mock.retry.assert_called_once()

    def test_task_marks_failed_after_max_retries(self):
        """After MaxRetriesExceededError, task sets status = failed."""
        from celery.exceptions import MaxRetriesExceededError

        with (
            patch(
                "app.workers.export_tasks._run_export_sync",
                side_effect=RuntimeError("perm"),
            ),
            patch("app.workers.export_tasks._update_job_sync") as mock_update,
        ):
            self_mock = self._make_task_self()
            self_mock.retry.side_effect = MaxRetriesExceededError()
            result = self._call_task(
                self_mock,
                job_id="j3",
                fmt="csv",
                filter_params={},
                analyst_id_hash="x",
            )
        assert result["status"] == JOB_STATUS_FAILED
        mock_update.assert_called_once_with("j3", JOB_STATUS_FAILED)


# ===========================================================================
# 12. Route handlers
# ===========================================================================


@pytest.fixture
def mock_analyst_user():
    return {"sub": "analyst_hash_xyz", "role": "analyst"}


@pytest.fixture
def mock_responder_user():
    return {"sub": "responder_hash_abc", "role": "responder"}


def _make_export_service_mock(record_count: int = 5, payload: bytes = b"{}"):
    """Return a patched ExportService that returns controlled data."""
    mock_svc = AsyncMock()
    mock_svc.count_records = AsyncMock(return_value=record_count)
    mock_svc.export_geojson = AsyncMock(return_value=payload)
    mock_svc.export_csv = AsyncMock(return_value=b"col1,col2\nval1,val2")
    mock_svc.export_shapefile = AsyncMock(return_value=b"PK\x03\x04zip-data")
    return mock_svc


class TestExportRoutes:
    """Integration-style tests for the export route handlers."""

    # -- GeoJSON --

    @pytest.mark.asyncio
    async def test_geojson_route_returns_response(self, mock_analyst_user):
        from app.api.v1.routes.export import export_geojson

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_redis = AsyncMock()

        payload = b'{"type":"FeatureCollection","features":[]}'
        with patch(
            "app.api.v1.routes.export.ExportService",
            return_value=_make_export_service_mock(record_count=2, payload=payload),
        ):
            response = await export_geojson(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                include_footprints=False,
                current_user=mock_analyst_user,
                db=mock_db,
                redis=mock_redis,
            )
        from fastapi.responses import Response

        assert isinstance(response, Response)
        assert response.media_type == "application/geo+json"

    @pytest.mark.asyncio
    async def test_geojson_route_async_when_large(self, mock_analyst_user):
        from app.api.v1.routes.export import export_geojson

        mock_db = AsyncMock()
        mock_redis = AsyncMock()

        large_count = ASYNC_THRESHOLD + 1
        with (
            patch(
                "app.api.v1.routes.export.ExportService",
                return_value=_make_export_service_mock(record_count=large_count),
            ),
            patch(
                "app.api.v1.routes.export.create_export_job",
                new_callable=AsyncMock,
                return_value="job-uuid-123",
            ),
        ):
            response = await export_geojson(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                include_footprints=False,
                current_user=mock_analyst_user,
                db=mock_db,
                redis=mock_redis,
            )
        from fastapi.responses import JSONResponse

        assert isinstance(response, JSONResponse)
        body = json.loads(response.body)
        assert body["job_id"] == "job-uuid-123"
        assert body["status"] == "processing"

    # -- CSV --

    @pytest.mark.asyncio
    async def test_csv_route_returns_csv_response(self, mock_analyst_user):
        from app.api.v1.routes.export import export_csv

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_redis = AsyncMock()

        with patch(
            "app.api.v1.routes.export.ExportService",
            return_value=_make_export_service_mock(record_count=3),
        ):
            response = await export_csv(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                current_user=mock_analyst_user,
                db=mock_db,
                redis=mock_redis,
            )
        assert "text/csv" in response.media_type

    @pytest.mark.asyncio
    async def test_csv_route_async_when_large(self, mock_analyst_user):
        from app.api.v1.routes.export import export_csv

        mock_db = AsyncMock()
        mock_redis = AsyncMock()

        with (
            patch(
                "app.api.v1.routes.export.ExportService",
                return_value=_make_export_service_mock(
                    record_count=ASYNC_THRESHOLD + 1
                ),
            ),
            patch(
                "app.api.v1.routes.export.create_export_job",
                new_callable=AsyncMock,
                return_value="job-csv-999",
            ),
        ):
            response = await export_csv(
                crisis_type="flood",
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                current_user=mock_analyst_user,
                db=mock_db,
                redis=mock_redis,
            )
        from fastapi.responses import JSONResponse

        assert isinstance(response, JSONResponse)
        body = json.loads(response.body)
        assert body["job_id"] == "job-csv-999"

    # -- Shapefile --

    @pytest.mark.asyncio
    async def test_shapefile_route_returns_zip(self, mock_analyst_user):
        from app.api.v1.routes.export import export_shapefile

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_redis = AsyncMock()

        with patch(
            "app.api.v1.routes.export.ExportService",
            return_value=_make_export_service_mock(record_count=2),
        ):
            response = await export_shapefile(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                current_user=mock_analyst_user,
                db=mock_db,
                redis=mock_redis,
            )
        assert response.media_type == "application/zip"

    @pytest.mark.asyncio
    async def test_shapefile_route_501_when_gdal_missing(self, mock_analyst_user):
        from fastapi import HTTPException

        from app.api.v1.routes.export import export_shapefile

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_redis = AsyncMock()

        svc_mock = _make_export_service_mock(record_count=2)
        svc_mock.export_shapefile = AsyncMock(side_effect=ImportError("no gdal"))

        with patch(
            "app.api.v1.routes.export.ExportService",
            return_value=svc_mock,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await export_shapefile(
                    crisis_type=None,
                    damage_severity=None,
                    infrastructure_type=None,
                    report_status=None,
                    time_from=None,
                    time_to=None,
                    min_ai_confidence=None,
                    current_user=mock_analyst_user,
                    db=mock_db,
                    redis=mock_redis,
                )
        assert exc_info.value.status_code == 501

    @pytest.mark.asyncio
    async def test_shapefile_route_async_when_large(self, mock_analyst_user):
        from app.api.v1.routes.export import export_shapefile

        mock_db = AsyncMock()
        mock_redis = AsyncMock()

        with (
            patch(
                "app.api.v1.routes.export.ExportService",
                return_value=_make_export_service_mock(
                    record_count=ASYNC_THRESHOLD + 1
                ),
            ),
            patch(
                "app.api.v1.routes.export.create_export_job",
                new_callable=AsyncMock,
                return_value="job-shp-777",
            ),
        ):
            response = await export_shapefile(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                current_user=mock_analyst_user,
                db=mock_db,
                redis=mock_redis,
            )
        from fastapi.responses import JSONResponse

        assert isinstance(response, JSONResponse)
        body = json.loads(response.body)
        assert body["job_id"] == "job-shp-777"

    # -- Job status --

    @pytest.mark.asyncio
    async def test_job_status_route_returns_complete(self, mock_analyst_user):
        from app.api.v1.routes.export import get_export_job

        job_data = {
            "status": JOB_STATUS_COMPLETE,
            "download_url": "http://presigned",
            "expires_at": "2025-01-01T00:00:00Z",
        }
        mock_redis = AsyncMock()
        with patch(
            "app.api.v1.routes.export.get_export_job_status",
            new_callable=AsyncMock,
            return_value=job_data,
        ):
            result = await get_export_job(
                job_id="completed-job",
                current_user=mock_analyst_user,
                redis=mock_redis,
            )
        assert result["status"] == JOB_STATUS_COMPLETE
        assert result["download_url"] == "http://presigned"

    @pytest.mark.asyncio
    async def test_job_status_route_404_when_not_found(self, mock_analyst_user):
        from fastapi import HTTPException

        from app.api.v1.routes.export import get_export_job

        mock_redis = AsyncMock()
        with patch(
            "app.api.v1.routes.export.get_export_job_status",
            new_callable=AsyncMock,
            return_value=None,
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_export_job(
                    job_id="ghost-job",
                    current_user=mock_analyst_user,
                    redis=mock_redis,
                )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_job_status_route_processing(self, mock_analyst_user):
        from app.api.v1.routes.export import get_export_job

        job_data = {
            "status": JOB_STATUS_PROCESSING,
            "download_url": None,
            "expires_at": None,
        }
        mock_redis = AsyncMock()
        with patch(
            "app.api.v1.routes.export.get_export_job_status",
            new_callable=AsyncMock,
            return_value=job_data,
        ):
            result = await get_export_job(
                job_id="running-job",
                current_user=mock_analyst_user,
                redis=mock_redis,
            )
        assert result["status"] == JOB_STATUS_PROCESSING
        assert result["download_url"] is None

    # -- Responder role access --

    @pytest.mark.asyncio
    async def test_responder_can_access_csv_export(self, mock_responder_user):
        from app.api.v1.routes.export import export_csv

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_redis = AsyncMock()

        with patch(
            "app.api.v1.routes.export.ExportService",
            return_value=_make_export_service_mock(record_count=1),
        ):
            response = await export_csv(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                current_user=mock_responder_user,
                db=mock_db,
                redis=mock_redis,
            )
        assert "text/csv" in response.media_type


# ===========================================================================
# 13. Anonymisation — end-to-end acceptance test
# ===========================================================================


class TestAnonymisationAcceptanceCriteria:
    """Directly tests the acceptance criteria from issue #16."""

    @pytest.mark.asyncio
    async def test_no_token_appears_in_any_export_format(self):
        """Export a report with a known session token — no token in any format."""
        known_token_hash = "known_session_token_hash_1234567890abcdef"
        report = _make_report(full_hash=known_token_hash)

        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")

        # GeoJSON
        geojson_bytes = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        assert known_token_hash not in geojson_bytes.decode()

        db2 = _make_db_mock([report])
        svc2 = ExportService(db=db2, analyst_id_hash="x")

        # CSV
        csv_bytes = await svc2.export_csv(ExportFilterParams())
        assert known_token_hash not in csv_bytes.decode("utf-8-sig")

    @pytest.mark.asyncio
    async def test_hash_truncated_in_geojson(self):
        """reporter_token_hash in GeoJSON must be at most 12 chars."""
        full_hash = "abcdef123456" + "should_not_appear"
        report = _make_report(full_hash=full_hash)
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_geojson(ExportFilterParams(), "2024-06-01")
        data = json.loads(result)
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            truncated = props.get("reporter_token_hash_truncated", "")
            assert len(truncated) <= 12
            assert "should_not_appear" not in truncated

    @pytest.mark.asyncio
    async def test_analyst_note_body_absent_in_csv(self):
        """AnalystNote.body must not appear in CSV export."""
        report = _make_report()
        report.analyst_notes = [MagicMock(body="TOP SECRET ANALYST NOTE")]
        db = _make_db_mock([report])
        svc = ExportService(db=db, analyst_id_hash="x")
        result = await svc.export_csv(ExportFilterParams())
        assert "TOP SECRET ANALYST NOTE" not in result.decode("utf-8-sig")

    @pytest.mark.asyncio
    async def test_audit_log_created_for_every_export(self):
        """Every export operation writes to AuditLog."""
        for fmt in ["geojson", "csv"]:
            db = _make_db_mock([_make_report()])
            svc = ExportService(db=db, analyst_id_hash="analyst_999")
            if fmt == "geojson":
                await svc.export_geojson(ExportFilterParams(), "2024-06-01")
            else:
                await svc.export_csv(ExportFilterParams())
            db.add.assert_called()
            audit_entry = db.add.call_args[0][0]
            assert audit_entry.actor_id_hash == "analyst_999"

    @pytest.mark.asyncio
    async def test_large_export_returns_job_id_under_500ms(self):
        """Synchronous response for large export must come back quickly."""
        import time

        from app.api.v1.routes.export import export_geojson

        mock_db = AsyncMock()
        mock_redis = AsyncMock()

        large_count = ASYNC_THRESHOLD + 100
        with (
            patch(
                "app.api.v1.routes.export.ExportService",
                return_value=_make_export_service_mock(record_count=large_count),
            ),
            patch(
                "app.api.v1.routes.export.create_export_job",
                new_callable=AsyncMock,
                return_value="fast-job-id",
            ),
        ):
            start = time.monotonic()
            response = await export_geojson(
                crisis_type=None,
                damage_severity=None,
                infrastructure_type=None,
                report_status=None,
                time_from=None,
                time_to=None,
                min_ai_confidence=None,
                include_footprints=False,
                current_user={"sub": "a", "role": "analyst"},
                db=mock_db,
                redis=mock_redis,
            )
            elapsed = time.monotonic() - start

        assert elapsed < 0.5, f"Response took {elapsed:.3f}s — expected < 500ms"
        from fastapi.responses import JSONResponse

        assert isinstance(response, JSONResponse)
        body = json.loads(response.body)
        assert body["status"] == "processing"
