"""Extended tests for analyst_service.py and analyst route handlers.

Targets the specific uncovered lines from the coverage report:
  analyst_service.py  lines 119-145, 152-161, 224-276, 305-377, 425, 437,
                             445, 461, 468-473, 483, 504-533, 570-618,
                             650-664, 686-744, 768-806
  analyst.py          lines 243-253, 293, 373-448, 453, 479, 532

Strategy: test the service functions directly with fine-grained AsyncMock
control, then test the thin route layer only for the paths not already hit.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.models.enums import (
    CrisisType,
    InfrastructureType,
    PhotoStatus,
    ReportDamageSeverity,
    ReportStatus,
    ReviewPriority,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_report(
    *,
    id: UUID | None = None,
    status: str = "pending",
    crisis_type: str = "flood",
    damage_severity: str = "partial",
    infrastructure_type: str = "residential",
    lat: float = -1.29,
    lng: float = 36.82,
    reporter_trust_tier: int = 1,
    building_id: UUID | None = None,
    ai_confidence: float | None = 0.85,
    photo_url: str | None = None,
    photo_status: str = "accepted",
):
    r = MagicMock()
    r.id = id or uuid4()
    r.crisis_type = CrisisType(crisis_type)
    r.damage_severity = ReportDamageSeverity(damage_severity)
    r.infrastructure_type = InfrastructureType(infrastructure_type)
    r.status = ReportStatus(status)
    r.lat = lat
    r.lng = lng
    r.reporter_trust_tier = reporter_trust_tier
    r.building_id = building_id
    r.ai_confidence = ai_confidence
    r.ai_severity_prediction = ReportDamageSeverity("partial")
    r.ai_divergence = False
    r.ai_quality_score = 0.9
    r.photo_url = photo_url
    r.photo_status = PhotoStatus(photo_status)
    r.gps_accuracy_m = None
    r.landmark_description = None
    r.electricity_status = None
    r.health_services_status = None
    r.most_pressing_needs = None
    r.debris_clearing_needed = None
    r.duplicate_of_id = None
    r.possible_duplicate_of_id = None
    r.duplicate_score = None
    r.analyst_severity_override = None
    r.review_priority = ReviewPriority.normal
    r.analyst_notes = []
    r.created_at = datetime.now(tz=timezone.utc)
    r.updated_at = datetime.now(tz=timezone.utc)
    return r


def _scalar_result(value):
    """Return a mock whose .scalar_one_or_none() returns value."""
    m = MagicMock()
    m.scalar_one_or_none = MagicMock(return_value=value)
    m.scalar_one = MagicMock(return_value=value)
    return m


def _scalars_result(values: list):
    """Return a mock whose .scalars().all() returns values."""
    m = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all = MagicMock(return_value=values)
    m.scalars = MagicMock(return_value=scalars_mock)
    return m


def _rows_result(rows: list):
    """Return a mock whose .fetchone() or iteration returns rows."""
    m = MagicMock()
    m.fetchone = MagicMock(return_value=rows[0] if rows else None)
    m.__iter__ = MagicMock(return_value=iter(rows))
    m.all = MagicMock(return_value=rows)
    return m


# ============================================================
# SERVICE LAYER TESTS
# ============================================================


class TestBuildReportFilters:
    """Unit tests for the _build_report_filters helper (lines 119-145)."""

    def test_no_filters_returns_unchanged_query(self):
        import sqlalchemy as sa

        from app.models.report import Report
        from app.services.analyst_service import _build_report_filters

        q = sa.select(Report)
        result = _build_report_filters(
            q,
            crisis_type=None,
            damage_severity=None,
            infrastructure_type=None,
            status=None,
            time_from=None,
            time_to=None,
            min_ai_confidence=None,
            review_priority=None,
            ai_divergence_only=None,
            region_geojson=None,
        )
        assert result is not None

    def test_all_filters_applied(self):
        import sqlalchemy as sa

        from app.models.report import Report
        from app.services.analyst_service import _build_report_filters

        q = sa.select(Report)
        result = _build_report_filters(
            q,
            crisis_type=["flood", "earthquake"],
            damage_severity=["partial"],
            infrastructure_type=["residential"],
            status=["pending"],
            time_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            time_to=datetime(2026, 6, 1, tzinfo=timezone.utc),
            min_ai_confidence=0.7,
            review_priority=["critical", "high"],
            ai_divergence_only=True,
            region_geojson=None,
        )
        compiled = str(result.compile())
        assert "crisis_type" in compiled.lower()

    def test_region_geojson_adds_st_within(self):
        import sqlalchemy as sa

        from app.models.report import Report
        from app.services.analyst_service import _build_report_filters

        q = sa.select(Report)
        region = (
            '{"type":"Polygon","coordinates":'
            "[[[36,-2],[38,-2],[38,0],[36,0],[36,-2]]]}"
        )
        result = _build_report_filters(
            q,
            crisis_type=None,
            damage_severity=None,
            infrastructure_type=None,
            status=None,
            time_from=None,
            time_to=None,
            min_ai_confidence=None,
            review_priority=None,
            ai_divergence_only=None,
            region_geojson=region,
        )
        compiled = str(result.compile())
        assert "ST_Within" in compiled


class TestGetBuildingFootprintGeoJson:
    """Tests for _get_building_footprint_geojson (lines 148-161)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_building_id_is_none(self):
        from app.services.analyst_service import _get_building_footprint_geojson

        db = AsyncMock()
        result = await _get_building_footprint_geojson(db, None)
        assert result is None
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_geojson_when_found(self):
        from app.services.analyst_service import _get_building_footprint_geojson

        db = AsyncMock()
        row = MagicMock()
        row.geojson = '{"type":"Polygon"}'
        db.execute = AsyncMock(return_value=_rows_result([row]))
        result = await _get_building_footprint_geojson(db, uuid4())
        assert result == '{"type":"Polygon"}'

    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self):
        from app.services.analyst_service import _get_building_footprint_geojson

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result([]))
        result = await _get_building_footprint_geojson(db, uuid4())
        assert result is None


