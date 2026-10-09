"""Tests for the ``create-account`` / ``create-admin`` management commands."""

from __future__ import annotations

import sys

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


async def test_create_account_responder_role(account_db):
    await cli._create_account("responder@example.org", "responder")

    rows = await _rows(account_db)
    assert [r.role for r in rows] == ["responder"]


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

    async def fake_create_account(email, role, label):
        calls.append((email, role, label))

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
        ],
    )

    cli.main()

    assert calls == [("r@example.org", "responder", None)]
