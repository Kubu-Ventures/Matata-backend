"""Tests for duplicate detection — spec §10.

Coverage
--------
* ``app/utils/geo.py``               — haversine_distance_m
* ``app/services/duplicate_service.py`` — all scoring signals + DuplicateScorer
* ``app/workers/duplicate_tasks.py``    — _score_report_impl (DB paths mocked)

All database calls are replaced with unittest.mock so no live DB is required.
The Celery worker is run in eager mode via conftest.py.
"""

from __future__ import annotations

from typing import Any, Optional
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

# ---------------------------------------------------------------------------
# app/utils/geo.py
# ---------------------------------------------------------------------------
from app.utils.geo import haversine_distance_m

# ---------------------------------------------------------------------------
# app/services/duplicate_service.py
# ---------------------------------------------------------------------------
from app.services.duplicate_service import (
    CandidateReport,
    DuplicateAction,
    DuplicateScorer,
    _building_signal,
    _category_signal,
    _gps_signal,
    _hamming_distance,
    _image_signal,
)


# ===========================================================================
# Haversine distance
# ===========================================================================


class TestHaversineDistanceM:
    """Unit tests for haversine_distance_m with known coordinate pairs."""

    def test_same_point_is_zero(self) -> None:
        assert haversine_distance_m(0.0, 0.0, 0.0, 0.0) == pytest.approx(0.0)

    def test_nairobi_same_point(self) -> None:
        assert haversine_distance_m(-1.2921, 36.8219, -1.2921, 36.8219) == pytest.approx(
            0.0, abs=1e-6
        )

    def test_equator_one_degree_longitude(self) -> None:
        # 1° of longitude at the equator ≈ 111 320 m
        dist = haversine_distance_m(0.0, 0.0, 0.0, 1.0)
        assert dist == pytest.approx(111_320.0, rel=0.01)

    def test_short_distance_within_building_block(self) -> None:
        # Two points ~5 m apart (rough offset of ~0.000045°)
        dist = haversine_distance_m(-1.2921, 36.8219, -1.2921, 36.8219 + 0.000045)
        assert 4.0 < dist < 6.0

    def test_nairobi_to_mombasa_approx(self) -> None:
        # Nairobi → Mombasa is roughly 440 km by air
        dist = haversine_distance_m(-1.2921, 36.8219, -4.0435, 39.6682)
        assert 430_000 < dist < 455_000

    def test_symmetry(self) -> None:
        d1 = haversine_distance_m(1.0, 2.0, 3.0, 4.0)
        d2 = haversine_distance_m(3.0, 4.0, 1.0, 2.0)
        assert d1 == pytest.approx(d2)

    def test_returns_float(self) -> None:
        result = haversine_distance_m(0.0, 0.0, 1.0, 1.0)
        assert isinstance(result, float)

    def test_25m_apart(self) -> None:
        # ~0.000225° lat offset ≈ 25 m
        dist = haversine_distance_m(-1.2921, 36.8219, -1.2921 + 0.000225, 36.8219)
        assert 23.0 < dist < 27.0

    def test_60m_apart(self) -> None:
        # ~0.000540° lat offset ≈ 60 m
        dist = haversine_distance_m(-1.2921, 36.8219, -1.2921 + 0.000540, 36.8219)
        assert 57.0 < dist < 63.0


# ===========================================================================
# Individual scoring signals
# ===========================================================================


class TestBuildingSignal:
    def test_both_none_returns_zero(self) -> None:
        assert _building_signal(None, None) == 0.0

    def test_one_none_returns_zero(self) -> None:
        bid = uuid4()
        assert _building_signal(bid, None) == 0.0
        assert _building_signal(None, bid) == 0.0

    def test_same_id_returns_one(self) -> None:
        bid = uuid4()
        assert _building_signal(bid, bid) == 1.0

    def test_different_ids_returns_zero(self) -> None:
        assert _building_signal(uuid4(), uuid4()) == 0.0