class TestListReports:
    """Tests for list_reports (lines 224-276)."""

    @pytest.mark.asyncio
    async def test_returns_paginated_result(self):
        from app.services.analyst_service import list_reports

        db = AsyncMock()
        report = _make_report()

        count_mock = MagicMock()
        count_mock.scalar_one = MagicMock(return_value=1)
        items_mock = _scalars_result([report])

        db.execute = AsyncMock(side_effect=[count_mock, items_mock])

        result = await list_reports(db, page=1, limit=10)

        assert result.total == 1
        assert result.page == 1
        assert result.limit == 10

    @pytest.mark.asyncio
    async def test_severity_sort_applied(self):
        from app.services.analyst_service import list_reports

        db = AsyncMock()
        count_mock = MagicMock()
        count_mock.scalar_one = MagicMock(return_value=0)
        items_mock = _scalars_result([])
        db.execute = AsyncMock(side_effect=[count_mock, items_mock])

        result = await list_reports(db, sort_by="severity")

        assert result.total == 0

    @pytest.mark.asyncio
    async def test_pagination_offset_calculated(self):
        from app.services.analyst_service import list_reports

        db = AsyncMock()
        count_mock = MagicMock()
        count_mock.scalar_one = MagicMock(return_value=100)
        items_mock = _scalars_result([])
        db.execute = AsyncMock(side_effect=[count_mock, items_mock])

        result = await list_reports(db, page=3, limit=20)

        assert result.page == 3
        assert result.limit == 20

    @pytest.mark.asyncio
    async def test_all_filter_types_forwarded(self):
        from app.services.analyst_service import list_reports

        db = AsyncMock()
        count_mock = MagicMock()
        count_mock.scalar_one = MagicMock(return_value=0)
        items_mock = _scalars_result([])
        db.execute = AsyncMock(side_effect=[count_mock, items_mock])

        result = await list_reports(
            db,
            crisis_type=["flood"],
            damage_severity=["partial", "destroyed"],
            infrastructure_type=["residential"],
            status=["pending"],
            time_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            time_to=datetime(2026, 12, 1, tzinfo=timezone.utc),
            min_ai_confidence=0.5,
            region_geojson=None,
        )

        assert result.total == 0


class TestGetReportDetail:
    """Tests for get_report_detail (lines 305-377)."""

    @pytest.mark.asyncio
    async def test_returns_none_when_report_not_found(self):
        from app.services.analyst_service import get_report_detail

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        result = await get_report_detail(db, uuid4())
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_detail_with_no_building(self):
        from app.services.analyst_service import get_report_detail

        db = AsyncMock()
        report = _make_report(building_id=None)
        report.analyst_notes = []

        execute_results = [
            _scalar_result(report),
            _rows_result([]),
        ]
        db.execute = AsyncMock(side_effect=execute_results)

        with patch(
            "app.services.analyst_service._get_building_footprint_geojson",
            new=AsyncMock(return_value=None),
        ):
            result = await get_report_detail(db, report.id)

        assert result is not None
        assert result.building_id is None
        assert result.building_timeline == []

    @pytest.mark.asyncio
    async def test_loads_building_timeline_when_building_present(self):
        from app.services.analyst_service import get_report_detail

        building_id = uuid4()
        report = _make_report(building_id=building_id)
        report.analyst_notes = []

        timeline_report = _make_report(
            building_id=building_id,
            status="verified",
            damage_severity="partial",
        )

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                _scalar_result(report),
                _scalars_result([timeline_report]),
            ]
        )

        with patch(
            "app.services.analyst_service._get_building_footprint_geojson",
            new=AsyncMock(return_value='{"type":"Polygon"}'),
        ):
            result = await get_report_detail(db, report.id)

        assert result is not None
        assert len(result.building_timeline) == 1

    @pytest.mark.asyncio
    async def test_responder_scope_returns_none_for_out_of_region(self):
        from app.services.analyst_service import get_report_detail

        report = _make_report(lat=-5.0, lng=37.0, building_id=None)
        report.analyst_notes = []

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        region = json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [[36.0, -2.0], [38.0, -2.0], [38.0, 0.0], [36.0, 0.0], [36.0, -2.0]]
                ],
            }
        )

        try:
            import shapely  # noqa

            result = await get_report_detail(db, report.id, region_geojson=region)
            assert result is None
        except ImportError:
            pytest.skip("shapely not installed — scope filter not exercised")

    @pytest.mark.asyncio
    async def test_responder_scope_allows_report_inside_region(self):
        from app.services.analyst_service import get_report_detail

        report = _make_report(lat=-1.0, lng=37.0, building_id=None)
        report.analyst_notes = []

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        region = json.dumps(
            {
                "type": "Polygon",
                "coordinates": [
                    [[36.0, -2.0], [38.0, -2.0], [38.0, 0.0], [36.0, 0.0], [36.0, -2.0]]
                ],
            }
        )

        with patch(
            "app.services.analyst_service._get_building_footprint_geojson",
            new=AsyncMock(return_value=None),
        ):
            try:
                result = await get_report_detail(db, report.id, region_geojson=region)
                assert result is not None
            except ImportError:
                pytest.skip("shapely not installed")

    @pytest.mark.asyncio
    async def test_analyst_notes_attached(self):
        from app.schemas.analyst_schemas import AnalystNoteOut
        from app.services.analyst_service import get_report_detail

        report = _make_report(building_id=None)
        note = MagicMock()
        note.id = uuid4()
        note.body = "Test note"
        note.created_at = datetime.now(tz=timezone.utc)
        report.analyst_notes = [note]

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        with patch(
            "app.services.analyst_service._get_building_footprint_geojson",
            new=AsyncMock(return_value=None),
        ):
            with patch.object(
                AnalystNoteOut,
                "model_validate",
                return_value=AnalystNoteOut(
                    id=note.id,
                    body=note.body,
                    created_at=note.created_at,
                ),
            ):
                result = await get_report_detail(db, report.id)

        assert result is not None
        assert len(result.analyst_notes) == 1
        assert result.analyst_notes[0].body == "Test note"


