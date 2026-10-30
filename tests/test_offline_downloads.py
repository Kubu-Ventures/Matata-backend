"""Tests for the offline download endpoints.

``GET /gis/health-facilities/all`` and ``GET /gis/buildings`` feed the data
the app saves on the phone so the clinic list and the building picker work
without a connection.
"""

from __future__ import annotations

import gzip
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
def client():
    from app.core.dependencies import get_db, get_sync_db
    from app.main import app

    async def _db():
        yield AsyncMock()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_sync_db] = lambda: MagicMock()
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestAllFacilities:
    def test_returns_compact_rows(self, client, monkeypatch):
        from app.services import health_facility_service

        monkeypatch.setattr(
            health_facility_service,
            "all_facilities",
            AsyncMock(return_value=[["Kibera Clinic", "clinic", -1.31, 36.78, None]]),
        )
        resp = client.get("/api/v1/gis/health-facilities/all")
        assert resp.status_code == 200
        body = resp.json()
        assert body["fields"] == ["name", "facility_type", "lat", "lng", "phone"]
        assert body["items"][0][0] == "Kibera Clinic"
        assert "max-age" in resp.headers["cache-control"]

    def test_gzipped_when_large(self, client, monkeypatch):
        from app.services import health_facility_service

        rows = [[f"Clinic {i}", "clinic", -1.3, 36.7, None] for i in range(200)]
        monkeypatch.setattr(
            health_facility_service, "all_facilities", AsyncMock(return_value=rows)
        )
        resp = client.get(
            "/api/v1/gis/health-facilities/all",
            headers={"Accept-Encoding": "gzip"},
        )
        assert resp.status_code == 200
        assert resp.headers.get("content-encoding") == "gzip"
        assert len(resp.json()["items"]) == 200

    @pytest.mark.asyncio
    async def test_all_facilities_query(self):
        from app.db.base_class import Base
        from app.models.health_facility import HealthFacility
        from app.services.health_facility_service import all_facilities

        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(c, tables=[HealthFacility.__table__])
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(
                HealthFacility(
                    external_id="osm:node/1",
                    name="Undugu",
                    facility_type="clinic",
                    lat=-1.2612345678,
                    lng=36.8571234567,
                    phone="+254700000000",
                )
            )
            await db.commit()
            rows = await all_facilities(db)
        await engine.dispose()
        assert rows == [["Undugu", "clinic", -1.261235, 36.857123, "+254700000000"]]


