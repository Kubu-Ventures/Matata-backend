"""Tests for the GIS worker implementation.

Covers:
* ``GISService`` matching sequence (all four paths)
* ``GeocodingService`` factory and providers
* ``match_building`` Celery task
* ``GET /api/v1/gis/building/match`` endpoint
* ``import_footprints`` CLI command

All tests are pure unit tests — no real database, Redis, or network required.
PostGIS query logic is tested via a mock SQLAlchemy session.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_BUILDING_ID = uuid4()
_BUILDING_ID_STR = str(_BUILDING_ID)
_FOOTPRINT_GEOJSON = (
    '{"type":"Polygon","coordinates":[[[36.8,-1.3],'
    "[36.81,-1.3],[36.81,-1.29],"
    "[36.8,-1.29],[36.8,-1.3]]]}"
)


def _make_row(
    building_id=_BUILDING_ID, footprint_geojson=_FOOTPRINT_GEOJSON, distance_m=10.0
):
    """Return a MagicMock that behaves like a SQLAlchemy Row."""
    row = MagicMock()
    row.id = str(building_id)
    row.footprint_geojson = footprint_geojson
    row.distance_m = distance_m
    return row


def _make_db(fetchone_return=None):
    """Return a mock synchronous SQLAlchemy Session."""
    db = MagicMock()
    execute_result = MagicMock()
    execute_result.fetchone.return_value = fetchone_return
    db.execute.return_value = execute_result
    return db


# ===========================================================================
# GISService
# ===========================================================================


class TestGISServicePointInPolygon:
    """Step 1 — exact polygon match."""

    def test_returns_confidence_1_on_polygon_match(self):
        from app.services.gis_service import GISService

        row = _make_row()
        db = _make_db(fetchone_return=row)

        svc = GISService(db)
        result = svc._point_in_polygon(lat=-1.295, lng=36.805)

        assert result is not None
        assert result.confidence == 1.0
        assert result.building_id == _BUILDING_ID
        assert result.distance_m is None

    def test_returns_none_when_no_polygon_contains_point(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        svc = GISService(db)
        result = svc._point_in_polygon(lat=-1.3, lng=36.9)
        assert result is None

    def test_query_uses_parameterised_sql(self):
        """Coordinates must appear as bound parameters, never string-interpolated."""
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        svc = GISService(db)
        svc._point_in_polygon(lat=-1.295, lng=36.805)

        # The execute call should pass a dict with lat/lng keys.
        call_args = db.execute.call_args
        params = call_args[0][1]  # second positional arg to execute()
        assert "lat" in params
        assert "lng" in params
        assert params["lat"] == -1.295
        assert params["lng"] == 36.805


class TestGISServiceNearestNeighbour:
    """Step 2 — nearest-centroid within radius."""

    def test_returns_correct_confidence_for_distance(self):
        from app.services.gis_service import GISService

        row = _make_row(distance_m=15.0)
        db = _make_db(fetchone_return=row)

        svc = GISService(db)
        # Default radius = 30 m.  Distance = 15 m → confidence = 1 - 15/30 = 0.5
        result = svc._nearest_neighbour(lat=-1.295, lng=36.805, search_radius_m=30.0)

        assert result is not None
        assert abs(result.confidence - 0.5) < 0.001
        assert result.distance_m == pytest.approx(15.0)

    def test_returns_none_at_radius_boundary(self):
        from app.services.gis_service import GISService

        # distance == radius → confidence = 0.0 → treated as no match
        row = _make_row(distance_m=30.0)
        db = _make_db(fetchone_return=row)

        svc = GISService(db)
        result = svc._nearest_neighbour(lat=-1.295, lng=36.805, search_radius_m=30.0)

        assert result is None

    def test_returns_none_when_no_building_in_radius(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        svc = GISService(db)
        result = svc._nearest_neighbour(lat=-1.295, lng=36.805, search_radius_m=30.0)
        assert result is None

    def test_query_uses_parameterised_sql(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        svc = GISService(db)
        svc._nearest_neighbour(lat=-1.295, lng=36.805, search_radius_m=30.0)

        call_args = db.execute.call_args
        params = call_args[0][1]
        assert "lat" in params and "lng" in params and "radius_m" in params
        assert params["lat"] == -1.295
        assert params["lng"] == 36.805
        assert params["radius_m"] == 30.0


class TestGISServiceComputeSearchRadius:
    """Dynamic radius computation."""

    def test_default_radius_when_accuracy_none(self):
        from app.services.gis_service import GISService

        radius = GISService._compute_search_radius(None)
        assert radius == pytest.approx(30.0)

    def test_default_radius_when_accuracy_below_threshold(self):
        from app.services.gis_service import GISService

        radius = GISService._compute_search_radius(49.9)
        assert radius == pytest.approx(30.0)

    def test_expanded_radius_when_accuracy_exceeds_50m(self):
        from app.services.gis_service import GISService

        radius = GISService._compute_search_radius(60.0)
        assert radius == pytest.approx(60.0 * 1.5)

    def test_radius_capped_at_100m(self):
        from app.services.gis_service import GISService

        radius = GISService._compute_search_radius(200.0)
        assert radius == pytest.approx(100.0)


class TestGISServiceMatchBuilding:
    """Full matching sequence integration."""

    def test_step1_polygon_match_short_circuits(self):
        """Point-in-polygon match skips steps 2–4 (only the probability pool
        query follows it)."""
        from app.services.gis_service import GISService

        row = _make_row()
        db = _make_db(fetchone_return=row)
        svc = GISService(db)

        result = svc.match_building(lat=-1.295, lng=36.805)

        # No buildings in the probability pool (mock) → heuristic 1.0 stays.
        assert result.confidence == 1.0
        assert result.building_id == _BUILDING_ID
        # Point-in-polygon, then the probability pool; no nearest-neighbour.
        assert db.execute.call_count == 2
        assert "limit" in db.execute.call_args[0][1]

    def test_step2_nearest_neighbour_when_no_polygon_match(self):
        """When step 1 fails, step 2 should run."""
        from app.services.gis_service import GISService

        # First call (point-in-polygon) returns None; second call (NN) returns a row.
        nn_row = _make_row(distance_m=20.0)
        execute_result_none = MagicMock()
        execute_result_none.fetchone.return_value = None
        execute_result_nn = MagicMock()
        execute_result_nn.fetchone.return_value = nn_row

        execute_result_pool = MagicMock()
        execute_result_pool.fetchall.return_value = []

        db = MagicMock()
        db.execute.side_effect = [
            execute_result_none,
            execute_result_nn,
            execute_result_pool,
        ]

        svc = GISService(db)
        result = svc.match_building(lat=-1.295, lng=36.805)

        assert result.building_id == _BUILDING_ID
        assert 0.0 < result.confidence < 1.0

    def test_step4_unmapped_when_all_steps_fail(self):
        """No GPS, no landmark → unmapped result."""
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        svc = GISService(db)

        result = svc.match_building(lat=None, lng=None)

        assert result.building_id is None
        assert result.confidence == 0.0

    def test_landmark_path_caps_confidence_at_0_5(self):
        """Landmark geocoding path must cap confidence at 0.5."""
        from app.services.geocoding_service import MockGeocodingProvider
        from app.services.gis_service import GISService

        # NN would return confidence > 0.5 (distance very close)
        nn_row = _make_row(distance_m=1.0)
        execute_result = MagicMock()
        execute_result.fetchone.return_value = nn_row
        db = MagicMock()
        db.execute.return_value = execute_result

        svc = GISService(db)
        geocoding = MockGeocodingProvider(result=(-1.295, 36.805))

        result = svc.match_building(
            lat=None,
            lng=None,
            landmark_description="near central market",
            geocoding_provider=geocoding,
        )

        assert result.confidence <= 0.5

    def test_unmapped_when_geocoding_fails(self):
        """If geocoding raises GeocodingError, fall through to unmapped."""
        from app.services.geocoding_service import MockGeocodingProvider
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        svc = GISService(db)
        geocoding = MockGeocodingProvider(raise_error=True)

        result = svc.match_building(
            lat=None,
            lng=None,
            landmark_description="somewhere",
            geocoding_provider=geocoding,
        )

        assert result.building_id is None


def _candidate_row(building_id, distance_m, external_id):
    row = _make_row(building_id=building_id, distance_m=distance_m)
    row.external_id = external_id
    return row


def _result(fetchone=None, fetchall=None):
    result = MagicMock()
    result.fetchone.return_value = fetchone
    result.fetchall.return_value = fetchall or []
    return result


class TestGISServiceCandidates:
    """Top-3 nearest buildings returned alongside the match."""

    def test_candidates_map_rows_nearest_first(self):
        from app.services.gis_service import MAX_CANDIDATES, GISService

        ids = [uuid4(), uuid4()]
        db = MagicMock()
        db.execute.return_value = _result(
            fetchall=[
                _candidate_row(ids[0], 0.0, "osm:way/1"),
                _candidate_row(ids[1], 3.5, "osm:way/2"),
            ]
        )

        candidates = GISService(db)._candidates(-1.295, 36.805, 30.0)

        assert [c.building_id for c in candidates] == ids
        assert [c.external_id for c in candidates] == ["osm:way/1", "osm:way/2"]
        assert candidates[1].distance_m == pytest.approx(3.5)
        params = db.execute.call_args[0][1]
        assert params == {
            "lat": -1.295,
            "lng": 36.805,
            "radius_m": 30.0,
            "limit": MAX_CANDIDATES,
        }

    def test_not_returned_unless_requested(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=_make_row())
        result = GISService(db).match_building(lat=-1.295, lng=36.805)

        # The pool is still queried (for the confidence) but not returned.
        assert result.candidates == []
        assert db.execute.call_count == 2

    def test_polygon_match_carries_candidates(self):
        from app.services.gis_service import GISService

        neighbour = uuid4()
        db = MagicMock()
        db.execute.side_effect = [
            _result(fetchone=_make_row()),
            _result(
                fetchall=[
                    _candidate_row(_BUILDING_ID, 0.0, "osm:way/1"),
                    _candidate_row(neighbour, 2.0, "osm:way/2"),
                ]
            ),
        ]

        result = GISService(db).match_building(
            lat=-1.295, lng=36.805, accuracy_m=60.0, with_candidates=True
        )

        assert [c.building_id for c in result.candidates] == [_BUILDING_ID, neighbour]
        # A neighbour 2 m away under a 60 m fix is almost as likely: the
        # confidence is a probability (~0.5), not a certain 1.0.
        assert result.confidence == pytest.approx(0.5, abs=0.01)
        assert result.confidence == result.candidates[0].probability
        assert sum(c.probability for c in result.candidates) == pytest.approx(1.0)
        # Candidates use the same accuracy-expanded radius as step 2.
        assert db.execute.call_args[0][1]["radius_m"] == pytest.approx(90.0)

    def test_nearest_neighbour_match_carries_candidates(self):
        from app.services.gis_service import GISService

        db = MagicMock()
        db.execute.side_effect = [
            _result(fetchone=None),
            _result(fetchone=_make_row(distance_m=4.0)),
            _result(fetchall=[_candidate_row(_BUILDING_ID, 4.0, "osm:way/1")]),
        ]

        result = GISService(db).match_building(
            lat=-1.295, lng=36.805, with_candidates=True
        )

        assert result.building_id == _BUILDING_ID
        assert result.candidates[0].building_id == result.building_id

    def test_unmapped_has_no_candidates(self):
        from app.services.gis_service import GISService

        db = MagicMock()
        db.execute.side_effect = [_result(fetchone=None), _result(fetchone=None)]

        result = GISService(db).match_building(
            lat=-1.295, lng=36.805, with_candidates=True
        )

        assert result.building_id is None
        assert result.candidates == []
        assert db.execute.call_count == 2

    def test_landmark_candidates_use_geocoded_point(self):
        from app.services.geocoding_service import MockGeocodingProvider
        from app.services.gis_service import GISService

        db = MagicMock()
        db.execute.side_effect = [
            _result(fetchone=_make_row(distance_m=1.0)),
            _result(fetchall=[_candidate_row(_BUILDING_ID, 1.0, "osm:way/1")]),
        ]

        result = GISService(db).match_building(
            lat=None,
            lng=None,
            landmark_description="near central market",
            geocoding_provider=MockGeocodingProvider(result=(-1.28, 36.82)),
            with_candidates=True,
        )

        assert result.confidence <= 0.5
        assert len(result.candidates) == 1
        params = db.execute.call_args[0][1]
        assert (params["lat"], params["lng"], params["radius_m"]) == (
            -1.28,
            36.82,
            100.0,
        )


class TestMatchProbabilities:
    """Distance-based probabilities that replace the old 1 - d/r score."""

    def test_single_building_takes_all(self):
        from app.services.gis_service import GISService

        assert GISService._probabilities([4.0], 6.9) == [pytest.approx(1.0)]

    def test_equal_distances_split_evenly(self):
        from app.services.gis_service import GISService

        probs = GISService._probabilities([1.0, 1.0], 6.9)
        assert probs == [pytest.approx(0.5), pytest.approx(0.5)]

    def test_nearer_building_is_more_likely(self):
        from app.services.gis_service import GISService

        probs = GISService._probabilities([0.0, 3.0, 12.0], 6.9)
        assert probs[0] > probs[1] > probs[2]
        assert sum(probs) == pytest.approx(1.0)

    def test_tight_fix_separates_neighbours_more(self):
        from app.services.gis_service import GISService

        loose = GISService._probabilities([0.0, 3.0], GISService._sigma_m(30.0))
        tight = GISService._probabilities([0.0, 3.0], GISService._sigma_m(3.0))
        assert tight[0] > loose[0]

    def test_sigma_from_reported_accuracy_plus_map_error(self):
        import math

        from app.services.gis_service import GISService

        assert GISService._sigma_m(10.0) == pytest.approx(math.hypot(10.0 / 1.515, 2.0))

    def test_default_accuracy_when_missing(self):
        from app.core.config import settings
        from app.services.gis_service import GISService

        assert GISService._sigma_m(None) == GISService._sigma_m(
            settings.GPS_DEFAULT_ACCURACY_M
        )

    def test_empty_pool(self):
        from app.services.gis_service import GISService

        assert GISService._probabilities([], 5.0) == []


class TestGISServiceConfirmBuilding:
    """Validation of the building a reporter picked on the form."""

    def test_accepts_building_within_radius(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=_make_row(distance_m=3.0))
        match = GISService(db).confirm_building(_BUILDING_ID, -1.295, 36.805)

        assert match is not None
        assert match.building_id == _BUILDING_ID
        assert match.confidence == 1.0
        assert match.distance_m == pytest.approx(3.0)
        params = db.execute.call_args[0][1]
        assert params["building_id"] == _BUILDING_ID_STR
        assert params["radius_m"] == pytest.approx(30.0)

    def test_rejects_missing_or_distant_building(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        assert GISService(db).confirm_building(_BUILDING_ID, -1.295, 36.805) is None

    def test_uses_accuracy_expanded_radius(self):
        from app.services.gis_service import GISService

        db = _make_db(fetchone_return=None)
        GISService(db).confirm_building(_BUILDING_ID, -1.295, 36.805, accuracy_m=60.0)
        assert db.execute.call_args[0][1]["radius_m"] == pytest.approx(90.0)


class _FakeRedis:
    """Minimal in-memory sync Redis: get/set(nx, px, ex)/pttl."""

    def __init__(self):
        self.store = {}
        self.sets = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, px=None, ex=None):
        self.sets.append((key, value, nx, px, ex))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def pttl(self, key):
        return 1


class TestCachedGeocodingProvider:
    def _provider(self, inner):
        from app.services.geocoding_service import CachedGeocodingProvider

        return CachedGeocodingProvider(inner, "nominatim", _FakeRedis())

    def test_second_lookup_is_served_from_cache(self):
        from app.services.geocoding_service import MockGeocodingProvider

        inner = MockGeocodingProvider(result=(-1.31, 36.79))
        cached = self._provider(inner)

        assert cached.geocode("Olympic  Primary School") == (-1.31, 36.79)
        # Normalised: case and whitespace don't create a second entry.
        assert cached.geocode("olympic primary school") == (-1.31, 36.79)
        assert len(inner.calls) == 1

    def test_stores_only_a_hash_of_the_text(self):
        from app.services.geocoding_service import MockGeocodingProvider

        cached = self._provider(MockGeocodingProvider())
        cached.geocode("near Mama Njeri's kiosk")
        key = next(iter(cached._redis.store))
        assert "kiosk" not in key and "njeri" not in key.lower()
        assert len(key.rsplit(":", 1)[-1]) == 64  # sha256 hex

    def test_caches_no_result_with_shorter_ttl(self):
        from app.core.config import settings
        from app.services.geocoding_service import MockGeocodingProvider

        inner = MockGeocodingProvider()
        inner.result = None
        cached = self._provider(inner)

        assert cached.geocode("nowhere") is None
        assert cached.geocode("nowhere") is None
        assert len(inner.calls) == 1
        assert cached._redis.sets[-1][4] == settings.GEOCODING_CACHE_MISS_TTL_S

    def test_errors_are_not_cached(self):
        from app.services.geocoding_service import (
            GeocodingError,
            MockGeocodingProvider,
        )

        cached = self._provider(MockGeocodingProvider(raise_error=True))
        with pytest.raises(GeocodingError):
            cached.geocode("market")
        assert cached._redis.store == {}


class TestNominatimRateLimit:
    def test_uses_shared_one_second_slot(self, monkeypatch):
        from app.services import geocoding_service

        fake = _FakeRedis()
        provider = geocoding_service.NominatimGeocodingProvider(redis_client=fake)
        response = MagicMock()
        response.json.return_value = [{"lat": "-1.3", "lon": "36.8"}]
        monkeypatch.setattr(
            geocoding_service.httpx, "get", MagicMock(return_value=response)
        )

        assert provider.geocode("market") == (-1.3, 36.8)
        key, _, nx, px, _ = fake.sets[0]
        assert key == geocoding_service._NOMINATIM_SLOT_KEY
        assert nx is True and px == 1000

    def test_waits_while_another_worker_holds_the_slot(self, monkeypatch):
        from app.services import geocoding_service

        fake = _FakeRedis()
        fake.store[geocoding_service._NOMINATIM_SLOT_KEY] = "1"  # held elsewhere
        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)
            fake.store.pop(geocoding_service._NOMINATIM_SLOT_KEY, None)  # expires

        monkeypatch.setattr(geocoding_service.time, "sleep", fake_sleep)
        provider = geocoding_service.NominatimGeocodingProvider(redis_client=fake)
        provider._wait_for_slot()
        assert len(sleeps) == 1

    def test_user_agent_identifies_matata_with_contact(self, monkeypatch):
        from app.services import geocoding_service

        monkeypatch.setattr(
            geocoding_service.settings, "GEOCODING_CONTACT", "ops@example.org"
        )
        ua = geocoding_service._nominatim_headers()["User-Agent"]
        assert ua.startswith("Matata/") and "ops@example.org" in ua


# ===========================================================================
# GeocodingService
# ===========================================================================


class TestMockGeocodingProvider:
    def test_returns_default_nairobi_coordinates(self):
        from app.services.geocoding_service import MockGeocodingProvider

        provider = MockGeocodingProvider()
        result = provider.geocode("central market")
        assert result == (-1.2921, 36.8219)

    def test_records_queries(self):
        from app.services.geocoding_service import MockGeocodingProvider

        provider = MockGeocodingProvider()
        provider.geocode("market")
        provider.geocode("hospital")
        assert provider.calls == ["market", "hospital"]

    def test_raise_error_flag(self):
        from app.services.geocoding_service import GeocodingError, MockGeocodingProvider

        provider = MockGeocodingProvider(raise_error=True)
        with pytest.raises(GeocodingError):
            provider.geocode("anywhere")

    def test_custom_result(self):
        from app.services.geocoding_service import MockGeocodingProvider

        provider = MockGeocodingProvider(result=(-0.5, 37.0))
        lat, lng = provider.geocode("somewhere")
        assert lat == pytest.approx(-0.5)
        assert lng == pytest.approx(37.0)


class TestGeocodingFactory:
    def test_returns_nominatim_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "app.core.config.settings",
            MagicMock(GEOCODING_PROVIDER="nominatim"),
        )
        from app.services import geocoding_service

        monkeypatch.setattr(
            geocoding_service.settings, "GEOCODING_PROVIDER", "nominatim"
        )
        provider = geocoding_service.get_geocoding_provider()
        # With Redis configured, the real provider sits behind the cache.
        assert isinstance(provider, geocoding_service.CachedGeocodingProvider)
        assert isinstance(provider._inner, geocoding_service.NominatimGeocodingProvider)

    def test_no_cache_without_redis(self, monkeypatch):
        from app.services import geocoding_service

        monkeypatch.setattr(
            geocoding_service.settings, "GEOCODING_PROVIDER", "nominatim"
        )
        monkeypatch.setattr(geocoding_service.settings, "REDIS_URL", "")
        provider = geocoding_service.get_geocoding_provider()
        assert isinstance(provider, geocoding_service.NominatimGeocodingProvider)

    def test_returns_mock_provider(self, monkeypatch):
        from app.services import geocoding_service

        monkeypatch.setattr(geocoding_service.settings, "GEOCODING_PROVIDER", "mock")
        provider = geocoding_service.get_geocoding_provider()
        assert isinstance(provider, geocoding_service.MockGeocodingProvider)

    def test_raises_on_unknown_provider(self, monkeypatch):
        from app.services import geocoding_service

        monkeypatch.setattr(
            geocoding_service.settings, "GEOCODING_PROVIDER", "unknown_provider"
        )
        with pytest.raises(ValueError, match="Unknown GEOCODING_PROVIDER"):
            geocoding_service.get_geocoding_provider()


# ===========================================================================
# Celery task — match_building
# ===========================================================================


class TestMatchBuildingTask:
    """Unit tests for the Celery GIS task.

    All database and Redis calls are mocked so no infrastructure is required.
    Tests call ``_match_building_impl`` directly to avoid needing a Celery
    worker context or a bound ``Task`` instance for ``self``.
    """

    def _make_report_row(
        self,
        lat=-1.295,
        lng=36.805,
        accuracy_m=None,
        landmark=None,
        confirmed=None,
        missing=False,
    ):
        row = MagicMock()
        row.lat = lat
        row.lng = lng
        row.gps_accuracy_m = accuracy_m
        row.landmark_description = landmark
        row.reporter_confirmed_building_id = confirmed
        row.reporter_building_missing = missing
        return row

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    @patch("app.workers.gis_tasks.get_geocoding_provider")
    def test_polygon_match_assigns_building_id(
        self, mock_geocoding, mock_redis_cls, mock_session_cls
    ):
        from app.services.gis_service import BuildingMatch
        from app.workers.gis_tasks import _match_building_impl

        report_row = self._make_report_row()
        db = MagicMock()
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = report_row
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db

        redis_instance = MagicMock()
        redis_instance.scan.return_value = (0, [])
        mock_redis_cls.return_value = redis_instance

        with patch(
            "app.workers.gis_tasks.GISService.match_building",
            return_value=BuildingMatch(
                building_id=_BUILDING_ID,
                confidence=1.0,
                distance_m=None,
                footprint_geojson=_FOOTPRINT_GEOJSON,
            ),
        ):
            with patch(
                "app.workers.gis_tasks.GISService.update_building_severity"
            ) as mock_update:
                result = _match_building_impl(str(uuid4()))

        assert result["confidence"] == 1.0
        assert UUID(result["building_id"]) == _BUILDING_ID
        mock_update.assert_called_once_with(_BUILDING_ID)

    def _run_with_confirmed(self, mock_redis_cls, mock_session_cls, confirm_result):
        from app.services.gis_service import BuildingMatch
        from app.workers.gis_tasks import _match_building_impl

        db = MagicMock()
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = self._make_report_row(
            confirmed=_BUILDING_ID
        )
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db
        redis_instance = MagicMock()
        redis_instance.scan.return_value = (0, [])
        mock_redis_cls.return_value = redis_instance

        fallback = BuildingMatch(
            building_id=uuid4(),
            confidence=0.4,
            distance_m=12.0,
            footprint_geojson=None,
        )
        with (
            patch(
                "app.workers.gis_tasks.GISService.confirm_building",
                return_value=confirm_result,
            ) as mock_confirm,
            patch(
                "app.workers.gis_tasks.GISService.match_building",
                return_value=fallback,
            ) as mock_match,
            patch("app.workers.gis_tasks.GISService.update_building_severity"),
        ):
            result = _match_building_impl(str(uuid4()))
        return result, mock_confirm, mock_match, fallback

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    @patch("app.workers.gis_tasks.get_geocoding_provider")
    def test_building_reported_missing_is_not_snapped_to_a_neighbour(
        self, mock_geocoding, mock_redis_cls, mock_session_cls
    ):
        from app.workers.gis_tasks import _match_building_impl

        db = MagicMock()
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = self._make_report_row(missing=True)
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db
        redis_instance = MagicMock()
        redis_instance.scan.return_value = (0, [])
        mock_redis_cls.return_value = redis_instance

        with (
            patch("app.workers.gis_tasks.GISService.match_building") as mock_match,
            patch("app.workers.gis_tasks.GISService.confirm_building") as mock_conf,
        ):
            result = _match_building_impl(str(uuid4()))

        mock_match.assert_not_called()
        mock_conf.assert_not_called()
        assert result["building_id"] is None
        assert result["confidence"] == 0.0

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    @patch("app.workers.gis_tasks.get_geocoding_provider")
    def test_reporter_confirmed_building_is_adopted(
        self, mock_geocoding, mock_redis_cls, mock_session_cls
    ):
        from app.services.gis_service import BuildingMatch

        confirmed = BuildingMatch(
            building_id=_BUILDING_ID,
            confidence=1.0,
            distance_m=2.0,
            footprint_geojson=_FOOTPRINT_GEOJSON,
        )
        result, mock_confirm, mock_match, _ = self._run_with_confirmed(
            mock_redis_cls, mock_session_cls, confirmed
        )

        mock_confirm.assert_called_once_with(_BUILDING_ID, -1.295, 36.805, None)
        mock_match.assert_not_called()
        assert UUID(result["building_id"]) == _BUILDING_ID
        assert result["confidence"] == 1.0

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    @patch("app.workers.gis_tasks.get_geocoding_provider")
    def test_rejected_confirmation_falls_back_to_matching(
        self, mock_geocoding, mock_redis_cls, mock_session_cls
    ):
        result, mock_confirm, mock_match, fallback = self._run_with_confirmed(
            mock_redis_cls, mock_session_cls, None
        )

        mock_confirm.assert_called_once()
        mock_match.assert_called_once()
        assert UUID(result["building_id"]) == fallback.building_id
        assert result["confidence"] == pytest.approx(0.4)

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    @patch("app.workers.gis_tasks.get_geocoding_provider")
    def test_redis_cache_invalidated_after_match(
        self, mock_geocoding, mock_redis_cls, mock_session_cls
    ):
        from app.services.gis_service import BuildingMatch
        from app.workers.gis_tasks import _match_building_impl

        report_row = self._make_report_row()
        db = MagicMock()
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = report_row
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db

        redis_instance = MagicMock()
        redis_instance.scan.return_value = (0, ["gis:heatmap:abc123"])
        mock_redis_cls.return_value = redis_instance

        with patch(
            "app.workers.gis_tasks.GISService.match_building",
            return_value=BuildingMatch(
                building_id=_BUILDING_ID,
                confidence=1.0,
                distance_m=None,
                footprint_geojson=None,
            ),
        ):
            with patch("app.workers.gis_tasks.GISService.update_building_severity"):
                _match_building_impl(str(uuid4()))

        assert redis_instance.delete.call_count >= 1

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    @patch("app.workers.gis_tasks.get_geocoding_provider")
    def test_unmapped_structure_sets_zero_confidence(
        self, mock_geocoding, mock_redis_cls, mock_session_cls
    ):
        from app.services.gis_service import BuildingMatch
        from app.workers.gis_tasks import _match_building_impl

        report_row = self._make_report_row(lat=None, lng=None)
        db = MagicMock()
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = report_row
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db

        redis_instance = MagicMock()
        redis_instance.scan.return_value = (0, [])
        mock_redis_cls.return_value = redis_instance

        with patch(
            "app.workers.gis_tasks.GISService.match_building",
            return_value=BuildingMatch(
                building_id=None,
                confidence=0.0,
                distance_m=None,
                footprint_geojson=None,
            ),
        ):
            result = _match_building_impl(str(uuid4()))

        assert result["building_id"] is None
        assert result["confidence"] == 0.0

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    @patch("app.workers.gis_tasks.sync_redis.Redis.from_url")
    def test_report_not_found_returns_gracefully(
        self, mock_redis_cls, mock_session_cls
    ):
        from app.workers.gis_tasks import _match_building_impl

        db = MagicMock()
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = None
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db

        redis_instance = MagicMock()
        mock_redis_cls.return_value = redis_instance

        result = _match_building_impl(str(uuid4()))

        assert result["building_id"] is None

    @patch("app.workers.gis_tasks._SyncSessionLocal")
    def test_no_sql_string_interpolation_of_coordinates(self, mock_session_cls):
        """Verify that coordinates are always passed as bound parameters."""
        from app.workers.gis_tasks import _match_building_impl

        lat, lng = -1.295, 36.805

        db = MagicMock()
        report_row = MagicMock()
        report_row.lat = lat
        report_row.lng = lng
        report_row.gps_accuracy_m = None
        report_row.landmark_description = None
        report_row.reporter_confirmed_building_id = None
        report_row.reporter_building_missing = False
        fetch_result = MagicMock()
        fetch_result.fetchone.return_value = report_row
        db.execute.return_value = fetch_result
        mock_session_cls.return_value = db

        executed_sqls: list[str] = []
        executed_params: list[dict] = []

        def capture_execute(stmt, params=None, **kwargs):
            try:
                sql_str = str(stmt)
            except Exception:
                sql_str = repr(stmt)
            executed_sqls.append(sql_str)
            if params:
                executed_params.append(params)
            return fetch_result

        db.execute.side_effect = capture_execute

        with patch("app.workers.gis_tasks.sync_redis.Redis.from_url") as mock_r:
            mock_r.return_value.scan.return_value = (0, [])
            with patch(
                "app.workers.gis_tasks.GISService.match_building",
                return_value=__import__(
                    "app.services.gis_service", fromlist=["BuildingMatch"]
                ).BuildingMatch(
                    building_id=None,
                    confidence=0.0,
                    distance_m=None,
                    footprint_geojson=None,
                ),
            ):
                _match_building_impl(str(uuid4()))

        for sql in executed_sqls:
            assert str(lat) not in sql, f"lat interpolated into SQL: {sql}"
            assert str(lng) not in sql, f"lng interpolated into SQL: {sql}"


# ===========================================================================
# Import footprints CLI
# ===========================================================================


class TestImportFootprintsCLI:
    def _make_geojson_feature(self, external_id="test-building-001"):
        return {
            "type": "Feature",
            "id": external_id,
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [36.8, -1.3],
                        [36.81, -1.3],
                        [36.81, -1.29],
                        [36.8, -1.29],
                        [36.8, -1.3],
                    ]
                ],
            },
            "properties": {},
        }

    def test_iter_features_from_feature_collection(self, tmp_path):
        from app.cli.import_footprints import _iter_features

        fc = {
            "type": "FeatureCollection",
            "features": [
                self._make_geojson_feature("b1"),
                self._make_geojson_feature("b2"),
            ],
        }
        source_file = tmp_path / "test.geojson"
        source_file.write_text(json.dumps(fc))

        features = list(_iter_features(str(source_file)))
        assert len(features) == 2

    def test_iter_features_from_ndjson(self, tmp_path):
        from app.cli.import_footprints import _iter_features

        lines = "\n".join(
            json.dumps(self._make_geojson_feature(f"b{i}")) for i in range(3)
        )
        source_file = tmp_path / "test.ndjson"
        source_file.write_text(lines)

        features = list(_iter_features(str(source_file)))
        assert len(features) == 3

    def test_skips_non_polygon_geometries(self, tmp_path):
        from app.cli.import_footprints import _iter_features

        fc = {
            "type": "FeatureCollection",
            "features": [
                self._make_geojson_feature("b1"),
                {
                    "type": "Feature",
                    "id": "point-001",
                    "geometry": {"type": "Point", "coordinates": [36.8, -1.3]},
                    "properties": {},
                },
            ],
        }
        source_file = tmp_path / "test.geojson"
        source_file.write_text(json.dumps(fc))

        # _iter_features yields all features; skipping is done in _run
        features = list(_iter_features(str(source_file)))
        assert len(features) == 2  # Both features yielded

    def test_geometry_hash_is_deterministic(self):
        from app.cli.import_footprints import _geometry_hash

        geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        h1 = _geometry_hash(geom)
        h2 = _geometry_hash(geom)
        assert h1 == h2
        assert len(h1) == 24

    @patch("app.cli.import_footprints.create_engine")
    def test_dry_run_skips_db_writes(self, mock_create_engine, tmp_path):
        from app.cli.import_footprints import _run

        fc = {
            "type": "FeatureCollection",
            "features": [self._make_geojson_feature("dry-run-b1")],
        }
        source_file = tmp_path / "test.geojson"
        source_file.write_text(json.dumps(fc))

        mock_engine = MagicMock()
        mock_create_engine.return_value = mock_engine
        mock_session = MagicMock()
        mock_engine.connect.return_value.__enter__ = lambda s: mock_session
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("app.cli.import_footprints.sessionmaker") as mock_sm:
            mock_session_factory = MagicMock()
            mock_sm.return_value = mock_session_factory
            mock_db = MagicMock()
            mock_session_factory.return_value = mock_db

            exit_code = _run(
                source=str(source_file),
                batch_size=100,
                dry_run=True,
            )

        # Dry run should not commit to DB
        mock_db.commit.assert_not_called()
        assert exit_code == 0

    def test_iter_features_from_overpass_json(self, tmp_path):
        from app.cli.import_footprints import _iter_features

        ring = [
            {"lat": -1.3, "lon": 36.8},
            {"lat": -1.3, "lon": 36.81},
            {"lat": -1.29, "lon": 36.81},
            {"lat": -1.3, "lon": 36.8},
        ]
        overpass = {
            "elements": [
                {
                    "type": "way",
                    "id": 42,
                    "geometry": ring,
                    "tags": {"building": "yes"},
                },
                {"type": "way", "id": 43, "geometry": ring[:3], "tags": {}},  # open
                {"type": "relation", "id": 7, "members": []},
            ]
        }
        source_file = tmp_path / "overpass.json"
        source_file.write_text(json.dumps(overpass))

        features = list(_iter_features(str(source_file)))
        assert len(features) == 1
        assert features[0]["id"] == "way/42"
        assert features[0]["geometry"]["coordinates"][0][0] == [36.8, -1.3]
        assert features[0]["properties"] == {"building": "yes"}

    @pytest.mark.parametrize(
        "feature_id, props, expected",
        [
            ("way/123", {}, "osm:way/123"),
            (None, {"@id": "relation/9"}, "osm:relation/9"),
            (None, {"osm_way_id": "55", "osm_id": None}, "osm:way/55"),
            (None, {"osm_way_id": None, "osm_id": "66"}, "osm:relation/66"),
            (None, {"osm_id": 77, "osm_type": "ways_poly"}, "osm:way/77"),
            (None, {"osm_id": 88}, "osm:88"),
        ],
    )
    def test_osm_external_id_conventions(self, feature_id, props, expected):
        from app.cli.import_footprints import _osm_external_id

        feature = self._make_geojson_feature(feature_id)
        feature["properties"] = props
        assert _osm_external_id(feature, feature["geometry"]) == expected

    def test_osm_external_id_falls_back_to_geometry_hash(self):
        from app.cli.import_footprints import _geometry_hash, _osm_external_id

        feature = self._make_geojson_feature(None)
        expected = f"osm:geom-{_geometry_hash(feature['geometry'])}"
        assert _osm_external_id(feature, feature["geometry"]) == expected

    @pytest.mark.parametrize(
        "props, expected",
        [
            ({"building": "yes"}, True),
            ({"tags": {"building": "house"}}, True),
            ({"building": "no"}, False),
            ({"highway": "residential"}, False),
            ({}, False),
        ],
    )
    def test_is_osm_building(self, props, expected):
        from app.cli.import_footprints import _is_osm_building

        assert _is_osm_building(props) is expected

    @pytest.mark.parametrize(
        "source_type, feature_id, expected_source, expected_ext_id",
        [
            ("microsoft", "ms-1", "microsoft_africa", "ms-1"),
            ("osm", "way/123", "osm", "osm:way/123"),
        ],
    )
    @patch("app.cli.import_footprints.create_engine")
    def test_run_writes_source_and_external_id(
        self,
        mock_create_engine,
        source_type,
        feature_id,
        expected_source,
        expected_ext_id,
        tmp_path,
    ):
        from app.cli.import_footprints import _run

        building = self._make_geojson_feature(feature_id)
        building["properties"] = {"building": "yes"}
        not_building = self._make_geojson_feature("way/999")
        not_building["properties"] = {"landuse": "residential"}
        fc = {"type": "FeatureCollection", "features": [building, not_building]}
        source_file = tmp_path / "fp.geojson"
        source_file.write_text(json.dumps(fc))

        with patch("app.cli.import_footprints.sessionmaker") as mock_sm:
            mock_db = MagicMock()
            mock_sm.return_value = MagicMock(return_value=mock_db)
            exit_code = _run(
                source=str(source_file),
                batch_size=100,
                dry_run=False,
                source_type=source_type,
            )

        assert exit_code == 0
        upserts = [
            c.args[1]
            for c in mock_db.execute.call_args_list
            if len(c.args) > 1 and "external_id" in c.args[1]
        ]
        # The OSM path drops the untagged landuse polygon; Microsoft keeps both.
        assert len(upserts) == (1 if source_type == "osm" else 2)
        assert upserts[0]["source"] == expected_source
        assert upserts[0]["external_id"] == expected_ext_id


# ===========================================================================
# GIS endpoint
# ===========================================================================


class TestGISBuildingMatchEndpoint:
    """HTTP integration tests for GET /api/v1/gis/building/match."""

    @pytest.fixture
    def app_client(self):
        """TestClient for the GIS router with DB + Redis dependencies stubbed.

        The route depends on ``get_sync_db`` and ``get_redis``; overriding them
        here keeps the test hermetic — it must never reach a real
        ``localhost:6379`` (that made CI red whenever Redis was absent).
        """
        from unittest.mock import AsyncMock

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.v1.routes.gis import router
        from app.core.dependencies import get_redis, get_sync_db

        test_app = FastAPI()
        test_app.include_router(router, prefix="/api/v1")

        redis_stub = AsyncMock()
        redis_stub.get = AsyncMock(return_value=None)
        redis_stub.set = AsyncMock(return_value=True)

        test_app.dependency_overrides[get_sync_db] = lambda: MagicMock()
        test_app.dependency_overrides[get_redis] = lambda: redis_stub

        return TestClient(test_app)

    def test_returns_200_with_building_id(self, app_client):
        from app.services.gis_service import BuildingMatch

        mock_match = BuildingMatch(
            building_id=_BUILDING_ID,
            confidence=1.0,
            distance_m=None,
            footprint_geojson=_FOOTPRINT_GEOJSON,
        )

        with patch("app.api.v1.routes.gis.GISService") as MockGIS:
            MockGIS.return_value.match_building.return_value = mock_match
            response = app_client.get(
                "/api/v1/gis/building/match",
                params={"lat": -1.295, "lng": 36.805},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["building_id"] == str(_BUILDING_ID)
        assert body["confidence"] == 1.0
        assert body["candidates"] == []

    def test_returns_candidates(self, app_client):
        from app.services.gis_service import BuildingCandidate, BuildingMatch

        neighbour = uuid4()
        mock_match = BuildingMatch(
            building_id=_BUILDING_ID,
            confidence=0.8,
            distance_m=6.0,
            footprint_geojson=_FOOTPRINT_GEOJSON,
            candidates=[
                BuildingCandidate(_BUILDING_ID, "osm:way/1", 6.0, _FOOTPRINT_GEOJSON),
                BuildingCandidate(neighbour, "osm:way/2", 7.5, _FOOTPRINT_GEOJSON),
            ],
        )

        with patch("app.api.v1.routes.gis.GISService") as MockGIS:
            MockGIS.return_value.match_building.return_value = mock_match
            response = app_client.get(
                "/api/v1/gis/building/match",
                params={"lat": -1.295, "lng": 36.805},
            )
            assert MockGIS.return_value.match_building.call_args.kwargs[
                "with_candidates"
            ]

        assert response.status_code == 200
        candidates = response.json()["candidates"]
        assert [c["building_id"] for c in candidates] == [
            str(_BUILDING_ID),
            str(neighbour),
        ]
        assert candidates[1]["external_id"] == "osm:way/2"
        assert candidates[1]["distance_m"] == 7.5

    def test_requires_lat_and_lng_params(self, app_client):
        """Missing required query params should return 422."""
        response = app_client.get("/api/v1/gis/building/match")
        assert response.status_code == 422

    def test_rejects_invalid_lat(self, app_client):
        response = app_client.get(
            "/api/v1/gis/building/match",
            params={"lat": 999.0, "lng": 36.805},
        )
        assert response.status_code == 422

    def test_rejects_invalid_lng(self, app_client):
        response = app_client.get(
            "/api/v1/gis/building/match",
            params={"lat": -1.295, "lng": 999.0},
        )
        assert response.status_code == 422
