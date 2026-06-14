"""Health check route handlers.

Three endpoints are registered under the root path (not /api/v1 — health
endpoints must be reachable before API versioning middleware runs):

GET /health        — liveness probe (no I/O, always fast)
GET /health/ready  — readiness probe (checks Postgres, Redis, storage)
GET /health/worker — worker probe (checks Celery queue depths)

All endpoints are intentionally excluded from authentication requirements
so that orchestrators (Kubernetes, Docker Swarm, load balancers) can probe
them without credentials.  The /metrics endpoint IS authenticated — see
app/api/v1/routes/metrics.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import get_db, get_redis
from app.schemas.health import (
    HealthResponse,
    ReadinessResponse,
    WorkerHealthResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["Health"])

# ---------------------------------------------------------------------------
# Liveness — GET /health
# ---------------------------------------------------------------------------


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description=(
        "Returns ``{status: ok}`` immediately with no downstream I/O. "
        "Should never return a non-2xx response while the process is running."
    ),
)
async def liveness() -> HealthResponse:
    """Liveness probe — no blocking I/O."""
    return HealthResponse(status="ok", version="0.1.0")


# ---------------------------------------------------------------------------
# Readiness — GET /health/ready
# ---------------------------------------------------------------------------


async def _check_postgres(db: AsyncSession) -> str:
    """Return 'ok' or an error string for PostgreSQL."""
    try:
        await db.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("readiness_check_postgres_failed", error=str(exc))
        return f"error: {type(exc).__name__}"


async def _check_redis(redis: Redis) -> str:
    """Return 'ok' or an error string for Redis."""
    try:
        await redis.ping()
        return "ok"
    except Exception as exc:
        logger.warning("readiness_check_redis_failed", error=str(exc))
        return f"error: {type(exc).__name__}"


async def _check_storage() -> str:
    """Return 'ok' or an error string for object storage.

    Uses the configured storage backend.  In mock mode this always returns
    'ok' without network I/O.  In S3 mode a HEAD request is issued against
    a sentinel key (``health/sentinel``) to verify bucket accessibility.
    """
    if settings.STORAGE_BACKEND == "mock":
        return "ok"

    try:
        import aiobotocore.session  # type: ignore[import]

        session = aiobotocore.session.get_session()
        client_kwargs: dict = {
            "region_name": settings.AWS_REGION or "us-east-1",
        }
        if settings.AWS_ACCESS_KEY_ID:
            client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        if settings.AWS_SECRET_ACCESS_KEY:
            client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        if settings.S3_ENDPOINT_URL:
            client_kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL

        async with session.create_client("s3", **client_kwargs) as s3:
            await s3.head_object(
                Bucket=settings.S3_BUCKET_NAME,
                Key="health/sentinel",
            )
        return "ok"
    except Exception as exc:  # noqa: BLE001
        # ClientError with 404 means bucket is reachable — sentinel just
        # doesn't exist yet, which is acceptable.
        err_name = type(exc).__name__
        if "404" in str(exc) or "NoSuchKey" in err_name:
            return "ok"
        logger.warning("readiness_check_storage_failed", error=str(exc))
        return f"error: {err_name}"


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    description=(
        "Checks PostgreSQL (SELECT 1), Redis (PING), and object storage "
        "(HEAD sentinel). Returns HTTP 200 only when all checks pass; "
        "HTTP 503 if any check fails. Response is never cached."
    ),
    responses={
        200: {"description": "All checks passed — service is ready."},
        503: {"description": "One or more checks failed — service is not ready."},
    },
)
async def readiness(
    response: Response,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ReadinessResponse:
    """Readiness probe with active downstream dependency checks."""
    # Run all checks concurrently
    pg_status, redis_status, storage_status = await asyncio.gather(
        _check_postgres(db),
        _check_redis(redis),
        _check_storage(),
    )

    checks: dict[str, str] = {
        "postgres": pg_status,
        "redis": redis_status,
        "storage": storage_status,
    }

    all_ok = all(v == "ok" for v in checks.values())

    if all_ok:
        overall = "ready"
    else:
        overall = "degraded"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Prevent caching by proxies / orchestrators
    response.headers["Cache-Control"] = "no-store"

    logger.info("readiness_probe", status=overall, checks=checks)
    return ReadinessResponse(status=overall, checks=checks)


# ---------------------------------------------------------------------------
# Worker health — GET /health/worker
# ---------------------------------------------------------------------------


def _get_celery_inspect() -> Any:  # noqa: ANN401
    """Return a Celery inspect handle.

    Lazy import avoids broker connection at startup.
    """
    from app.workers.celery_app import celery_app  # noqa: PLC0415

    return celery_app.control.inspect(timeout=3.0)


@router.get(
    "/health/worker",
    response_model=WorkerHealthResponse,
    summary="Worker health probe",
    description=(
        "Queries Celery inspect().active() and inspect().reserved() to "
        "return per-queue task counts. Returns HTTP 503 if any queue depth "
        "exceeds ``AI_PROCESSING_QUEUE_ALERT_DEPTH``."
    ),
    responses={
        200: {"description": "All queues within acceptable depth."},
        503: {"description": "At least one queue exceeds the alert threshold."},
    },
)
async def worker_health(response: Response) -> WorkerHealthResponse:
    """Probe Celery worker queue depths."""
    inspect = _get_celery_inspect()

    try:
        active_raw: dict[str, list[Any]] | None = inspect.active()
        reserved_raw: dict[str, list[Any]] | None = inspect.reserved()
    except Exception as exc:  # noqa: BLE001
        logger.warning("worker_health_inspect_failed", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return WorkerHealthResponse(
            status="unavailable",
            queues={},
            alert=True,
        )

    active_raw = active_raw or {}
    reserved_raw = reserved_raw or {}

    # Aggregate task counts per queue name
    queue_depths: dict[str, int] = {}

    def _count_by_queue(
        tasks_by_worker: dict[str, list[Any]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for tasks in tasks_by_worker.values():
            for task in tasks:
                q = task.get("delivery_info", {}).get("routing_key", "default")
                counts[q] = counts.get(q, 0) + 1
        return counts

    for queue, count in _count_by_queue(active_raw).items():
        queue_depths[queue] = queue_depths.get(queue, 0) + count
    for queue, count in _count_by_queue(reserved_raw).items():
        queue_depths[queue] = queue_depths.get(queue, 0) + count

    threshold = settings.AI_PROCESSING_QUEUE_ALERT_DEPTH
    alert = any(depth > threshold for depth in queue_depths.values())

    overall = "alert" if alert else "ok"
    if alert:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    logger.info(
        "worker_health_probe",
        status=overall,
        queue_depths=queue_depths,
        threshold=threshold,
    )

    return WorkerHealthResponse(
        status=overall,
        queues=queue_depths,
        alert=alert,
    )
