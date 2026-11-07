"""Admin endpoints and service for limiting accounts to an area."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.analyst_account import AnalystAccount
from app.services import auth_service
from app.services.auth_service import Role
from app.services.region_scope import bboxes_to_region, parse_bbox

KIBERA = bboxes_to_region([parse_bbox("-1.3256,36.7647,-1.2993,36.8066")])


@pytest.fixture
async def session(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(AnalystAccount.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _register(session, role, region=None):
    return await auth_service.register_analyst_account(
        email=f"{uuid.uuid4().hex[:8]}@example.org",
        role=role,
        created_by_sub="admin",
        db=session,
        region_geojson=region,
    )


async def test_register_analyst_with_area_is_stored_compact(session):
    account = await _register(session, Role.analyst, KIBERA)
    assert json.loads(account.region_geojson) == json.loads(KIBERA)


async def test_register_rejects_admin_area_and_bare_responder(session):
    with pytest.raises(ValueError, match="admin"):
        await _register(session, Role.admin, KIBERA)
    with pytest.raises(ValueError, match="responder"):
        await _register(session, Role.responder)


async def test_register_rejects_invalid_area(session):
    with pytest.raises(ValueError, match="Polygon"):
        await _register(session, Role.analyst, '{"type": "Point"}')


async def test_update_area_set_and_clear_for_analyst(session):
    account = await _register(session, Role.analyst)
    updated = await auth_service.update_analyst_account_area(
        str(account.id), KIBERA, session
    )
    assert updated.region_geojson is not None
    cleared = await auth_service.update_analyst_account_area(
        str(account.id), None, session
    )
    assert cleared.region_geojson is None


async def test_update_area_role_rules(session):
    responder = await _register(session, Role.responder, KIBERA)
    with pytest.raises(ValueError):
        await auth_service.update_analyst_account_area(str(responder.id), None, session)
    admin = await _register(session, Role.admin)
    with pytest.raises(ValueError):
        await auth_service.update_analyst_account_area(str(admin.id), KIBERA, session)


async def test_update_area_unknown_account(session):
    assert (
        await auth_service.update_analyst_account_area(
            str(uuid.uuid4()), KIBERA, session
        )
        is None
    )


# --- route ---


def _client():
    from tests.test_analyst_auth import _make_app

    return TestClient(_make_app())


def _admin_headers():
    from tests.test_analyst_auth import _admin_token

    return {"Authorization": f"Bearer {_admin_token()}"}


def test_put_area_route():
    from tests.test_analyst_auth import _make_account

    with patch(
        "app.api.v1.routes.analyst_auth.auth_service.update_analyst_account_area",
        new=AsyncMock(return_value=_make_account(region_geojson=KIBERA)),
    ) as svc:
        resp = _client().put(
            f"/api/v1/auth/analyst/accounts/{uuid.uuid4()}/area",
            json={"region_geojson": KIBERA},
            headers=_admin_headers(),
        )
    assert resp.status_code == 200
    assert resp.json()["region_geojson"] == KIBERA
    assert svc.await_args.kwargs["region_geojson"] == KIBERA


@pytest.mark.parametrize(
    "side_effect,code",
    [(ValueError("A responder must be limited to an area."), 422), (None, 404)],
)
def test_put_area_errors(side_effect, code):
    with patch(
        "app.api.v1.routes.analyst_auth.auth_service.update_analyst_account_area",
        new=AsyncMock(side_effect=side_effect, return_value=None),
    ):
        resp = _client().put(
            f"/api/v1/auth/analyst/accounts/{uuid.uuid4()}/area",
            json={"region_geojson": None},
            headers=_admin_headers(),
        )
    assert resp.status_code == code


def test_put_area_requires_admin():
    from tests.test_analyst_auth import _make_app

    resp = TestClient(_make_app()).put(
        f"/api/v1/auth/analyst/accounts/{uuid.uuid4()}/area",
        json={"region_geojson": None},
    )
    assert resp.status_code in (401, 403)
