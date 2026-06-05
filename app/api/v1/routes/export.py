"""Export route handlers — spec §12 / issue #16.

All routes are registered under the ``/api/v1/export`` prefix.

Endpoints
---------
``GET /export/geojson``       — GeoJSON FeatureCollection export.
``GET /export/csv``           — UTF-8 CSV export.
``GET /export/shapefile``     — Shapefile ZIP export.
``GET /export/jobs/{job_id}`` — Async export job status.

Role enforcement
----------------
All export endpoints require ``analyst`` or ``responder`` role.
Anonymisation is enforced unconditionally in ``ExportService`` — it cannot
be bypassed via the dashboard interface or by calling the API directly with
an analyst JWT.

Async export
------------
If the number of records matching the active filters exceeds 10,000, the
endpoint returns immediately with a job ID and processing status rather than
blocking.  The ``ExportWorker`` Celery task handles the actual generation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.routes.auth import require_role
from app.core.dependencies import get_db, get_redis
from app.services.auth_service import Role
from app.services.export_service import (
    ASYNC_THRESHOLD,
    ExportFilterParams,
    ExportService,
)
from app.workers.export_tasks import (
    create_export_job,
    get_export_job_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/export", tags=["Export"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _analyst_id_hash(current_user: dict) -> str:
    return current_user.get("sub", "")


def _build_filters(
    crisis_type: Optional[str],
    damage_severity: Optional[str],
    infrastructure_type: Optional[str],
    report_status: Optional[str],
    time_from: Optional[datetime],
    time_to: Optional[datetime],
    min_ai_confidence: Optional[float],
    include_footprints: bool = False,
) -> ExportFilterParams:
    """Parse comma-separated filter strings into an ``ExportFilterParams``."""

    def _split(value: Optional[str]) -> Optional[List[str]]:
        if not value:
            return None
        return [v.strip() for v in value.split(",") if v.strip()]

    return ExportFilterParams(
        crisis_type=_split(crisis_type),
        damage_severity=_split(damage_severity),
        infrastructure_type=_split(infrastructure_type),
        status=_split(report_status),
        time_from=time_from,
        time_to=time_to,
        min_ai_confidence=min_ai_confidence,
        include_footprints=include_footprints,
    )


def _filters_to_dict(filters: ExportFilterParams) -> dict:
    """Serialise ``ExportFilterParams`` to a JSON-safe dict for Celery dispatch."""
    d: dict = {}
    if filters.crisis_type:
        d["crisis_type"] = filters.crisis_type
    if filters.damage_severity:
        d["damage_severity"] = filters.damage_severity
    if filters.infrastructure_type:
        d["infrastructure_type"] = filters.infrastructure_type
    if filters.status:
        d["status"] = filters.status
    if filters.time_from:
        d["time_from"] = filters.time_from.isoformat()
    if filters.time_to:
        d["time_to"] = filters.time_to.isoformat()
    if filters.min_ai_confidence is not None:
        d["min_ai_confidence"] = filters.min_ai_confidence
    d["include_footprints"] = filters.include_footprints
    return d


# ---------------------------------------------------------------------------
# Shared query params (reused across all three export endpoints)
# ---------------------------------------------------------------------------

_COMMON_PARAMS = dict(
    crisis_type=Query(default=None, description="Comma-separated crisis types."),
    damage_severity=Query(default=None, description="Comma-separated severity levels."),
    infrastructure_type=Query(
        default=None, description="Comma-separated infrastructure types."
    ),
    report_status=Query(
        default=None, alias="status", description="Comma-separated report statuses."
    ),
    time_from=Query(default=None, description="ISO 8601 UTC lower bound for created_at."),
    time_to=Query(default=None, description="ISO 8601 UTC upper bound for created_at."),
    min_ai_confidence=Query(
        default=None, ge=0.0, le=1.0, description="Minimum AI confidence threshold."
    ),
)


# ---------------------------------------------------------------------------
# GET /export/geojson
# ---------------------------------------------------------------------------


@router.get(
    "/geojson",
    summary="Export reports as GeoJSON",
    description=(
        "Returns a GeoJSON FeatureCollection of filtered reports. "
        "If ``include_footprints=true`` is set, building footprint polygons are "
        "appended as a separate FeatureCollection. "
        "Exports > 10,000 records are processed asynchronously — the endpoint "
        "returns a job ID immediately. "
        "Requires ``analyst`` or ``responder`` role."
    ),
    responses={
        200: {
            "content": {
                "application/geo+json": {},
                "application/json": {},
            },
            "description": "GeoJSON export or async job response.",
        }
    },
)
async def export_geojson(
    crisis_type: Optional[str] = Query(default=None),
    damage_severity: Optional[str] = Query(default=None),
    infrastructure_type: Optional[str] = Query(default=None),
    report_status: Optional[str] = Query(default=None, alias="status"),
    time_from: Optional[datetime] = Query(default=None),
    time_to: Optional[datetime] = Query(default=None),
    min_ai_confidence: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    include_footprints: bool = Query(default=False),
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    filters = _build_filters(
        crisis_type,
        damage_severity,
        infrastructure_type,
        report_status,
        time_from,
        time_to,
        min_ai_confidence,
        include_footprints,
    )
    svc = ExportService(db=db, analyst_id_hash=_analyst_id_hash(current_user))
    count = await svc.count_records(filters)

    if count > ASYNC_THRESHOLD:
        job_id = await create_export_job(
            redis=redis,
            fmt="geojson",
            filter_params=_filters_to_dict(filters),
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        return JSONResponse({"job_id": job_id, "status": "processing"})

    iso_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    payload = await svc.export_geojson(filters, iso_date)
    await db.commit()

    return Response(
        content=payload,
        media_type="application/geo+json",
        headers={
            "Content-Disposition": (
                f'attachment; filename="crisismap_export_{iso_date}.geojson"'
            )
        },
    )


# ---------------------------------------------------------------------------
# GET /export/csv
# ---------------------------------------------------------------------------


@router.get(
    "/csv",
    summary="Export reports as CSV",
    description=(
        "Returns a UTF-8 CSV file with a header row. "
        "Booleans as TRUE/FALSE, timestamps in ISO 8601 UTC. "
        "Formula injection is prevented by prefixing dangerous characters. "
        "Exports > 10,000 records are processed asynchronously. "
        "Requires ``analyst`` or ``responder`` role."
    ),
    responses={
        200: {
            "content": {
                "text/csv; charset=utf-8": {},
                "application/json": {},
            },
            "description": "CSV export or async job response.",
        }
    },
)
async def export_csv(
    crisis_type: Optional[str] = Query(default=None),
    damage_severity: Optional[str] = Query(default=None),
    infrastructure_type: Optional[str] = Query(default=None),
    report_status: Optional[str] = Query(default=None, alias="status"),
    time_from: Optional[datetime] = Query(default=None),
    time_to: Optional[datetime] = Query(default=None),
    min_ai_confidence: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    filters = _build_filters(
        crisis_type,
        damage_severity,
        infrastructure_type,
        report_status,
        time_from,
        time_to,
        min_ai_confidence,
    )
    svc = ExportService(db=db, analyst_id_hash=_analyst_id_hash(current_user))
    count = await svc.count_records(filters)

    if count > ASYNC_THRESHOLD:
        job_id = await create_export_job(
            redis=redis,
            fmt="csv",
            filter_params=_filters_to_dict(filters),
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        return JSONResponse({"job_id": job_id, "status": "processing"})

    iso_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    payload = await svc.export_csv(filters)
    await db.commit()

    return Response(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="crisismap_export_{iso_date}.csv"'
            )
        },
    )


# ---------------------------------------------------------------------------
# GET /export/shapefile
# ---------------------------------------------------------------------------


@router.get(
    "/shapefile",
    summary="Export reports as Shapefile ZIP",
    description=(
        "Returns a ZIP archive containing .shp/.dbf/.shx/.prj files. "
        "CRS: WGS84 (EPSG:4326). DBF field names truncated to 10 characters. "
        "Requires GDAL (libgdal-dev) in the container. "
        "Exports > 10,000 records are processed asynchronously. "
        "Requires ``analyst`` or ``responder`` role."
    ),
    responses={
        200: {
            "content": {
                "application/zip": {},
                "application/json": {},
            },
            "description": "Shapefile ZIP or async job response.",
        }
    },
)
async def export_shapefile(
    crisis_type: Optional[str] = Query(default=None),
    damage_severity: Optional[str] = Query(default=None),
    infrastructure_type: Optional[str] = Query(default=None),
    report_status: Optional[str] = Query(default=None, alias="status"),
    time_from: Optional[datetime] = Query(default=None),
    time_to: Optional[datetime] = Query(default=None),
    min_ai_confidence: Optional[float] = Query(default=None, ge=0.0, le=1.0),
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    filters = _build_filters(
        crisis_type,
        damage_severity,
        infrastructure_type,
        report_status,
        time_from,
        time_to,
        min_ai_confidence,
    )
    svc = ExportService(db=db, analyst_id_hash=_analyst_id_hash(current_user))
    count = await svc.count_records(filters)

    if count > ASYNC_THRESHOLD:
        job_id = await create_export_job(
            redis=redis,
            fmt="shapefile",
            filter_params=_filters_to_dict(filters),
            analyst_id_hash=_analyst_id_hash(current_user),
        )
        return JSONResponse({"job_id": job_id, "status": "processing"})

    iso_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        payload = await svc.export_shapefile(filters)
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=(
                "Shapefile export requires GDAL. "
                "Please contact the system administrator. "
                f"Detail: {exc}"
            ),
        ) from exc
    await db.commit()

    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="crisismap_export_{iso_date}.zip"'
            )
        },
    )


# ---------------------------------------------------------------------------
# GET /export/jobs/{job_id}
# ---------------------------------------------------------------------------


@router.get(
    "/jobs/{job_id}",
    summary="Get async export job status",
    description=(
        "Returns the current status of an asynchronous export job. "
        "``status`` is one of: ``processing``, ``complete``, ``failed``. "
        "``download_url`` and ``expires_at`` are populated when status is "
        "``complete``. Download URLs are valid for 24 hours. "
        "Requires ``analyst`` or ``responder`` role."
    ),
)
async def get_export_job(
    job_id: str,
    current_user: dict = Depends(require_role(Role.analyst, Role.responder)),
    redis: Redis = Depends(get_redis),
) -> dict:
    job = await get_export_job_status(redis, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export job {job_id!r} not found or has expired.",
        )
    return {
        "status": job.get("status"),
        "download_url": job.get("download_url"),
        "expires_at": job.get("expires_at"),
    }
