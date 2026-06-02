"""Celery application configuration and queue routing.

A single ``celery_app`` instance is shared by all workers.  Routing is
configured so that each task type delivers to the correct dedicated queue:

* ``gis``           — building footprint matching (``celery-gis`` worker)
* ``ai``            — image quality assessment and damage classification
* ``notifications`` — analyst alerts and reporter photo requests
* ``export``        — GeoJSON / CSV / Shapefile generation

The broker and result backend default to ``REDIS_URL`` but can be overridden
independently via ``CELERY_BROKER_URL`` and ``CELERY_RESULT_BACKEND``.
"""

from __future__ import annotations

from celery import Celery

from app.core.config import settings

# ---------------------------------------------------------------------------
# Broker and backend URLs
# ---------------------------------------------------------------------------

_broker_url: str = getattr(settings, "CELERY_BROKER_URL", None) or settings.REDIS_URL
_result_backend: str = (
    getattr(settings, "CELERY_RESULT_BACKEND", None) or settings.REDIS_URL
)

# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------

celery_app = Celery(
    "crisismap",
    broker=_broker_url,
    backend=_result_backend,
    include=[
        "app.workers.gis_tasks",
        "app.workers.ai_tasks",
    ],
)

# ---------------------------------------------------------------------------
# Celery configuration
# ---------------------------------------------------------------------------

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Timezone
    timezone="UTC",
    enable_utc=True,
    # Reliability — acknowledge task only after execution, not on receipt.
    # This ensures tasks are re-queued if the worker crashes mid-execution.
    task_acks_late=True,
    # Worker prefetch: 1 task at a time prevents slow tasks from starving the queue.
    worker_prefetch_multiplier=1,
    # Retry policy defaults
    task_default_retry_delay=30,  # seconds
    task_max_retries=3,
    # Queue routing
    task_routes={
        "app.workers.gis_tasks.*": {"queue": "gis"},
        "app.workers.ai_tasks.*": {"queue": "ai"},
    },
    # Default queue (for tasks without explicit routing)
    task_default_queue="default",
    # Result TTL — keep task results for 1 hour
    result_expires=3600,
    # Critical: prevents Celery from eagerly connecting to the broker
    # at import time, which would cause ImportError in test environments
    # where Redis is not available.
    broker_connection_retry_on_startup=True,
    task_always_eager=False,
)

# ---------------------------------------------------------------------------
# Queue definitions
# ---------------------------------------------------------------------------

# Declare all queues so that workers starting with -Q <name> find them.
celery_app.conf.task_queues = {}  # Celery creates queues on-the-fly by default.