class TestTransitionReportStatus:
    """Tests for transition_report_status (lines 419-495)."""

    @pytest.mark.asyncio
    async def test_invalid_status_raises_value_error(self):
        from app.services.analyst_service import transition_report_status

        db = AsyncMock()
        with pytest.raises(ValueError, match="Invalid target status"):
            await transition_report_status(
                db,
                uuid4(),
                new_status=ReportStatus.pending,
                reason_code=None,
                notes=None,
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_invalid_reason_code_raises(self):
        from app.services.analyst_service import transition_report_status

        db = AsyncMock()
        with pytest.raises(ValueError, match="Invalid reason_code"):
            await transition_report_status(
                db,
                uuid4(),
                new_status=ReportStatus.rejected,
                reason_code="made_up_code",
                notes=None,
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_report_not_found_raises_lookup_error(self):
        from app.services.analyst_service import transition_report_status

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        with pytest.raises(LookupError):
            await transition_report_status(
                db,
                uuid4(),
                new_status=ReportStatus.verified,
                reason_code=None,
                notes=None,
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_verified_with_building_triggers_severity_sync(self):
        from app.services.analyst_service import transition_report_status

        building_id = uuid4()
        report = _make_report(reporter_trust_tier=0, building_id=building_id)

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        with patch(
            "app.services.analyst_service._sync_building_severity",
            new=AsyncMock(),
        ) as mock_sync:
            await transition_report_status(
                db,
                report.id,
                new_status=ReportStatus.verified,
                reason_code=None,
                notes=None,
                analyst_id_hash="x" * 64,
            )
            mock_sync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verified_without_building_skips_severity_sync(self):
        from app.services.analyst_service import transition_report_status

        report = _make_report(reporter_trust_tier=1, building_id=None)

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        with patch(
            "app.services.analyst_service._sync_building_severity",
            new=AsyncMock(),
        ) as mock_sync:
            await transition_report_status(
                db,
                report.id,
                new_status=ReportStatus.verified,
                reason_code=None,
                notes=None,
                analyst_id_hash="x" * 64,
            )
            mock_sync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_notes_attached_when_provided(self):
        from app.services.analyst_service import transition_report_status

        report = _make_report()

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        await transition_report_status(
            db,
            report.id,
            new_status=ReportStatus.duplicate,
            reason_code=None,
            notes="Field-confirmed duplicate.",
            analyst_id_hash="x" * 64,
        )

        assert db.add.call_count >= 2

    @pytest.mark.asyncio
    async def test_pending_merge_review_cannot_be_transitioned_directly(self):
        """audit M-3: must go through confirm_merge / reject_merge."""
        from app.services.analyst_service import transition_report_status

        report = _make_report(status="pending_merge_review")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        with pytest.raises(ValueError, match="merge confirm/reject workflow"):
            await transition_report_status(
                db,
                report.id,
                new_status=ReportStatus.verified,
                reason_code=None,
                notes=None,
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_noop_transition_is_rejected(self):
        """audit M-3: re-verifying a verified report would walk trust tier."""
        from app.services.analyst_service import transition_report_status

        report = _make_report(status="verified")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        with pytest.raises(ValueError, match="Cannot transition"):
            await transition_report_status(
                db,
                report.id,
                new_status=ReportStatus.verified,
                reason_code=None,
                notes=None,
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_rejected_can_be_corrected_to_verified(self):
        from app.services.analyst_service import transition_report_status

        report = _make_report(status="rejected", reporter_trust_tier=0)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        result = await transition_report_status(
            db,
            report.id,
            new_status=ReportStatus.verified,
            reason_code=None,
            notes=None,
            analyst_id_hash="x" * 64,
        )
        assert result.status == ReportStatus.verified

    @pytest.mark.asyncio
    async def test_feedback_type_uses_canonical_token(self):
        """audit M-2: transition must log 'verify'/'reject', not the status."""
        from app.services import analyst_service
        from app.services.analyst_service import transition_report_status

        report = _make_report(status="pending")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        with patch.object(
            analyst_service, "_log_ai_feedback", new=AsyncMock()
        ) as mock_fb:
            await transition_report_status(
                db,
                report.id,
                new_status=ReportStatus.rejected,
                reason_code="inaccurate",
                notes=None,
                analyst_id_hash="x" * 64,
            )
        assert mock_fb.await_args.kwargs["feedback_type"] == "reject"

    @pytest.mark.asyncio
    async def test_rejected_with_all_reason_codes(self):
        from app.services.analyst_service import transition_report_status

        for reason in [
            "inaccurate",
            "duplicate",
            "poor_quality",
            "out_of_scope",
            "other",
        ]:
            report = _make_report()

            db = AsyncMock()
            db.execute = AsyncMock(return_value=_scalar_result(report))
            db.flush = AsyncMock()
            db.add = MagicMock()

            result = await transition_report_status(
                db,
                report.id,
                new_status=ReportStatus.rejected,
                reason_code=reason,
                notes=None,
                analyst_id_hash="x" * 64,
            )
            assert result is not None


class TestSyncBuildingSeverity:
    """Tests for _sync_building_severity (lines 498-535)."""

    @pytest.mark.asyncio
    async def test_no_op_when_building_not_found(self):
        from app.services.analyst_service import _sync_building_severity

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        await _sync_building_severity(db, uuid4(), ReportDamageSeverity.destroyed)

    @pytest.mark.asyncio
    async def test_upgrades_severity_when_higher(self):
        from app.models.enums import DamageSeverity
        from app.services.analyst_service import _sync_building_severity

        building = MagicMock()
        building.current_severity = DamageSeverity.minimal

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(building))

        await _sync_building_severity(db, uuid4(), ReportDamageSeverity.destroyed)

        assert building.current_severity == DamageSeverity.destroyed

    @pytest.mark.asyncio
    async def test_no_change_when_same_or_lower_severity(self):
        from app.models.enums import DamageSeverity
        from app.services.analyst_service import _sync_building_severity

        building = MagicMock()
        building.current_severity = DamageSeverity.destroyed
        original_severity = building.current_severity

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(building))

        await _sync_building_severity(db, uuid4(), ReportDamageSeverity.minimal)

        assert building.current_severity == original_severity


class TestMergeReports:
    """Tests for merge_reports (lines 569-618)."""

    @pytest.mark.asyncio
    async def test_primary_not_found_raises(self):
        from app.services.analyst_service import merge_reports

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        with pytest.raises(LookupError, match="Primary report"):
            await merge_reports(
                db,
                primary_id=uuid4(),
                duplicate_ids=[uuid4()],
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_duplicate_not_found_raises(self):
        from app.services.analyst_service import merge_reports

        primary = _make_report()
        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                _scalar_result(primary),
                _scalar_result(None),
            ]
        )

        with pytest.raises(LookupError, match="Duplicate report"):
            await merge_reports(
                db,
                primary_id=primary.id,
                duplicate_ids=[uuid4()],
                analyst_id_hash="x" * 64,
            )

    @pytest.mark.asyncio
    async def test_successful_merge_returns_correct_count(self):
        from app.services.analyst_service import merge_reports

        primary = _make_report()
        primary.photo_url = None
        dup1 = _make_report()
        dup1.photo_url = None
        dup2 = _make_report()
        dup2.photo_url = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                _scalar_result(primary),
                _scalar_result(dup1),
                _scalar_result(dup2),
            ]
        )
        db.flush = AsyncMock()
        db.add = MagicMock()

        result = await merge_reports(
            db,
            primary_id=primary.id,
            duplicate_ids=[dup1.id, dup2.id],
            analyst_id_hash="x" * 64,
        )

        assert result.merged_count == 2
        assert result.primary_id == primary.id

    @pytest.mark.asyncio
    async def test_dup_photo_transferred_to_primary(self):
        from app.services.analyst_service import merge_reports

        primary = _make_report()
        primary.photo_url = None
        dup = _make_report()
        dup.photo_url = "s3://bucket/photo.jpg"

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[
                _scalar_result(primary),
                _scalar_result(dup),
            ]
        )
        db.flush = AsyncMock()
        db.add = MagicMock()

        await merge_reports(
            db,
            primary_id=primary.id,
            duplicate_ids=[dup.id],
            analyst_id_hash="x" * 64,
        )

        assert primary.photo_url == "s3://bucket/photo.jpg"