class TestBuildingsDownload:
    def _patch(self, monkeypatch, rows):
        from app.services import gis_service

        mock = MagicMock(return_value=rows)
        monkeypatch.setattr(gis_service.GISService, "buildings_in_bbox", mock)
        return mock

    def test_returns_rows(self, client, monkeypatch):
        geo = json.dumps(
            {
                "type": "Polygon",
                "coordinates": [[[36.78, -1.31], [36.781, -1.31], [36.78, -1.31]]],
            }
        )
        mock = self._patch(monkeypatch, [["b1", "osm:way/1", geo]])
        resp = client.get(
            "/api/v1/gis/buildings",
            params={
                "min_lat": -1.315,
                "min_lng": 36.78,
                "max_lat": -1.31,
                "max_lng": 36.785,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"][0][:2] == ["b1", "osm:way/1"]
        assert body["truncated"] is False
        mock.assert_called_once()

    def test_box_too_large(self, client, monkeypatch):
        self._patch(monkeypatch, [])
        resp = client.get(
            "/api/v1/gis/buildings",
            params={"min_lat": -1.4, "min_lng": 36.7, "max_lat": -1.3, "max_lng": 36.8},
        )
        assert resp.status_code == 422

    def test_empty_box(self, client, monkeypatch):
        self._patch(monkeypatch, [])
        resp = client.get(
            "/api/v1/gis/buildings",
            params={
                "min_lat": -1.3,
                "min_lng": 36.7,
                "max_lat": -1.3,
                "max_lng": 36.705,
            },
        )
        assert resp.status_code == 422

    def test_gzip(self, client, monkeypatch):
        geo = json.dumps({"type": "Polygon", "coordinates": [[[36.78, -1.31]]]})
        self._patch(monkeypatch, [[f"b{i}", f"osm:way/{i}", geo] for i in range(100)])
        resp = client.get(
            "/api/v1/gis/buildings",
            params={
                "min_lat": -1.315,
                "min_lng": 36.78,
                "max_lat": -1.31,
                "max_lng": 36.785,
            },
            headers={"Accept-Encoding": "gzip"},
        )
        assert resp.headers.get("content-encoding") == "gzip"
        assert len(resp.json()["items"]) == 100


def test_buildings_in_bbox_maps_rows():
    from app.services.gis_service import GISService

    row = MagicMock(id="abc", external_id="osm:way/9", geojson='{"type":"Polygon"}')
    db = MagicMock()
    db.execute.return_value.fetchall.return_value = [row]
    rows = GISService(db).buildings_in_bbox(-1.32, 36.78, -1.31, 36.79)
    assert rows == [["abc", "osm:way/9", '{"type":"Polygon"}']]
    params = db.execute.call_args[0][1]
    assert params["min_lng"] == 36.78 and params["max_lat"] == -1.31


def test_gzip_body_roundtrip():
    """Guard the helper: compressed body decodes to the same JSON."""
    from starlette.requests import Request

    from app.api.v1.routes.gis import _json_response

    scope = {
        "type": "http",
        "headers": [(b"accept-encoding", b"gzip")],
        "method": "GET",
        "path": "/",
    }
    payload = {"items": [["x" * 50] for _ in range(100)]}
    resp = _json_response(Request(scope), payload, max_age=60)
    assert json.loads(gzip.decompress(resp.body)) == payload


# ===========================================================================
# Offline areas
# ===========================================================================


class TestValidateBbox:
    def test_ok(self):
        from app.services.offline_area_service import validate_bbox

        validate_bbox(-1.322, 36.770, -1.304, 36.800)

    @pytest.mark.parametrize(
        "box",
        [
            (-1.3, 36.8, -1.31, 36.9),  # min above max
            (-1.4, 36.7, -1.3, 36.8),  # 0.1 degrees tall
            (-1.3, 36.7, -1.29, 36.8),  # 0.1 degrees wide
        ],
    )
    def test_rejected(self, box):
        from app.services.offline_area_service import validate_bbox

        with pytest.raises(ValueError):
            validate_bbox(*box)


class TestAreasForSubject:
    @pytest.mark.asyncio
    async def test_home_area_matches_code_prefix(self):
        from app.db.base_class import Base
        from app.models.field_reporter import FieldReporter
        from app.models.offline_area import OfflineArea
        from app.services.offline_area_service import areas_for_subject

        engine = create_async_engine("sqlite+aiosqlite://")
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda c: Base.metadata.create_all(
                    c, tables=[OfflineArea.__table__, FieldReporter.__table__]
                )
            )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            kib = OfflineArea(
                name="Kibera",
                kind="home",
                code_prefix="KIB",
                min_lat=-1.322,
                min_lng=36.77,
                max_lat=-1.304,
                max_lng=36.8,
            )
            mat = OfflineArea(
                name="Mathare",
                kind="home",
                code_prefix="MAT",
                min_lat=-1.268,
                min_lng=36.845,
                max_lat=-1.252,
                max_lng=36.875,
            )
            off = OfflineArea(
                name="Old",
                kind="risk",
                min_lat=0,
                min_lng=0,
                max_lat=0.01,
                max_lng=0.01,
                is_active=False,
            )
            db.add_all([kib, mat, off, FieldReporter(code="KIB-03", email_hash="eh")])
            await db.commit()

            mine = await areas_for_subject("eh", db)
            public = await areas_for_subject("someone-else", db)
        await engine.dispose()

        assert {a["name"] for a in mine["areas"]} == {"Kibera", "Mathare"}
        assert mine["home_area_ids"] == [str(kib.id)]
        assert public["home_area_ids"] == []
        kib_row = next(a for a in mine["areas"] if a["name"] == "Kibera")
        assert kib_row["bbox"] == [-1.322, 36.77, -1.304, 36.8]


def test_offline_areas_route(client, monkeypatch):
    from app.api.v1.routes.auth import get_current_user
    from app.main import app
    from app.services import offline_area_service

    monkeypatch.setattr(
        offline_area_service,
        "areas_for_subject",
        AsyncMock(return_value={"areas": [], "home_area_ids": []}),
    )
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": "x",
        "role": "reporter",
    }
    resp = client.get("/api/v1/gis/offline-areas")
    assert resp.status_code == 200
    assert resp.json() == {"areas": [], "home_area_ids": []}
