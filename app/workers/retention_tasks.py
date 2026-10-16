"""Photo retention sweep — deletes report photos older than a set age.

Why this exists
---------------
Report photos show people's homes and sometimes the people themselves. They
are needed while a response is running, but not forever. The privacy policy
promises that photos are deleted after ``PHOTO_RETENTION_DAYS``; this sweep is
what keeps that promise.

What it does
------------
For every stored photo whose *newest* referencing report is older than the
cutoff, it deletes the object from storage and sets ``photo_url`` to NULL on
every report that points at it. A merge can copy a duplicate's photo key onto
the primary report (``analyst_service.merge_reports``), so one key may be
shared; grouping by key means a photo is never deleted while a newer report
still relies on it.

The report row itself is kept: location, damage details, ``photo_phash`` and
``photo_status`` stay, so the analyst record and exports remain intact (the
export's ``has_photo`` becomes false).

Off by default
--------------
``PHOTO_RETENTION_DAYS = 0`` (the default) disables the sweep entirely. It
runs daily on Celery beat (see ``celery_app.conf.beat_schedule``) or on demand
with ``python -m app.cli purge-photos``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from celery import Task
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Synchronous engine — Celery/CLI context, mirrors reconciliation_tasks.
_sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
_sync_engine = create_engine(_sync_url, pool_pre_ping=True, pool_size=2, max_overflow=2)
_SyncSessionLocal = sessionmaker(
    bind=_sync_engine, autoflush=False, autocommit=False, expire_on_commit=False
)

DEFAULT_LIMIT = 500

_EXPIRED_KEYS_QUERY = text("""
    SELECT photo_url
    FROM report
    WHERE photo_url IS NOT NULL
    GROUP BY photo_url
    HAVING MAX(created_at) < :cutoff
    ORDER BY MAX(created_at) ASC
    LIMIT :limit
    """)

_CLEAR_KEY = text("UPDATE report SET photo_url = NULL WHERE photo_url = :key")


def _empty_summary(retention_days: int, dry_run: bool) -> dict[str, Any]:
    return {
        "retention_days": retention_days,
        "dry_run": dry_run,
        "expired": 0,
        "deleted": 0,
        "failed": 0,
    }


def _purge_impl(
    *,
    retention_days: Optional[int] = None,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Delete photos whose newest referencing report is older than the cutoff.

    ``retention_days`` defaults to ``settings.PHOTO_RETENTION_DAYS``; zero or
    less does nothing. Returns ``{retention_days, dry_run, expired, deleted,
    failed}``.
    """
    from app.services.storage_service import get_storage_service

    days = settings.PHOTO_RETENTION_DAYS if retention_days is None else retention_days
    summary = _empty_summary(days, dry_run)
    if days <= 0:
        logger.debug("photo retention disabled (PHOTO_RETENTION_DAYS=%s)", days)
        return summary

    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(days=days)
    db: Session = _SyncSessionLocal()
    try:
        keys = [
            row.photo_url
            for row in db.execute(
                _EXPIRED_KEYS_QUERY, {"cutoff": cutoff, "limit": limit}
            ).fetchall()
        ]
        db.rollback()  # read done — release the snapshot
        summary["expired"] = len(keys)
        if not keys or dry_run:
            logger.info("photo retention sweep: %s", summary)
            return summary

        storage = get_storage_service()
        for key in keys:
            try:
                asyncio.run(storage.delete_image(key))
            except Exception as exc:  # noqa: BLE001
                # Keep the key so the next run retries; never clear a row
                # whose object may still exist.
                summary["failed"] += 1
                logger.warning(
                    "photo retention: delete failed for a key (%s)", type(exc).__name__
                )
                continue
            db.execute(_CLEAR_KEY, {"key": key})
            db.commit()
            summary["deleted"] += 1

        logger.info("photo retention sweep complete: %s", summary)
        return summary
    finally:
        db.close()


@celery_app.task(
    name="app.workers.retention_tasks.purge_expired_photos",
    bind=True,
    max_retries=0,
    acks_late=True,
)
def purge_expired_photos(self: Task, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Celery entry point — see ``_purge_impl``. Never retries; the next daily
    run is the retry."""
    try:
        return _purge_impl(limit=limit)
    except Exception:  # noqa: BLE001
        logger.exception("photo retention sweep failed")
        summary = _empty_summary(settings.PHOTO_RETENTION_DAYS, False)
        summary["error"] = True
        return summary
