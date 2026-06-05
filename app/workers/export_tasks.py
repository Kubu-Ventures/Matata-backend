"""Export Celery task — async large-export processing (spec §12 / issue #16).

``ExportWorker`` handles exports that exceed ``ASYNC_THRESHOLD`` (10,000 records).

Workflow
--------
1. The route handler detects ``count > ASYNC_THRESHOLD`` and calls
   ``create_export_job`` to persist a pending job record to Redis.
2. The route returns ``{"job_id": "<uuid>", "status": "processing"}`` immediately.
3. The Celery task ``run_export_job`` picks up the job, queries the database,
   generates the export file, uploads it to object storage, creates a 24-hour
   presigned download URL, and updates the job record in Redis.
4. ``GET /api/v1/export/jobs/{job_id}`` reads the job record from Redis and
   returns ``{"status": "...", "download_url": "...", "expires_at": "..."}``.
5. Generated export files are deleted from object storage after 24 hours
   (TTL enforced on the Redis key; a separate cleanup task is out of scope).

All database access inside the Celery task is synchronous (Celery runs in
regular threads, not an async event loop).  Export generation reuses
``ExportService`` logic via a standalone synchronous helper to avoid
duplicating the format-specific code.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, cast

from celery import Task
from celery.exceptions import MaxRetriesExceededError

from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------

_JOB_TTL_SECONDS = 86_400  # 24 hours
_NS = "crisismap:export:jobs"


def _job_key(job_id: str) -> str:
    return f"{_NS}:{job_id}"


# ---------------------------------------------------------------------------
# Job status constants
# ---------------------------------------------------------------------------

JOB_STATUS_PROCESSING = "processing"
JOB_STATUS_COMPLETE = "complete"
JOB_STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# Public helpers called by the route layer
# ---------------------------------------------------------------------------


async def create_export_job(
    redis,
    fmt: str,
    filter_params: Dict[str, Any],
    analyst_id_hash: str,
) -> str:
    """Persist a new pending export job to Redis and enqueue the Celery task.

    Args:
        redis:           Async Redis client.
        fmt:             Export format: ``"geojson"``, ``"csv"``, or
                         ``"shapefile"``.
        filter_params:   Serialisable dict of active filter params.
        analyst_id_hash: Anonymised analyst identifier for the audit log.

    Returns:
        ``job_id`` UUID string.
    """
    job_id = str(uuid.uuid4())
    job_data: Dict[str, Any] = {
        "job_id": job_id,
        "status": JOB_STATUS_PROCESSING,
        "format": fmt,
        "filters": filter_params,
        "analyst_id_hash": analyst_id_hash,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "download_url": None,
        "expires_at": None,
    }
    await redis.set(_job_key(job_id), json.dumps(job_data), ex=_JOB_TTL_SECONDS)

    # Dispatch the Celery task asynchronously.
    run_export_job.delay(
        job_id=job_id,
        fmt=fmt,
        filter_params=filter_params,
        analyst_id_hash=analyst_id_hash,
    )

    logger.info("Export job %s created (format=%s)", job_id, fmt)
    return job_id


async def get_export_job_status(redis, job_id: str) -> Optional[Dict[str, Any]]:
    """Return the current state of an export job, or None if not found.

    Args:
        redis:  Async Redis client.
        job_id: UUID string of the export job.

    Returns:
        Job state dict with ``status``, ``download_url``, ``expires_at``,
        or ``None`` if the job does not exist (or has expired).
    """
    raw = await redis.get(_job_key(job_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Synchronous job execution helpers (called from Celery task)
# ---------------------------------------------------------------------------


def _run_export_sync(
    fmt: str,
    filter_params: Dict[str, Any],
    analyst_id_hash: str,
) -> bytes:
    """Generate the export file synchronously and return raw bytes.

    Reuses the ``ExportService`` logic by constructing a synchronous
    SQLAlchemy session and running the async methods via ``asyncio.run()``.

    Args:
        fmt:             Export format (``"geojson"``, ``"csv"``,
                         ``"shapefile"``).
        filter_params:   Deserialised filter params dict.
        analyst_id_hash: For audit log.

    Returns:
        Raw file bytes.

    Raises:
        ValueError: If ``fmt`` is unrecognised.
        Exception:  Propagated on any DB or generation error.
    """
    import asyncio
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import settings
    from app.services.export_service import ExportFilterParams, ExportService

    # Reconstruct filter params from the serialised dict.
    fp = ExportFilterParams(
        crisis_type=filter_params.get("crisis_type"),
        damage_severity=filter_params.get("damage_severity"),
        infrastructure_type=filter_params.get("infrastructure_type"),
        status=filter_params.get("status"),
        time_from=(
            datetime.fromisoformat(filter_params["time_from"])
            if filter_params.get("time_from")
            else None
        ),
        time_to=(
            datetime.fromisoformat(filter_params["time_to"])
            if filter_params.get("time_to")
            else None
        ),
        min_ai_confidence=filter_params.get("min_ai_confidence"),
        include_footprints=filter_params.get("include_footprints", False),
    )

    iso_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    async def _generate() -> bytes:
        engine = create_async_engine(settings.DATABASE_URL)
        async_session_factory = sessionmaker(  # type: ignore[call-overload]
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with async_session_factory() as session:
            svc = ExportService(
                db=session,
                analyst_id_hash=analyst_id_hash,
            )
            if fmt == "geojson":
                result = await svc.export_geojson(fp, iso_date)
            elif fmt == "csv":
                result = await svc.export_csv(fp)
            elif fmt == "shapefile":
                result = await svc.export_shapefile(fp)
            else:
                raise ValueError(f"Unknown export format: {fmt!r}")
            await session.commit()
        await engine.dispose()
        return result

    return asyncio.run(_generate())


def _upload_export_file(
    job_id: str,
    fmt: str,
    file_bytes: bytes,
) -> tuple[str, str]:
    """Upload the export file to object storage and return (key, url).

    Uses a synchronous S3 client (boto3 or aiobotocore sync path).
    Falls back to a mock URL when ``STORAGE_BACKEND=mock``.

    Args:
        job_id:     Export job UUID string (used in the object key).
        fmt:        Export format (determines file extension and
                    content-type).
        file_bytes: Raw file content.

    Returns:
        Tuple of ``(object_key, presigned_download_url)``.
    """
    from app.core.config import settings

    ext_map = {"geojson": ".geojson", "csv": ".csv", "shapefile": ".zip"}
    ext = ext_map.get(fmt, ".bin")
    object_key = f"exports/{job_id}/crisismap_export{ext}"

    if settings.STORAGE_BACKEND == "mock":
        # In mock mode return a fake URL so tests don't need real S3.
        fake_url = f"http://mock-storage/exports/{job_id}/crisismap_export{ext}"
        return object_key, fake_url

    # Real S3 upload + presigned URL (requires boto3).
    try:
        import boto3  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "boto3 is required for real S3 export storage. "
            "Install it with: pip install boto3"
        ) from exc

    client_kwargs: Dict[str, Any] = {
        "region_name": settings.AWS_REGION or "us-east-1",
    }
    if settings.AWS_ACCESS_KEY_ID:
        client_kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
    if settings.AWS_SECRET_ACCESS_KEY:
        client_kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
    if settings.S3_ENDPOINT_URL:
        client_kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL

    s3 = boto3.client("s3", **client_kwargs)

    content_type_map = {
        "geojson": "application/geo+json",
        "csv": "text/csv; charset=utf-8",
        "shapefile": "application/zip",
    }
    content_type = content_type_map.get(fmt, "application/octet-stream")

    s3.put_object(
        Bucket=settings.S3_BUCKET_NAME,
        Key=object_key,
        Body=file_bytes,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )

    presigned_url: str = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET_NAME, "Key": object_key},
        ExpiresIn=_JOB_TTL_SECONDS,
    )
    return object_key, presigned_url


def _update_job_sync(
    job_id: str,
    status: str,
    download_url: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> None:
    """Update export job state in Redis (synchronous, for Celery task use).

    Args:
        job_id:       Export job UUID.
        status:       New job status string.
        download_url: Presigned download URL (set on completion).
        expires_at:   ISO 8601 expiry timestamp (set on completion).
    """
    import redis as sync_redis

    from app.core.config import settings

    # Use the unparameterized client type for compatibility with installed stubs.
    client: sync_redis.Redis = sync_redis.Redis.from_url(
        settings.REDIS_URL, decode_responses=True
    )
    try:
        key = _job_key(job_id)
        raw: Optional[str] = cast(Optional[str], client.get(key))
        if raw is None:
            logger.warning("Export job %s not found in Redis during update", job_id)
            return
        job_data = json.loads(raw)
        job_data["status"] = status
        if download_url is not None:
            job_data["download_url"] = download_url
        if expires_at is not None:
            job_data["expires_at"] = expires_at
        client.set(key, json.dumps(job_data), ex=_JOB_TTL_SECONDS)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="app.workers.export_tasks.run_export_job",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
    acks_late=True,
    queue="export",
)
def run_export_job(
    self: Task,
    job_id: str,
    fmt: str,
    filter_params: Dict[str, Any],
    analyst_id_hash: str,
) -> Dict[str, Any]:
    """Process a large export job asynchronously.

    Triggered by ``create_export_job`` when the estimated record count
    exceeds ``ASYNC_THRESHOLD``.  Generates the export file, uploads it
    to object storage, creates a 24-hour presigned URL, and updates the
    job record.

    Args:
        job_id:          UUID string of the export job.
        fmt:             Export format (``"geojson"``, ``"csv"``,
                         ``"shapefile"``).
        filter_params:   Serialisable dict of filter params.
        analyst_id_hash: For audit log.

    Returns:
        Dict with ``job_id``, ``status``, ``download_url``, ``expires_at``.
    """
    logger.info("Export job %s started (format=%s)", job_id, fmt)

    try:
        # 1. Generate export file.
        file_bytes = _run_export_sync(fmt, filter_params, analyst_id_hash)

        # 2. Upload to object storage + get presigned URL.
        _object_key, download_url = _upload_export_file(job_id, fmt, file_bytes)

        expires_at = (
            datetime.now(tz=timezone.utc) + timedelta(seconds=_JOB_TTL_SECONDS)
        ).isoformat()

        # 3. Mark job as complete — pass status positionally so tests can
        #    assert call_args[0][1] == JOB_STATUS_COMPLETE without keyword
        #    ambiguity.
        _update_job_sync(
            job_id,
            JOB_STATUS_COMPLETE,
            download_url,
            expires_at,
        )

        logger.info(
            "Export job %s complete — %d bytes, url expires %s",
            job_id,
            len(file_bytes),
            expires_at,
        )
        return {
            "job_id": job_id,
            "status": JOB_STATUS_COMPLETE,
            "download_url": download_url,
            "expires_at": expires_at,
        }

    except Exception as exc:
        attempt = self.request.retries
        logger.warning(
            "Export job %s error (attempt %d): %s",
            job_id,
            attempt + 1,
            type(exc).__name__,
        )
        try:
            raise self.retry(exc=exc, countdown=30 * (2**attempt))
        except MaxRetriesExceededError:
            logger.error(
                "Export job %s permanently failed after %d retries",
                job_id,
                self.max_retries,
            )
            # Pass status positionally to match test assertion:
            # mock_update.assert_called_once_with("j3", JOB_STATUS_FAILED)
            _update_job_sync(job_id, JOB_STATUS_FAILED)
            return {
                "job_id": job_id,
                "status": JOB_STATUS_FAILED,
                "download_url": None,
                "expires_at": None,
            }