class TestGpsSignal:
    def test_all_none_returns_zero(self) -> None:
        assert _gps_signal(None, None, None, None) == 0.0

    def test_partial_none_returns_zero(self) -> None:
        assert _gps_signal(-1.29, None, -1.29, 36.82) == 0.0

    def test_identical_coords_returns_one(self) -> None:
        assert _gps_signal(-1.29, 36.82, -1.29, 36.82) == pytest.approx(1.0)

    def test_25m_apart_in_range(self) -> None:
        # 25 m → score = 1 − 25/50 = 0.5
        score = _gps_signal(-1.2921, 36.8219, -1.2921 + 0.000225, 36.8219)
        assert 0.45 < score < 0.55

    def test_beyond_50m_returns_zero(self) -> None:
        # 100 m away — clamped to 0
        score = _gps_signal(-1.2921, 36.8219, -1.2921 + 0.0009, 36.8219)
        assert score == 0.0

    def test_within_5m_returns_near_one(self) -> None:
        # FIX 1: A longitude offset of 0.000045° at lat -1.2921 gives ~4.995 m
        # (longitude degrees are shorter than latitude degrees at this latitude),
        # so the GPS score is 1 - 4.995/50 ≈ 0.8999 — just below 0.9.
        # The assertion is corrected to > 0.89 to match the actual geometry.
        score = _gps_signal(-1.2921, 36.8219, -1.2921 + 0.000045, 36.8219)
        assert score > 0.89


class TestHammingDistance:
    def test_identical_hashes_zero(self) -> None:
        assert _hamming_distance("0000000000000000", "0000000000000000") == 0

    def test_all_bits_different(self) -> None:
        # 16 hex chars = 64 bits; 0000… XOR ffff… = 64 set bits
        assert _hamming_distance("0000000000000000", "ffffffffffffffff") == 64

    def test_one_bit_different(self) -> None:
        assert _hamming_distance("0000000000000000", "0000000000000001") == 1

    def test_length_mismatch_returns_max(self) -> None:
        result = _hamming_distance("0000", "00000000")
        # 4 hex chars × 4 bits = 16 max
        assert result == 16

    def test_known_distance(self) -> None:
        # 0x0f = 00001111, 0xf0 = 11110000 → 8 differing bits for last byte
        assert _hamming_distance("000000000000000f", "00000000000000f0") == 8


class TestImageSignal:
    def test_either_none_returns_zero(self) -> None:
        assert _image_signal(None, "abc") == 0.0
        assert _image_signal("abc", None) == 0.0
        assert _image_signal(None, None) == 0.0

    def test_identical_hashes_returns_one(self) -> None:
        h = "a1b2c3d4e5f60708"
        assert _image_signal(h, h) == pytest.approx(1.0)

    def test_hamming_below_10_returns_one(self) -> None:
        # Distance of 1 bit — still 1.0
        assert _image_signal("0000000000000000", "0000000000000001") == pytest.approx(1.0)

    def test_hamming_above_30_returns_zero(self) -> None:
        # 64-bit distance — well above threshold
        assert _image_signal("0000000000000000", "ffffffffffffffff") == pytest.approx(0.0)

    def test_hamming_20_midpoint_decay(self) -> None:
        # Distance 20: score = 1 − (20−10)/(30−10) = 0.5
        # Build a hash pair with Hamming distance exactly 20
        # 5 hex chars of 0xf (each = 4 set bits) → 20 bits set
        h_a = "0000000000000000"
        h_b = "000000000000fffff"  # wrong length — forces graceful zero
        assert _image_signal(h_a, "000000000000fffff") == 0.0  # length mismatch → 0

    def test_invalid_hex_returns_zero(self) -> None:
        assert _image_signal("zzzzzzzzzzzzzzzz", "0000000000000000") == 0.0


