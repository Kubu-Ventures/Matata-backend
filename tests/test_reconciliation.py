"""Tests for the pipeline reconciliation sweep (audit M-8 / M-9).

The ``_STUCK_QUERY`` SQL is PostgreSQL-specific (``make_interval``), so — as
with the PostGIS paths elsewhere — the DB session is fully mocked here and the
query text itself is exercised in the integration/simulation run, not in unit
tests.
"""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test_reconcile.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)
os.environ.setdefault("PHONE_HASH_SALT", "x" * 32)
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from app.workers import reconciliation_tasks as rt  # noqa: E402


def _row(*, needs_gis=False, needs_ai=False, needs_dedup=False):
    return SimpleNamespace(
        id=str(uuid4()),
        needs_gis=needs_gis,
        needs_ai=needs_ai,
        needs_dedup=needs_dedup,
    )


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def execute(self, *_a, **_k):
        return MagicMock(fetchall=lambda: self._rows)

    def rollback(self):
        pass

    def close(self):
        self.closed = True


@contextlib.contextmanager
def _harness(rows, redis_ok=True):
    """Patch DB session, celery app and Redis for one sweep."""
    session = _FakeSession(rows)
    redis_client = MagicMock()
    if not redis_ok:
        redis_client.ping.side_effect = RuntimeError("no redis")
    celery_mock = MagicMock()

    with (
        patch.object(rt, "_SyncSessionLocal", MagicMock(return_value=session)),
        patch.object(rt, "celery_app", celery_mock),
        patch("redis.Redis.from_url", MagicMock(return_value=redis_client)),
    ):
        yield celery_mock, redis_client, session


def _sent_tasks(celery_mock):
    return [c.args[0] for c in celery_mock.send_task.call_args_list]


def test_nothing_stuck_returns_zero_summary():
    with _harness([]) as (_c, _r, session):
        summary = rt._reconcile_impl()
        assert summary == {"scanned": 0, "gis": 0, "ai": 0, "dedup_direct": 0}
    assert session.closed is True


def test_needs_gis_only_seeds_gate_1_and_dispatches_gis():
    row = _row(needs_gis=True)
    with _harness([row]) as (celery_mock, redis_client, _s):
        summary = rt._reconcile_impl()
        assert summary["gis"] == 1 and summary["ai"] == 0
        seed_call = redis_client.set.call_args
        assert seed_call.args[1] == 1
        assert row.id in seed_call.args[0]
        sent = _sent_tasks(celery_mock)
        assert "app.workers.gis_tasks.match_building" in sent
        assert "app.workers.ai_tasks.process_report_image" not in sent


def test_needs_gis_and_ai_seeds_gate_2_and_dispatches_both():
    row = _row(needs_gis=True, needs_ai=True)
    with _harness([row]) as (celery_mock, redis_client, _s):
        summary = rt._reconcile_impl()
        assert summary["gis"] == 1 and summary["ai"] == 1
        assert redis_client.set.call_args.args[1] == 2
        sent = _sent_tasks(celery_mock)
        assert "app.workers.gis_tasks.match_building" in sent
        assert "app.workers.ai_tasks.process_report_image" in sent


def test_needs_dedup_only_dispatches_score_report_without_gate():
    row = _row(needs_dedup=True)
    with _harness([row]) as (celery_mock, redis_client, _s):
        summary = rt._reconcile_impl()
        assert summary == {"scanned": 1, "gis": 0, "ai": 0, "dedup_direct": 1}
        redis_client.set.assert_not_called()  # no coordination gate needed
        assert _sent_tasks(celery_mock) == ["app.workers.duplicate_tasks.score_report"]


def test_redis_unavailable_aborts_without_dispatch():
    row = _row(needs_gis=True)
    with _harness([row], redis_ok=False) as (celery_mock, _r, _s):
        summary = rt._reconcile_impl()
        assert summary == {"scanned": 0, "gis": 0, "ai": 0, "dedup_direct": 0}
        celery_mock.send_task.assert_not_called()


def test_task_wrapper_swallows_errors():
    with patch.object(rt, "_reconcile_impl", side_effect=RuntimeError("boom")):
        result = rt.reconcile_stuck_reports.__wrapped__.__func__(MagicMock())
    assert result["error"] is True
