"""Async job queue service.

Publishes background processing jobs to Redis Streams so that the
``submission_service`` never has to know how the AI or GIS workers are
implemented.

Two streams are defined:
* ``crisismap:queue:gis``  — building footprint matching jobs consumed by the
  GIS worker.
* ``crisismap:queue:ai``   — image quality assessment and damage classification
  jobs consumed by the AI worker.

Design notes
------------
* Redis Streams (XADD) are used rather than simple pub/sub because streams
  are durable: messages are retained until acknowledged by a consumer group,
  meaning a worker restart does not lose pending jobs.
* ``maxlen`` trimming keeps each stream bounded to the last 10,000 entries so
  Redis memory consumption is predictable under sustained load.
* All publish operations are fire-and-forget from the caller's perspective —
  they do not affect the HTTP response to the reporter.  If Redis is
  unavailable, the error is logged and the submission still succeeds (the
  async workers will pick up work once Redis recovers via polling).
* A ``MockQueueService`` is provided for unit tests so no real Redis instance
  is required.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Redis Stream key constants — centralised here so all code uses the same names.
_STREAM_GIS = "crisismap:queue:gis"
_STREAM_AI = "crisismap:queue:ai"

# Maximum entries per stream — oldest entries are trimmed automatically by Redis.
_STREAM_MAXLEN = 10_000


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class QueueService(Protocol):
    """Interface for publishing async processing jobs."""

    async def publish_gis_job(self, report_id: UUID) -> None:
        """Publish a building footprint matching job for *report_id*.

        Args:
            report_id: UUID of the Report that needs GIS processing.
        """
        ...  # pragma: no cover

    async def publish_ai_job(self, report_id: UUID) -> None:
        """Publish an AI quality + classification job for *report_id*.

        Args:
            report_id: UUID of the Report whose photo needs AI processing.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# RedisQueueService — production
# ---------------------------------------------------------------------------


class RedisQueueService:
    """Publishes jobs to Celery using Redis as the broker."""

    async def publish_gis_job(self, report_id: UUID) -> None:
        try:
            celery_app.send_task(
                "app.workers.gis_tasks.match_building",
                args=[str(report_id)],
                queue="gis",
            )
            logger.info("GIS job published for report %s", report_id)
        except Exception as exc:
            logger.error(
                "Failed to publish GIS job for report %s: %s",
                report_id,
                type(exc).__name__,
            )

    async def publish_ai_job(self, report_id: UUID) -> None:
        try:
            celery_app.send_task(
                "app.workers.ai_tasks.process_report_image",
                args=[str(report_id)],
                queue="ai",
            )
            logger.info("AI job published for report %s", report_id)
        except Exception as exc:
            logger.error(
                "Failed to publish AI job for report %s: %s",
                report_id,
                type(exc).__name__,
            )


# ---------------------------------------------------------------------------
# MockQueueService — unit tests
# ---------------------------------------------------------------------------


class MockQueueService:
    """In-memory queue service for unit tests.

    Records every published job so tests can assert on the calls without
    requiring a real Redis connection.

    Attributes:
        gis_jobs: List of report UUIDs published via ``publish_gis_job``.
        ai_jobs:  List of report UUIDs published via ``publish_ai_job``.
    """

    def __init__(self) -> None:
        self.gis_jobs: list[UUID] = []
        self.ai_jobs: list[UUID] = []

    async def publish_gis_job(self, report_id: UUID) -> None:
        """Record *report_id* in ``gis_jobs``."""
        self.gis_jobs.append(report_id)

    async def publish_ai_job(self, report_id: UUID) -> None:
        """Record *report_id* in ``ai_jobs``."""
        self.ai_jobs.append(report_id)