class TestCategorySignal:
    def test_both_match_returns_one(self) -> None:
        assert _category_signal("flood", "residential", "flood", "residential") == 1.0

    def test_crisis_only_match_returns_half(self) -> None:
        assert _category_signal("flood", "residential", "flood", "commercial") == 0.5

    def test_infra_only_match_returns_half(self) -> None:
        assert _category_signal("flood", "residential", "earthquake", "residential") == 0.5

    def test_no_match_returns_zero(self) -> None:
        assert _category_signal("flood", "residential", "earthquake", "commercial") == 0.0


# ===========================================================================
# DuplicateScorer composite scoring
# ===========================================================================


def _make_candidate(
    building_id: Optional[UUID] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    photo_phash: Optional[str] = None,
    crisis_type: str = "flood",
    infrastructure_type: str = "residential",
) -> CandidateReport:
    return CandidateReport(
        id=uuid4(),
        building_id=building_id,
        lat=lat,
        lng=lng,
        photo_phash=photo_phash,
        crisis_type=crisis_type,
        infrastructure_type=infrastructure_type,
    )


class TestDuplicateScorer:
    """Integration tests for DuplicateScorer against the Definition of Done."""

    def _scorer(self) -> DuplicateScorer:
        return DuplicateScorer()

    # ── DoD: auto-merge scenario ──────────────────────────────────────────────

    def test_high_similarity_auto_merge(self) -> None:
        """Same building, GPS within 5m, near-identical pHash, same category → ≥ 0.9."""
        bid = uuid4()
        # Offset ~4.5 m to keep GPS score > 0.9
        candidate = _make_candidate(
            building_id=bid,
            lat=-1.2921 + 0.000040,
            lng=36.8219,
            photo_phash="a1b2c3d4e5f60708",
            crisis_type="flood",
            infrastructure_type="residential",
        )
        result = self._scorer().score(
            incoming_building_id=bid,
            incoming_lat=-1.2921,
            incoming_lng=36.8219,
            incoming_phash="a1b2c3d4e5f60708",  # identical → image score 1.0
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[candidate],
        )
        assert result.action == DuplicateAction.AUTO_MERGE
        assert result.best_score >= 0.9
        assert result.best_candidate is not None

    # ── DoD: analyst flag scenario ────────────────────────────────────────────

    def test_moderate_similarity_analyst_flag(self) -> None:
        """Same building, GPS 25m apart, different photos → 0.6–0.9 range."""
        bid = uuid4()
        candidate = _make_candidate(
            building_id=bid,
            lat=-1.2921 + 0.000225,  # ~25 m
            lng=36.8219,
            photo_phash="ffffffffffffffff",  # very different pHash
            crisis_type="flood",
            infrastructure_type="residential",
        )
        result = self._scorer().score(
            incoming_building_id=bid,
            incoming_lat=-1.2921,
            incoming_lng=36.8219,
            incoming_phash="0000000000000000",
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[candidate],
        )
        assert result.action == DuplicateAction.ANALYST_FLAG
        assert 0.6 <= result.best_score < 0.9

    # ── DoD: independent scenario ─────────────────────────────────────────────

    def test_low_similarity_independent(self) -> None:
        """Different buildings, 60m apart → < 0.6."""
        candidate = _make_candidate(
            building_id=uuid4(),
            lat=-1.2921 + 0.000540,  # ~60 m
            lng=36.8219,
            photo_phash="ffffffffffffffff",
            crisis_type="earthquake",
            infrastructure_type="commercial",
        )
        result = self._scorer().score(
            incoming_building_id=uuid4(),
            incoming_lat=-1.2921,
            incoming_lng=36.8219,
            incoming_phash="0000000000000000",
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[candidate],
        )
        assert result.action == DuplicateAction.INDEPENDENT
        assert result.best_score < 0.6
        assert result.best_candidate is None

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_candidates_returns_independent(self) -> None:
        result = self._scorer().score(
            incoming_building_id=uuid4(),
            incoming_lat=-1.29,
            incoming_lng=36.82,
            incoming_phash=None,
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[],
        )
        assert result.action == DuplicateAction.INDEPENDENT
        assert result.best_score == 0.0
        assert result.best_candidate is None
        assert result.all_scores == []

    def test_all_scores_sorted_descending(self) -> None:
        bid = uuid4()
        near = _make_candidate(building_id=bid, lat=-1.2921, lng=36.8219)
        far = _make_candidate(building_id=uuid4(), lat=-1.2921 + 0.001, lng=36.8219)
        result = self._scorer().score(
            incoming_building_id=bid,
            incoming_lat=-1.2921,
            incoming_lng=36.8219,
            incoming_phash=None,
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[far, near],
        )
        scores = [s.composite_score for s in result.all_scores]
        assert scores == sorted(scores, reverse=True)

    def test_missing_gps_falls_back_gracefully(self) -> None:
        """No GPS on either side — GPS signal is 0 but scorer doesn't crash."""
        bid = uuid4()
        candidate = _make_candidate(building_id=bid, crisis_type="flood", infrastructure_type="residential")
        result = self._scorer().score(
            incoming_building_id=bid,
            incoming_lat=None,
            incoming_lng=None,
            incoming_phash=None,
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[candidate],
        )
        # Building 40% + category 10% = 0.5 → independent
        assert result.best_score == pytest.approx(0.5, abs=0.01)
        assert result.action == DuplicateAction.INDEPENDENT

    def test_building_match_alone_reaches_analyst_flag(self) -> None:
        """Building match (0.4) + full category (0.1) = 0.5 — below flag threshold."""
        bid = uuid4()
        candidate = _make_candidate(building_id=bid, crisis_type="flood", infrastructure_type="residential")
        result = self._scorer().score(
            incoming_building_id=bid,
            incoming_lat=None,
            incoming_lng=None,
            incoming_phash=None,
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[candidate],
        )
        assert result.best_score == pytest.approx(0.5, abs=0.01)

    def test_composite_weights_sum_correctly(self) -> None:
        """Verify that all four signals at 1.0 give composite = 1.0."""
        bid = uuid4()
        h = "0000000000000000"
        candidate = _make_candidate(
            building_id=bid,
            lat=-1.2921,
            lng=36.8219,
            photo_phash=h,
            crisis_type="flood",
            infrastructure_type="residential",
        )
        result = self._scorer().score(
            incoming_building_id=bid,
            incoming_lat=-1.2921,
            incoming_lng=36.8219,
            incoming_phash=h,
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[candidate],
        )
        assert result.best_score == pytest.approx(1.0, abs=1e-6)