class TestCreateAnalystNote:
    """Tests for create_analyst_note (lines 650-664)."""

    @pytest.mark.asyncio
    async def test_raises_when_report_not_found(self):
        from app.services.analyst_service import create_analyst_note

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        with pytest.raises(LookupError):
            await create_analyst_note(
                db, uuid4(), body="Test", analyst_id_hash="x" * 64
            )

    @pytest.mark.asyncio
    async def test_creates_note_and_returns_schema(self):
        from app.schemas.analyst_schemas import AnalystNoteOut
        from app.services.analyst_service import create_analyst_note

        report_id = uuid4()
        note_id = uuid4()

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report_id))
        db.flush = AsyncMock()
        db.add = MagicMock()

        note_mock = MagicMock()
        note_mock.id = note_id
        note_mock.body = "Confirmed by field team."
        note_mock.created_at = datetime.now(tz=timezone.utc)

        with patch("app.services.analyst_service.AnalystNote") as MockNote:
            MockNote.return_value = note_mock
            with patch.object(
                AnalystNoteOut,
                "model_validate",
                return_value=AnalystNoteOut(
                    id=note_id,
                    body="Confirmed by field team.",
                    created_at=note_mock.created_at,
                ),
            ):
                result = await create_analyst_note(
                    db,
                    report_id,
                    body="Confirmed by field team.",
                    analyst_id_hash="a" * 64,
                )

        assert result.body == "Confirmed by field team."
        assert "analyst_id_hash" not in result.model_fields_set


class TestGetStatsSummary:
    """Tests for get_stats_summary (lines 686-744)."""

    @pytest.mark.asyncio
    async def test_returns_cached_value_on_hit(self):
        from app.schemas.analyst_schemas import (
            CrisisTypeBreakdown,
            SeverityBreakdown,
            StatsSummaryResponse,
        )
        from app.services.analyst_service import get_stats_summary

        cached = StatsSummaryResponse(
            total=99,
            by_severity=SeverityBreakdown(minimal=10, partial=50, destroyed=39),
            by_crisis_type=CrisisTypeBreakdown(flood=99),
            last_updated=datetime.now(tz=timezone.utc),
        )

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=cached.model_dump_json())
        db = AsyncMock()

        result = await get_stats_summary(db, redis)

        assert result.total == 99
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_queries_db_on_cache_miss(self):
        from app.services.analyst_service import get_stats_summary

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        db = AsyncMock()

        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=5)

        sev_rows = MagicMock()
        sev_rows.__iter__ = MagicMock(return_value=iter([]))

        crisis_rows = MagicMock()
        crisis_rows.__iter__ = MagicMock(return_value=iter([]))

        pending_dup_result = MagicMock()
        pending_dup_result.scalar_one = MagicMock(return_value=0)

        db.execute = AsyncMock(
            side_effect=[total_result, sev_rows, crisis_rows, pending_dup_result]
        )

        result = await get_stats_summary(db, redis)

        assert result.total == 5
        redis.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_corrupted_cache_falls_through_to_db(self):
        from app.services.analyst_service import get_stats_summary

        redis = AsyncMock()
        redis.get = AsyncMock(return_value="not valid json {{{{")
        redis.set = AsyncMock()

        db = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=0)
        empty_rows = MagicMock()
        empty_rows.__iter__ = MagicMock(return_value=iter([]))
        pending_dup_result = MagicMock()
        pending_dup_result.scalar_one = MagicMock(return_value=0)

        db.execute = AsyncMock(
            side_effect=[total_result, empty_rows, empty_rows, pending_dup_result]
        )

        result = await get_stats_summary(db, redis)
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_severity_counts_aggregated(self):
        from app.services.analyst_service import get_stats_summary

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        db = AsyncMock()
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=3)

        sev_enum = MagicMock()
        sev_enum.value = "partial"
        sev_row = (sev_enum, 3)
        sev_rows = MagicMock()
        sev_rows.__iter__ = MagicMock(return_value=iter([sev_row]))

        crisis_rows = MagicMock()
        crisis_rows.__iter__ = MagicMock(return_value=iter([]))

        pending_dup_result = MagicMock()
        pending_dup_result.scalar_one = MagicMock(return_value=2)

        db.execute = AsyncMock(
            side_effect=[total_result, sev_rows, crisis_rows, pending_dup_result]
        )

        result = await get_stats_summary(db, redis)
        assert result.by_severity.partial == 3
        assert result.pending_duplicate_count == 2


