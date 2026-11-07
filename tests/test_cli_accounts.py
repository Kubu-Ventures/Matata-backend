"""Tests for the ``create-account`` / ``create-admin`` management commands."""

from __future__ import annotations

import json
import sys
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from app.cli import __main__ as cli
from app.core.config import settings
from app.models.analyst_account import AnalystAccount
from app.services.auth_service import hash_identifier


@pytest.fixture
async def account_db(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'accounts.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(AnalystAccount.__table__.create)
    monkeypatch.setattr(settings, "DATABASE_URL", url)
    yield engine
    await engine.dispose()


async def _rows(engine):
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(
                AnalystAccount.__table__.c.email_hash,
                AnalystAccount.__table__.c.role,
                AnalystAccount.__table__.c.label,
            )
        )
        return result.all()


async def test_create_account_stores_hashed_email_and_role(account_db, capsys):
    await cli._create_account("Analyst@Example.org ", "analyst", "tana-river-1")

    rows = await _rows(account_db)
    assert rows == [(hash_identifier("analyst@example.org"), "analyst", "tana-river-1")]
    assert "Analyst account created" in capsys.readouterr().out


KIBERA = "-1.3256,36.7647,-1.2993,36.8066"
MATHARE = "-1.2674,36.8389,-1.2495,36.8790"


async def _regions(engine):
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.select(
                AnalystAccount.__table__.c.id,
                AnalystAccount.__table__.c.region_geojson,
            )
        )
        return result.all()


async def test_create_account_responder_role(account_db):
    await cli._create_account(
        "responder@example.org", "responder", bboxes=[KIBERA, MATHARE]
    )

    rows = await _rows(account_db)
    assert [r.role for r in rows] == ["responder"]
    region = json.loads((await _regions(account_db))[0].region_geojson)
    assert region["type"] == "MultiPolygon"
    assert len(region["coordinates"]) == 2
    # GeoJSON order is [lng, lat]: Kibera's SW corner.
    assert region["coordinates"][0][0][0] == [36.7647, -1.3256]


async def test_create_responder_without_area_is_refused(account_db, capsys):
    with pytest.raises(SystemExit) as exc:
        await cli._create_account("responder@example.org", "responder")
    assert exc.value.code == 1
    assert "needs an area" in capsys.readouterr().out
    assert await _rows(account_db) == []


async def test_bbox_on_analyst_is_refused(account_db, capsys):
    with pytest.raises(SystemExit) as exc:
        await cli._create_account("a@example.org", "analyst", bboxes=[KIBERA])
    assert exc.value.code == 1
    assert await _rows(account_db) == []


async def test_bad_bbox_is_refused(account_db, capsys):
    with pytest.raises(SystemExit) as exc:
        await cli._create_account(
            "r@example.org", "responder", bboxes=["-1.29,36.76,-1.32,36.80"]
        )
    assert exc.value.code == 1
    assert "latitudes" in capsys.readouterr().out
    assert await _rows(account_db) == []


async def test_set_account_area_replaces_region(account_db, capsys):
    await cli._create_account("r@example.org", "responder", bboxes=[KIBERA])
    account_id = str((await _regions(account_db))[0].id)

    await cli._set_account_area(account_id, [KIBERA, MATHARE], clear=False)

    region = json.loads((await _regions(account_db))[0].region_geojson)
    assert len(region["coordinates"]) == 2
    assert "sign out and back in" in capsys.readouterr().out


async def test_set_account_area_refuses_analyst(account_db, capsys):
    await cli._create_account("a@example.org", "analyst")
    account_id = str((await _regions(account_db))[0].id)
    with pytest.raises(SystemExit) as exc:
        await cli._set_account_area(account_id, [KIBERA], clear=False)
    assert exc.value.code == 1
    assert (await _regions(account_db))[0].region_geojson is None


async def test_set_account_area_refuses_clearing_responder(account_db):
    await cli._create_account("r@example.org", "responder", bboxes=[KIBERA])
    account_id = str((await _regions(account_db))[0].id)
    with pytest.raises(SystemExit):
        await cli._set_account_area(account_id, None, clear=True)
    assert (await _regions(account_db))[0].region_geojson is not None


async def test_set_account_area_needs_bbox_or_clear(account_db, capsys):
    with pytest.raises(SystemExit):
        await cli._set_account_area(str(uuid.uuid4()), None, clear=False)
    assert "either" in capsys.readouterr().out


async def test_list_accounts_shows_area(account_db, capsys):
    await cli._create_account("r@example.org", "responder", bboxes=[KIBERA])
    await cli._create_account("a@example.org", "analyst")
    capsys.readouterr()
    await cli._list_accounts()
    out = capsys.readouterr().out
    assert "AREA" in out and "limited" in out and " all " in out


async def test_create_account_is_idempotent(account_db, capsys):
    await cli._create_account("a@example.org", "analyst")
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        await cli._create_account("a@example.org", "analyst")

    assert exc.value.code == 0
    assert len(await _rows(account_db)) == 1
    assert "already registered as role=analyst" in capsys.readouterr().out


async def test_create_account_existing_with_other_role_is_left_unchanged(
    account_db, capsys
):
    await cli._create_account("a@example.org", "analyst")
    capsys.readouterr()

    with pytest.raises(SystemExit):
        await cli._create_account("a@example.org", "admin")

    rows = await _rows(account_db)
    assert [r.role for r in rows] == ["analyst"]
    assert "To change it to admin" in capsys.readouterr().out


async def test_create_account_rejects_invalid_email(account_db):
    with pytest.raises(SystemExit) as exc:
        await cli._create_account("not-an-email", "analyst")

    assert exc.value.code == 1
    assert await _rows(account_db) == []


async def test_create_account_rejects_reporter_role(account_db):
    with pytest.raises(SystemExit) as exc:
        await cli._create_account("a@example.org", "reporter")

    assert exc.value.code == 1
    assert await _rows(account_db) == []


async def test_create_admin_delegates_with_admin_role(account_db):
    await cli._create_admin("boss@example.org", "ops-lead")

    rows = await _rows(account_db)
    assert [(r.role, r.label) for r in rows] == [("admin", "ops-lead")]


def test_main_dispatches_create_account(monkeypatch):
    calls = []

    async def fake_create_account(email, role, label, bboxes):
        calls.append((email, role, label, bboxes))

    monkeypatch.setattr(cli, "_create_account", fake_create_account)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "app.cli",
            "create-account",
            "--email",
            "r@example.org",
            "--role",
            "responder",
            f"--bbox={KIBERA}",
            f"--bbox={MATHARE}",
        ],
    )

    cli.main()

    assert calls == [("r@example.org", "responder", None, [KIBERA, MATHARE])]
