"""Tests for the photo retention sweep (PHOTO_RETENTION_DAYS).

As in ``test_reconciliation``, the DB session and the storage service are
mocked: the sweep's job is the decision logic (off by default, dry run, never
clear a row whose object may still exist), not the SQL engine.
"""

from __future__ import annotations

import contextlib
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_retention.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)
os.environ.setdefault("PHONE_HASH_SALT", "x" * 32)
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from app.workers import retention_tasks as rt  # noqa: E402


class _FakeSession:
    """Answers the expired-keys query with *keys*; records every update."""

    def __init__(self, keys):
        self._keys = keys
        self.queries: list[dict] = []
        self.cleared: list[str] = []
        self.commits = 0
        self.closed = False

    def execute(self, statement, params=None):
        params = params or {}
        if "key" in params:
            self.cleared.append(params["key"])
            return MagicMock()
        self.queries.append(params)
        rows = [SimpleNamespace(photo_url=k) for k in self._keys]
        return MagicMock(fetchall=lambda: rows)

    def rollback(self):
        pass

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@contextlib.contextmanager
def _harness(keys, delete_side_effect=None):
    session = _FakeSession(keys)
    storage = MagicMock()
    storage.delete_image = AsyncMock(side_effect=delete_side_effect)
    with (
        patch.object(rt, "_SyncSessionLocal", MagicMock(return_value=session)),
        patch(
            "app.services.storage_service.get_storage_service",
            MagicMock(return_value=storage),
        ),
    ):
        yield session, storage


def test_disabled_by_default_touches_nothing():
    with _harness(["reports/a.jpg"]) as (session, storage):
        summary = rt._purge_impl(retention_days=0)
    assert summary["expired"] == 0 and summary["deleted"] == 0
    assert session.queries == []
    storage.delete_image.assert_not_called()


def test_setting_default_is_off():
    assert rt.settings.PHOTO_RETENTION_DAYS == 0


def test_dry_run_counts_without_deleting():
    with _harness(["reports/a.jpg", "reports/b.jpg"]) as (session, storage):
        summary = rt._purge_impl(retention_days=30, dry_run=True)
    assert summary["expired"] == 2 and summary["deleted"] == 0
    storage.delete_image.assert_not_called()
    assert session.cleared == []
    assert session.closed is True


def test_deletes_object_then_clears_every_row_using_the_key():
    with _harness(["reports/a.jpg", "reports/b.jpg"]) as (session, storage):
        summary = rt._purge_impl(retention_days=30)
    assert summary["deleted"] == 2 and summary["failed"] == 0
    deleted = [c.args[0] for c in storage.delete_image.await_args_list]
    assert deleted == ["reports/a.jpg", "reports/b.jpg"]
    assert session.cleared == ["reports/a.jpg", "reports/b.jpg"]
    assert session.commits == 2


def test_cutoff_is_retention_days_before_now():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    with _harness([]) as (session, _storage):
        rt._purge_impl(retention_days=90, now=now)
    assert session.queries[0]["cutoff"] == now - timedelta(days=90)


def test_failed_delete_keeps_the_row_for_the_next_run():
    def fail_on_b(key):
        if key == "reports/b.jpg":
            raise RuntimeError("storage down")

    with _harness(["reports/a.jpg", "reports/b.jpg"], fail_on_b) as (session, _s):
        summary = rt._purge_impl(retention_days=30)
    assert summary["deleted"] == 1 and summary["failed"] == 1
    assert session.cleared == ["reports/a.jpg"]


def test_celery_task_never_raises():
    with patch.object(rt, "_purge_impl", side_effect=RuntimeError("db down")):
        summary = rt.purge_expired_photos.run()
    assert summary["error"] is True