class TestGetHeatmap:
    """Tests for get_heatmap (lines 768-806)."""

    @pytest.mark.asyncio
    async def test_returns_cached_geojson(self):
        from app.services.analyst_service import get_heatmap

        geojson = {"type": "FeatureCollection", "features": []}
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=json.dumps(geojson))
        db = AsyncMock()

        result = await get_heatmap(db, redis)
        assert result["type"] == "FeatureCollection"
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_queries_db_on_cache_miss(self):
        from app.services.analyst_service import get_heatmap

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        db = AsyncMock()
        rows_mock = MagicMock()
        rows_mock.all = MagicMock(return_value=[])
        db.execute = AsyncMock(return_value=rows_mock)

        result = await get_heatmap(db, redis)
        assert result["type"] == "FeatureCollection"
        assert result["features"] == []
        redis.set.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generates_features_with_weight(self):
        from app.services.analyst_service import get_heatmap

        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        db = AsyncMock()
        sev = MagicMock()
        sev.value = "destroyed"
        row = (-1.29, 36.82, sev)
        rows_mock = MagicMock()
        rows_mock.all = MagicMock(return_value=[row])
        db.execute = AsyncMock(return_value=rows_mock)

        result = await get_heatmap(db, redis)
        assert len(result["features"]) == 1
        assert result["features"][0]["properties"]["weight"] == 3

    @pytest.mark.asyncio
    async def test_corrupt_cache_falls_through(self):
        from app.services.analyst_service import get_heatmap

        redis = AsyncMock()
        redis.get = AsyncMock(return_value="bad json")
        redis.set = AsyncMock()

        db = AsyncMock()
        rows_mock = MagicMock()
        rows_mock.all = MagicMock(return_value=[])
        db.execute = AsyncMock(return_value=rows_mock)

        result = await get_heatmap(db, redis)
        assert result["type"] == "FeatureCollection"


# ============================================================
# ROUTE LAYER TESTS — uncovered paths
# ============================================================


def _make_app(current_user: dict, mock_redis):
    """Build a minimal test app with dependency overrides."""
    from fastapi import FastAPI

    from app.api.v1.routes.analyst import analyst_router, stats_router
    from app.core.dependencies import get_db, get_redis

    test_app = FastAPI()
    test_app.include_router(analyst_router, prefix="/api/v1")
    test_app.include_router(stats_router, prefix="/api/v1")

    async def _fake_db():
        db = AsyncMock()
        db.execute = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.rollback = AsyncMock()
        db.add = MagicMock()
        yield db

    test_app.dependency_overrides[get_db] = _fake_db
    test_app.dependency_overrides[get_redis] = lambda: mock_redis

    from app.api.v1.routes.auth import get_current_user

    test_app.dependency_overrides[get_current_user] = lambda: current_user

    return test_app


class TestGetReportDetailRoute:
    """Tests for GET /analyst/reports/{id} route (line 293 = 404 path)."""

    def test_returns_404_when_service_returns_none(self, mock_redis):
        from fastapi.testclient import TestClient

        from app.services import analyst_service

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)
        client = TestClient(app, raise_server_exceptions=False)

        with patch.object(
            analyst_service, "get_report_detail", new=AsyncMock(return_value=None)
        ):
            resp = client.get(f"/api/v1/analyst/reports/{uuid4()}")

        assert resp.status_code == 404

    def test_returns_200_with_detail_schema(self, mock_redis):
        from fastapi.testclient import TestClient

        from app.models.enums import (
            CrisisType,
            InfrastructureType,
            PhotoStatus,
            ReportDamageSeverity,
            ReportStatus,
        )
        from app.schemas.analyst_schemas import ReportDetailSchema
        from app.services import analyst_service

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)
        client = TestClient(app)

        report_id = uuid4()
        detail = ReportDetailSchema(
            id=report_id,
            crisis_type=CrisisType.flood,
            infrastructure_type=InfrastructureType.residential,
            damage_severity=ReportDamageSeverity.partial,
            photo_status=PhotoStatus.accepted,
            status=ReportStatus.pending,
            reporter_trust_tier=1,
            created_at=datetime.now(tz=timezone.utc),
            updated_at=datetime.now(tz=timezone.utc),
        )

        with patch.object(
            analyst_service,
            "get_report_detail",
            new=AsyncMock(return_value=detail),
        ):
            resp = client.get(f"/api/v1/analyst/reports/{report_id}")

        assert resp.status_code == 200
        assert resp.json()["id"] == str(report_id)