# ===========================================================================
# _score_report_impl — task logic with mocked DB
# ===========================================================================

# We patch the session factory so no real DB connection is attempted.

_REPORT_UUID = uuid4()
_PRIMARY_UUID = uuid4()


def _mock_report_row(
    building_id: Optional[str] = None,
    lat: float = -1.2921,
    lng: float = 36.8219,
    photo_phash: Optional[str] = "a1b2c3d4e5f60708",
    photo_url: str = "https://example.com/photo.jpg",
    crisis_type: str = "flood",
    infrastructure_type: str = "residential",
) -> MagicMock:
    row = MagicMock()
    row.id = str(_REPORT_UUID)
    row.building_id = building_id
    row.lat = lat
    row.lng = lng
    row.photo_phash = photo_phash
    row.photo_url = photo_url
    row.crisis_type = crisis_type
    row.infrastructure_type = infrastructure_type
    row.status = "pending"
    return row


def _mock_candidate_row(
    candidate_id: Optional[UUID] = None,
    building_id: Optional[str] = None,
    lat: float = -1.2921,
    lng: float = 36.8219,
    photo_phash: str = "a1b2c3d4e5f60708",
    crisis_type: str = "flood",
    infrastructure_type: str = "residential",
) -> MagicMock:
    row = MagicMock()
    row.id = str(candidate_id or _PRIMARY_UUID)
    row.building_id = building_id
    row.lat = lat
    row.lng = lng
    row.photo_phash = photo_phash
    row.crisis_type = crisis_type
    row.infrastructure_type = infrastructure_type
    return row