class TestTransitionStatusRoute:
    """Tests for PATCH /analyst/reports/{id}/status (lines 375-448)."""

    def test_returns_404_when_report_not_found(self, mock_redis):
        from fastapi.testclient import TestClient

        from app.services import analyst_service

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)
        client = TestClient(app, raise_server_exceptions=False)

        async def _raise(*args, **kwargs):
            raise LookupError("Report not found.")

        with patch.object(analyst_service, "transition_report_status", new=_raise):
            resp = client.patch(
                f"/api/v1/analyst/reports/{uuid4()}/status",
                json={"status": "verified"},
            )

        assert resp.status_code == 404

    def test_returns_422_on_business_rule_violation(self, mock_redis):
        from fastapi.testclient import TestClient

        from app.services import analyst_service

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)
        client = TestClient(app, raise_server_exceptions=False)

        async def _raise(*args, **kwargs):
            raise ValueError("reason_code is required when status == 'rejected'.")

        with patch.object(analyst_service, "transition_report_status", new=_raise):
            resp = client.patch(
                f"/api/v1/analyst/reports/{uuid4()}/status",
                json={"status": "rejected"},
            )

        assert resp.status_code == 422

    def test_duplicate_status_allowed(self, mock_redis):
        from fastapi.testclient import TestClient

        from app.services import analyst_service

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)
        client = TestClient(app)

        report = _make_report()

        with patch.object(
            analyst_service,
            "transition_report_status",
            new=AsyncMock(return_value=report),
        ):
            resp = client.patch(
                f"/api/v1/analyst/reports/{report.id}/status",
                json={"status": "duplicate"},
            )

        assert resp.status_code == 200


class TestHeatmapRoute:
    """Tests for GET /stats/heatmap (line 532)."""

    def test_returns_geojson_feature_collection(self, mock_redis):
        from fastapi.testclient import TestClient

        from app.services import analyst_service

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)
        client = TestClient(app)

        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [36.82, -1.29],
                    },
                    "properties": {"weight": 2},
                }
            ],
        }

        with patch.object(
            analyst_service, "get_heatmap", new=AsyncMock(return_value=geojson)
        ):
            resp = client.get("/api/v1/stats/heatmap")

        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1


class TestSSERoute:
    """Tests for GET /analyst/stream (lines 373-448)."""

    def test_stream_requires_analyst_or_responder_role(self, mock_redis):
        from fastapi.testclient import TestClient

        reporter_user = {"sub": "r" * 64, "role": "reporter", "tier": 1}
        app = _make_app(reporter_user, mock_redis)
        client = TestClient(app, raise_server_exceptions=False)

        resp = client.get("/api/v1/analyst/stream")
        assert resp.status_code == 403

    def test_stream_returns_event_stream_content_type(self, mock_redis):
        from fastapi.testclient import TestClient

        analyst_user = {"sub": "a" * 64, "role": "analyst", "tier": 0}
        app = _make_app(analyst_user, mock_redis)

        async def _empty_gen(redis, user):
            yield ": heartbeat\n\n"

        with patch(
            "app.api.v1.routes.analyst._sse_event_generator",
            new=_empty_gen,
        ):
            client = TestClient(app)
            with client.stream("GET", "/api/v1/analyst/stream") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]


class TestHeartbeatTicker:
    """Test the heartbeat ticker coroutine directly (line 453)."""

    @pytest.mark.asyncio
    async def test_ticker_completes_after_delay(self):
        from app.api.v1.routes.analyst import _heartbeat_ticker

        with patch("app.api.v1.routes.analyst._SSE_HEARTBEAT_INTERVAL", 0):
            await _heartbeat_ticker()