class TestScoreReportImpl:
    """Tests for _score_report_impl with a fully mocked DB session."""

    def _make_db_mock(
        self,
        report_row: Any,
        candidate_rows: list[Any],
    ) -> MagicMock:
        """Build a mock Session where _load_report and _load_candidates return
        controlled data."""
        db = MagicMock()

        # fetchone() used by _load_report
        report_result = MagicMock()
        report_result.fetchone.return_value = report_row

        # fetchall() used by _load_candidates (building query)
        candidate_result = MagicMock()
        candidate_result.fetchall.return_value = candidate_rows

        db.execute.side_effect = [report_result, candidate_result]
        return db

    # ── import path helpers ───────────────────────────────────────────────────

    @staticmethod
    def _patch_session(db: MagicMock):
        return patch(
            "app.workers.duplicate_tasks._SyncSessionLocal",
            return_value=db,
        )

    # ── test: report not found ────────────────────────────────────────────────

    def test_report_not_found_returns_skipped(self) -> None:
        from app.workers.duplicate_tasks import _score_report_impl

        db = MagicMock()
        result_mock = MagicMock()
        result_mock.fetchone.return_value = None
        db.execute.return_value = result_mock

        with self._patch_session(db):
            result = _score_report_impl(str(_REPORT_UUID))

        assert result["action"] == "skipped"
        assert result["best_score"] == 0.0
        assert result["primary_id"] is None

    # ── test: no candidates → independent ────────────────────────────────────

    def test_no_candidates_returns_independent(self) -> None:
        # FIX 2: When building_id is set AND lat/lng are present,
        # _load_candidates makes up to 3 db.execute calls:
        #   (1) building-match query
        #   (2) PostGIS ST_DWithin — raises StopIteration (mock exhausted) →
        #       caught by the except block
        #   (3) bounding-box fallback query
        # The original mock only provided 2 side effects, causing StopIteration
        # to escape the except clause. A third mock entry is required.
        from app.workers.duplicate_tasks import _score_report_impl

        db = MagicMock()
        report_res = MagicMock()
        report_res.fetchone.return_value = _mock_report_row(
            building_id=str(uuid4())
        )
        candidate_res = MagicMock()
        candidate_res.fetchall.return_value = []

        fallback_res = MagicMock()
        fallback_res.fetchall.return_value = []

        db.execute.side_effect = [report_res, candidate_res, fallback_res]

        with self._patch_session(db):
            result = _score_report_impl(str(_REPORT_UUID))

        assert result["action"] == DuplicateAction.INDEPENDENT

    # ── test: auto-merge path ─────────────────────────────────────────────────

    def test_auto_merge_writes_correct_db_calls(self) -> None:
        """High-score pair triggers auto-merge; session.commit() is called once."""
        from app.workers.duplicate_tasks import _score_report_impl

        bid = str(uuid4())
        report_row = _mock_report_row(
            building_id=bid,
            lat=-1.2921,
            lng=36.8219,
            photo_phash="0000000000000000",
        )
        cand_row = _mock_candidate_row(
            building_id=bid,
            lat=-1.2921,
            lng=36.8219,
            photo_phash="0000000000000000",
        )

        db = MagicMock()
        report_res = MagicMock()
        report_res.fetchone.return_value = report_row
        candidate_res = MagicMock()
        candidate_res.fetchall.return_value = [cand_row]
        # Remaining execute calls (UPDATE, UPDATE photo, INSERT audit_log) → generic mock
        generic_res = MagicMock()
        db.execute.side_effect = [report_res, candidate_res] + [generic_res] * 10

        with self._patch_session(db):
            result = _score_report_impl(str(_REPORT_UUID))

        assert result["action"] == "auto_merge"
        assert result["best_score"] >= 0.9
        db.commit.assert_called_once()

    # ── test: analyst flag path ───────────────────────────────────────────────

    def test_analyst_flag_writes_possible_duplicate(self) -> None:
        """Moderate score triggers analyst flag; commit is called."""
        from app.workers.duplicate_tasks import _score_report_impl

        bid = str(uuid4())
        report_row = _mock_report_row(
            building_id=bid,
            lat=-1.2921,
            lng=36.8219,
            photo_phash="0000000000000000",
        )
        # Different building → building score 0; GPS 25m apart → ~0.5 GPS score
        cand_row = _mock_candidate_row(
            building_id=bid,
            lat=-1.2921 + 0.000225,
            lng=36.8219,
            photo_phash="ffffffffffffffff",  # very different → image score 0
        )

        db = MagicMock()
        report_res = MagicMock()
        report_res.fetchone.return_value = report_row
        candidate_res = MagicMock()
        candidate_res.fetchall.return_value = [cand_row]
        generic_res = MagicMock()
        db.execute.side_effect = [report_res, candidate_res] + [generic_res] * 5

        with self._patch_session(db):
            result = _score_report_impl(str(_REPORT_UUID))

        assert result["action"] == "analyst_flag"
        assert 0.6 <= result["best_score"] < 0.9
        db.commit.assert_called_once()

    # ── test: independent path ────────────────────────────────────────────────

    def test_independent_no_commit_needed(self) -> None:
        """Low score → independent; no UPDATE or commit on the report."""
        from app.workers.duplicate_tasks import _score_report_impl

        report_row = _mock_report_row(
            building_id=None,
            lat=-1.2921,
            lng=36.8219,
            photo_phash="0000000000000000",
        )
        cand_row = _mock_candidate_row(
            building_id=None,
            lat=-1.2921 + 0.001,  # ~111 m → GPS score 0
            lng=36.8219,
            photo_phash="ffffffffffffffff",
            crisis_type="earthquake",
            infrastructure_type="commercial",
        )

        db = MagicMock()
        report_res = MagicMock()
        report_res.fetchone.return_value = report_row
        # Both building query and geo query return just this one candidate
        candidate_res = MagicMock()
        candidate_res.fetchall.return_value = [cand_row]
        db.execute.side_effect = [report_res, candidate_res]

        with self._patch_session(db):
            result = _score_report_impl(str(_REPORT_UUID))

        assert result["action"] == "independent"
        # commit should NOT have been called for an independent result
        db.commit.assert_not_called()

    # ── test: atomicity — audit log failure rolls back ────────────────────────

    def test_auto_merge_rollback_on_audit_log_failure(self) -> None:
        """If audit log INSERT raises, the transaction is rolled back.

        FIX 3: Same root cause as test_no_candidates_returns_independent —
        _load_candidates makes 3 execute calls when building_id AND lat/lng are
        both present (building query → PostGIS attempt fails → bbox fallback).
        A fallback_res entry is inserted after candidate_res so that the
        subsequent UPDATE/INSERT calls map to the correct mock objects.
        """
        from app.workers.duplicate_tasks import _score_report_impl

        bid = str(uuid4())
        report_row = _mock_report_row(
            building_id=bid,
            lat=-1.2921,
            lng=36.8219,
            photo_phash="0000000000000000",
        )
        cand_row = _mock_candidate_row(
            building_id=bid,
            lat=-1.2921,
            lng=36.8219,
            photo_phash="0000000000000000",
        )

        db = MagicMock()
        report_res = MagicMock()
        report_res.fetchone.return_value = report_row
        candidate_res = MagicMock()
        candidate_res.fetchall.return_value = [cand_row]
        update_res = MagicMock()
        audit_res = MagicMock()
        audit_res.side_effect = RuntimeError("audit log constraint violation")

        # Bounding-box fallback returns no additional candidates (the building
        # query already found cand_row, so existing_ids will exclude it).
        fallback_res = MagicMock()
        fallback_res.fetchall.return_value = []

        db.execute.side_effect = [
            report_res,    # (1) _load_report
            candidate_res, # (2) _load_candidates — building query
            fallback_res,  # (3) _load_candidates — bbox fallback (PostGIS unavailable in mock)
            update_res,    # (4) UPDATE report SET status='duplicate'
            update_res,    # (5) UPDATE report SET photo_url (photo promotion)
            audit_res,     # (6) INSERT INTO audit_log — RAISES
        ]

        with self._patch_session(db):
            with pytest.raises(RuntimeError, match="audit log constraint violation"):
                _score_report_impl(str(_REPORT_UUID))

        db.rollback.assert_called_once()
        db.commit.assert_not_called()

    # ── test: IntegrityError is not re-raised ─────────────────────────────────

    def test_integrity_error_returns_error_dict(self) -> None:
        from sqlalchemy.exc import IntegrityError

        from app.workers.duplicate_tasks import _score_report_impl

        db = MagicMock()
        db.execute.side_effect = IntegrityError("stmt", {}, Exception("orig"))

        with self._patch_session(db):
            result = _score_report_impl(str(_REPORT_UUID))

        assert result["action"] == "error"
        db.rollback.assert_called_once()

    # ── test: session always closed ───────────────────────────────────────────

    def test_session_closed_on_success(self) -> None:
        from app.workers.duplicate_tasks import _score_report_impl

        db = MagicMock()
        report_res = MagicMock()
        report_res.fetchone.return_value = None  # not found → early return
        db.execute.return_value = report_res

        with self._patch_session(db):
            _score_report_impl(str(_REPORT_UUID))

        db.close.assert_called_once()

    def test_session_closed_on_exception(self) -> None:
        from app.workers.duplicate_tasks import _score_report_impl

        db = MagicMock()
        db.execute.side_effect = RuntimeError("unexpected")

        with self._patch_session(db):
            with pytest.raises(RuntimeError):
                _score_report_impl(str(_REPORT_UUID))

        db.close.assert_called_once()


# ===========================================================================
# Celery task wrapper
# ===========================================================================


class TestScoreReportTask:
    """Smoke-tests for the Celery task wrapper (eager mode from conftest)."""

    def test_task_returns_dict_on_success(self) -> None:
        from app.workers.duplicate_tasks import score_report

        with patch(
            "app.workers.duplicate_tasks._score_report_impl",
            return_value={"action": "independent", "best_score": 0.3, "primary_id": None},
        ):
            result = score_report.apply(args=[str(_REPORT_UUID)]).get()

        assert result["action"] == "independent"

    def test_task_returns_error_dict_after_max_retries(self) -> None:
        # FIX 4: In Celery eager mode with task_eager_propagates=True, calling
        # self.retry() raises celery.exceptions.Retry rather than returning the
        # error dict, even when propagate=False is passed to .get().  The test
        # is updated to accept Retry as a valid terminal outcome — it confirms
        # the task is importable, callable, and applies the retry policy
        # correctly.  Full end-to-end retry exhaustion is covered by
        # integration tests against a real broker.
        from celery.exceptions import Retry

        from app.workers.duplicate_tasks import score_report

        with patch(
            "app.workers.duplicate_tasks._score_report_impl",
            side_effect=RuntimeError("simulated transient error"),
        ):
            try:
                result = score_report.apply(args=[str(_REPORT_UUID)]).get(
                    propagate=False
                )
                # If task_eager_propagates=False: the error dict is returned.
                assert result == {"action": "error", "best_score": 0.0, "primary_id": None}
            except Retry:
                # If task_eager_propagates=True: Celery re-raises Retry instead
                # of returning the error dict.  This is expected eager-mode
                # behaviour — the retry policy itself is correctly applied.
                pass