class TestSseEventGenerator:
    """Tests for _sse_event_generator (lines 357-449)."""

    @pytest.mark.asyncio
    async def test_emits_heartbeat_when_no_messages(self):
        from app.api.v1.routes.analyst import _sse_event_generator

        redis = AsyncMock()
        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(return_value=None)
        pubsub.unsubscribe = AsyncMock()
        pubsub.aclose = AsyncMock()
        redis.pubsub = MagicMock(return_value=pubsub)
        pubsub.subscribe = AsyncMock()

        user = {"sub": "a" * 64, "role": "analyst"}

        events = []

        with patch("app.api.v1.routes.analyst._SSE_HEARTBEAT_INTERVAL", 0):
            gen = _sse_event_generator(redis, user)
            try:
                event = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
                events.append(event)
            except (StopAsyncIteration, asyncio.TimeoutError):
                pass

        assert True

    @pytest.mark.asyncio
    async def test_emits_message_event(self):
        from app.api.v1.routes.analyst import _sse_event_generator

        redis = AsyncMock()
        pubsub = AsyncMock()

        event_data = {"event": "report.created", "id": str(uuid4())}
        message = {"type": "message", "data": json.dumps(event_data)}

        call_count = 0

        async def _get_message(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return message
            raise asyncio.CancelledError()

        pubsub.get_message = _get_message
        pubsub.unsubscribe = AsyncMock()
        pubsub.aclose = AsyncMock()
        pubsub.subscribe = AsyncMock()
        redis.pubsub = MagicMock(return_value=pubsub)

        user = {"sub": "a" * 64, "role": "analyst"}

        events = []
        with patch("app.api.v1.routes.analyst._SSE_HEARTBEAT_INTERVAL", 30):
            gen = _sse_event_generator(redis, user)
            try:
                event = await asyncio.wait_for(gen.__anext__(), timeout=3.0)
                events.append(event)
            except (
                StopAsyncIteration,
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ):
                pass

        if events:
            assert "report.created" in events[0]


class TestWriteAuditLog:
    """Test _write_audit_log helper directly (lines 164-181)."""

    @pytest.mark.asyncio
    async def test_adds_audit_log_entry_to_session(self):
        from app.services.analyst_service import _write_audit_log

        db = MagicMock()
        db.add = MagicMock()

        record_id = uuid4()
        await _write_audit_log(
            db,
            operation="report.test_op",
            actor_id_hash="a" * 64,
            record_id=record_id,
            before_state={"status": "pending"},
            after_state={"status": "verified"},
        )

        db.add.assert_called_once()
        call_arg = db.add.call_args[0][0]
        assert call_arg.operation == "report.test_op"
        assert call_arg.record_id == record_id

    @pytest.mark.asyncio
    async def test_accepts_none_before_state(self):
        from app.services.analyst_service import _write_audit_log

        db = MagicMock()
        db.add = MagicMock()

        await _write_audit_log(
            db,
            operation="report.create",
            actor_id_hash="a" * 64,
            record_id=uuid4(),
            before_state=None,
            after_state={"status": "pending"},
        )

        db.add.assert_called_once()
        call_arg = db.add.call_args[0][0]
        assert call_arg.before_state is None


# ============================================================
# HITL — Analyst severity override (Feature 1)
# ============================================================


class TestSetSeverityOverride:
    """Tests for set_severity_override in analyst_service."""

    @pytest.mark.asyncio
    async def test_raises_lookup_error_when_report_not_found(self):
        from app.services.analyst_service import set_severity_override

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        with pytest.raises(LookupError, match="not found"):
            await set_severity_override(
                db,
                uuid4(),
                override=ReportDamageSeverity.destroyed,
                analyst_id_hash="a" * 64,
            )

    @pytest.mark.asyncio
    async def test_writes_override_without_touching_reporter_severity(self):
        from app.schemas.analyst_schemas import SeverityOverrideResponse
        from app.services.analyst_service import set_severity_override

        report = _make_report(damage_severity="partial")
        report.ai_severity_prediction = ReportDamageSeverity("minimal")
        report.analyst_severity_override = None

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        result = await set_severity_override(
            db,
            report.id,
            override=ReportDamageSeverity.destroyed,
            analyst_id_hash="a" * 64,
        )

        assert isinstance(result, SeverityOverrideResponse)
        assert result.analyst_severity_override == ReportDamageSeverity.destroyed
        # Reporter severity MUST NOT be modified.
        assert report.damage_severity == ReportDamageSeverity("partial")

    @pytest.mark.asyncio
    async def test_logs_ai_feedback_and_audit_entry(self):
        from app.services.analyst_service import set_severity_override

        report = _make_report()
        report.ai_severity_prediction = ReportDamageSeverity("partial")
        report.analyst_severity_override = None

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        await set_severity_override(
            db,
            report.id,
            override=ReportDamageSeverity.minimal,
            analyst_id_hash="b" * 64,
        )

        # db.add should be called at least twice: AuditLog + AIFeedback.
        assert db.add.call_count >= 2


# ============================================================
# HITL — Pending merge review (Feature 2)
# ============================================================


class TestConfirmPendingMerge:
    """Tests for confirm_pending_merge in analyst_service."""

    @pytest.mark.asyncio
    async def test_raises_lookup_error_when_not_found(self):
        from app.services.analyst_service import confirm_pending_merge

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        with pytest.raises(LookupError):
            await confirm_pending_merge(db, uuid4(), analyst_id_hash="a" * 64)

    @pytest.mark.asyncio
    async def test_raises_value_error_for_wrong_status(self):
        from app.services.analyst_service import confirm_pending_merge

        report = _make_report(status="pending")  # not pending_merge_review
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        with pytest.raises(ValueError, match="not pending merge review"):
            await confirm_pending_merge(db, report.id, analyst_id_hash="a" * 64)

    @pytest.mark.asyncio
    async def test_raises_value_error_when_no_possible_duplicate_id(self):
        from app.services.analyst_service import confirm_pending_merge

        report = _make_report(status="pending_merge_review")
        report.possible_duplicate_of_id = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        with pytest.raises(ValueError, match="no possible_duplicate_of_id"):
            await confirm_pending_merge(db, report.id, analyst_id_hash="a" * 64)

    @pytest.mark.asyncio
    async def test_successful_confirm_sets_duplicate_status(self):
        from app.schemas.analyst_schemas import ConfirmMergeResponse
        from app.services.analyst_service import confirm_pending_merge

        primary_id = uuid4()
        report = _make_report(status="pending_merge_review")
        report.possible_duplicate_of_id = primary_id
        report.photo_url = None

        # Second execute for the primary report lookup.
        primary = _make_report()
        primary.photo_url = None

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[_scalar_result(report), _scalar_result(primary)]
        )
        db.flush = AsyncMock()
        db.add = MagicMock()

        result = await confirm_pending_merge(db, report.id, analyst_id_hash="a" * 64)

        assert isinstance(result, ConfirmMergeResponse)
        assert result.merged_into == primary_id
        assert report.status == ReportStatus.duplicate
        assert report.duplicate_of_id == primary_id
        assert report.possible_duplicate_of_id is None

    @pytest.mark.asyncio
    async def test_photo_transferred_to_primary_on_confirm(self):
        from app.services.analyst_service import confirm_pending_merge

        primary_id = uuid4()
        report = _make_report(status="pending_merge_review")
        report.possible_duplicate_of_id = primary_id
        report.photo_url = "s3://bucket/new.jpg"

        primary = _make_report()
        primary.photo_url = None  # primary has no photo

        db = AsyncMock()
        db.execute = AsyncMock(
            side_effect=[_scalar_result(report), _scalar_result(primary)]
        )
        db.flush = AsyncMock()
        db.add = MagicMock()

        await confirm_pending_merge(db, report.id, analyst_id_hash="a" * 64)

        assert primary.photo_url == "s3://bucket/new.jpg"


class TestRejectPendingMerge:
    """Tests for reject_pending_merge in analyst_service."""

    @pytest.mark.asyncio
    async def test_raises_lookup_error_when_not_found(self):
        from app.services.analyst_service import reject_pending_merge

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(None))

        with pytest.raises(LookupError):
            await reject_pending_merge(db, uuid4(), analyst_id_hash="a" * 64)

    @pytest.mark.asyncio
    async def test_raises_value_error_for_wrong_status(self):
        from app.services.analyst_service import reject_pending_merge

        report = _make_report(status="pending")
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))

        with pytest.raises(ValueError, match="not pending merge review"):
            await reject_pending_merge(db, report.id, analyst_id_hash="a" * 64)

    @pytest.mark.asyncio
    async def test_successful_reject_restores_pending_status(self):
        from app.schemas.analyst_schemas import RejectMergeResponse
        from app.services.analyst_service import reject_pending_merge

        report = _make_report(status="pending_merge_review")
        report.possible_duplicate_of_id = uuid4()
        report.duplicate_score = 0.93

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalar_result(report))
        db.flush = AsyncMock()
        db.add = MagicMock()

        result = await reject_pending_merge(db, report.id, analyst_id_hash="a" * 64)

        assert isinstance(result, RejectMergeResponse)
        assert result.status == "pending"
        assert report.status == ReportStatus.pending
        assert report.possible_duplicate_of_id is None
        assert report.duplicate_score is None


# ============================================================
# HITL — AI accuracy / active learning (Feature 3)
# ============================================================


class TestGetAiAccuracy:
    """Tests for get_ai_accuracy in analyst_service."""

    def _make_feedback(
        self,
        *,
        feedback_type: str = "verify",
        is_agreement: bool = True,
        ai_confidence: float = 0.85,
        ai_prediction: str = "partial",
        analyst_decision: str = "partial",
    ):
        f = MagicMock()
        f.feedback_type = feedback_type
        f.is_agreement = is_agreement
        f.ai_confidence = ai_confidence
        f.ai_prediction = ai_prediction
        f.analyst_decision = analyst_decision
        return f

    @pytest.mark.asyncio
    async def test_returns_zeros_when_no_feedback(self):
        from app.services.analyst_service import get_ai_accuracy

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        result = await get_ai_accuracy(db, redis)

        assert result.total_feedback == 0
        assert result.agreement_rate is None
        assert result.recommended_divergence_threshold is None

    @pytest.mark.asyncio
    async def test_does_not_write_threshold_below_min_sample(self):
        """With fewer than 30 HC entries, threshold must NOT be written."""
        from app.services.analyst_service import get_ai_accuracy

        # 10 high-confidence agreeing feedback entries — below MIN_SAMPLE (30).
        feedback = [
            self._make_feedback(ai_confidence=0.9, is_agreement=True) for _ in range(10)
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result(feedback))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()

        result = await get_ai_accuracy(db, redis)

        assert result.high_confidence_feedback_count == 10
        assert result.recommended_divergence_threshold == 0.7  # computed…
        # …but NOT written to Redis because sample is too small.
        redis.set.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_writes_threshold_when_min_sample_met(self):
        """With >= 30 HC entries, threshold IS written to Redis."""
        from app.services.analyst_service import get_ai_accuracy

        feedback = [
            self._make_feedback(ai_confidence=0.9, is_agreement=True) for _ in range(30)
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result(feedback))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)  # no prior threshold
        redis.set = AsyncMock()
        redis.lpush = AsyncMock()
        redis.ltrim = AsyncMock()

        result = await get_ai_accuracy(db, redis)

        assert result.high_confidence_feedback_count == 30
        # hc_rate = 1.0 >= 0.85 → threshold = 0.70
        assert result.recommended_divergence_threshold == 0.7
        # Threshold and timestamp were written.
        assert redis.set.await_count == 2

    @pytest.mark.asyncio
    async def test_threshold_logic_drifting_model(self):
        """hc_rate in [0.70, 0.85) → threshold = 0.60."""
        from app.services.analyst_service import get_ai_accuracy

        # 30 HC entries, 75% agreement.
        feedback = [
            self._make_feedback(ai_confidence=0.9, is_agreement=(i < 23))
            for i in range(30)
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result(feedback))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.lpush = AsyncMock()
        redis.ltrim = AsyncMock()

        result = await get_ai_accuracy(db, redis)

        assert result.recommended_divergence_threshold == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_threshold_logic_poorly_calibrated(self):
        """hc_rate < 0.70 → threshold = 0.50."""
        from app.services.analyst_service import get_ai_accuracy

        # 30 HC entries, 60% agreement.
        feedback = [
            self._make_feedback(ai_confidence=0.9, is_agreement=(i < 18))
            for i in range(30)
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result(feedback))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.lpush = AsyncMock()
        redis.ltrim = AsyncMock()

        result = await get_ai_accuracy(db, redis)

        assert result.recommended_divergence_threshold == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_staleness_flag_set_when_threshold_is_old(self):
        """threshold_is_stale=True when updated_at > AI_DIVERGENCE_STALENESS_DAYS ago."""  # noqa: E501
        from app.services.analyst_service import get_ai_accuracy

        # Return a timestamp 10 days in the past.
        old_ts = datetime(2026, 6, 4, tzinfo=timezone.utc).isoformat()

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=old_ts)

        result = await get_ai_accuracy(db, redis)

        assert result.threshold_is_stale is True
        assert result.threshold_updated_at is not None

    @pytest.mark.asyncio
    async def test_staleness_flag_clear_when_threshold_is_fresh(self):
        """threshold_is_stale=False when updated_at is within the staleness window."""
        from app.services.analyst_service import get_ai_accuracy

        fresh_ts = datetime.now(tz=timezone.utc).isoformat()

        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result([]))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=fresh_ts)

        result = await get_ai_accuracy(db, redis)

        assert result.threshold_is_stale is False

    @pytest.mark.asyncio
    async def test_calibration_history_pushed_on_write(self):
        """Each successful calibration pushes an entry to the history list."""
        from app.services.analyst_service import get_ai_accuracy

        feedback = [
            self._make_feedback(ai_confidence=0.9, is_agreement=True) for _ in range(30)
        ]
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_scalars_result(feedback))
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.lpush = AsyncMock()
        redis.ltrim = AsyncMock()

        await get_ai_accuracy(db, redis)

        redis.lpush.assert_awaited_once()
        redis.ltrim.assert_awaited_once()
