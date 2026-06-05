"""Data export service — spec §12 / issue #16.

Implements three export formats (GeoJSON, CSV, Shapefile) plus an async
job mechanism for large exports (> 10,000 records).

Privacy guarantees (enforced unconditionally at this layer)
-----------------------------------------------------------
* Plaintext phone number — NEVER present.
* ``reporter_token_hash`` — truncated to first 12 characters only.
* ``AnalystNote.body`` — NEVER present.
* Only fields listed in spec §12.1 are exported.

These restrictions are enforced inside ``Anonymiser`` and cannot be
bypassed by any caller, JWT level, URL parameter, or request header.

Schema follows HDX disaster damage dataset standards; field mapping is
documented in ``docs/hdx_schema.md``.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.enums import (
    CrisisType,
    ElectricityStatus,
    HealthServicesStatus,
    InfrastructureType,
    ReportDamageSeverity,
    ReportStatus,
)
from app.models.report import Report

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exportable field definitions — spec §12.1
# ---------------------------------------------------------------------------

# DBF field names must be ≤ 10 characters (ESRI constraint).
# Canonical mapping: Python attribute → DBF column name.
DBF_FIELD_MAP: Dict[str, str] = {
    "report_id": "report_id",
    "building_id": "bldg_id",
    "crisis_type": "crisis_tp",       # was "crisis_type" (11 chars) — fixed
    "infrastructure_type": "infra_type",
    "damage_severity": "dmg_sev",
    "ai_severity_prediction": "ai_sev",
    "ai_confidence": "ai_conf",
    "status": "status",
    "reporter_token_hash_truncated": "rptr_hash",
    "reporter_trust_tier": "trust_tier",
    "lat": "lat",
    "lng": "lng",
    "gps_accuracy_m": "gps_acc_m",
    "landmark_description": "landmark",
    "electricity_status": "elec_stat",
    "health_services_status": "health_st",
    "most_pressing_needs": "needs",
    "debris_clearing_needed": "debris",
    "photo_url": "photo_url",
    "created_at": "created_at",
    "updated_at": "updated_at",
}

# Async threshold — exports above this size are processed asynchronously.
ASYNC_THRESHOLD = 10_000


# ---------------------------------------------------------------------------
# ExportRecord — the anonymised, exportable representation of a Report
# ---------------------------------------------------------------------------


@dataclass
class ExportRecord:
    """Anonymised, exportable representation of a single report.

    This is the ONLY data structure that leaves ``ExportService``.
    All field values are safe for external distribution.
    """

    report_id: str
    building_id: Optional[str]
    crisis_type: str
    infrastructure_type: str
    damage_severity: str
    ai_severity_prediction: Optional[str]
    ai_confidence: Optional[float]
    status: str
    # Hash truncated to 12 chars — non-reversible, non-identifying.
    reporter_token_hash_truncated: str
    reporter_trust_tier: int
    lat: Optional[float]
    lng: Optional[float]
    gps_accuracy_m: Optional[float]
    landmark_description: Optional[str]
    electricity_status: Optional[str]
    health_services_status: Optional[str]
    most_pressing_needs: Optional[str]
    debris_clearing_needed: Optional[bool]
    photo_url: Optional[str]
    created_at: str  # ISO 8601 UTC
    updated_at: str  # ISO 8601 UTC

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain dict of all exportable fields."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ---------------------------------------------------------------------------
# Anonymiser
# ---------------------------------------------------------------------------


class Anonymiser:
    """Transform a ``Report`` ORM instance into an ``ExportRecord``.

    This class applies all redaction rules defined in spec §12.2.
    It is deliberately stateless and can be tested in isolation.
    """

    # Fields that must NEVER appear in any export — enforced here.
    _BLOCKED_FIELDS = frozenset(
        {
            "reporter_token_hash",  # full hash — use truncated version only
            "photo_phash",          # internal dedup field
            "duplicate_of_id",      # internal reference
            "possible_duplicate_of_id",
            "duplicate_score",
            "offline_queued_at",
            "footprint_match_confidence",
            "ai_quality_score",
            "ai_divergence",
        }
    )

    def anonymise(self, report: Report) -> ExportRecord:
        """Convert a ``Report`` ORM instance to an anonymised ``ExportRecord``.

        Args:
            report: SQLAlchemy ``Report`` instance (all scalar columns loaded).

        Returns:
            ``ExportRecord`` with all redaction rules applied.
        """

        def _enum_val(v: Any) -> Optional[str]:
            if v is None:
                return None
            return v.value if hasattr(v, "value") else str(v)

        def _iso(dt: Any) -> str:
            if dt is None:
                return ""
            if isinstance(dt, datetime):
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.isoformat()
            return str(dt)

        # Truncate reporter_token_hash to first 12 characters only.
        raw_hash: str = report.reporter_token_hash or ""
        truncated_hash = raw_hash[:12]

        return ExportRecord(
            report_id=str(report.id),
            building_id=str(report.building_id) if report.building_id else None,
            crisis_type=_enum_val(report.crisis_type) or "",
            infrastructure_type=_enum_val(report.infrastructure_type) or "",
            damage_severity=_enum_val(report.damage_severity) or "",
            ai_severity_prediction=_enum_val(report.ai_severity_prediction),
            ai_confidence=report.ai_confidence,
            status=_enum_val(report.status) or "",
            reporter_token_hash_truncated=truncated_hash,
            reporter_trust_tier=report.reporter_trust_tier,
            lat=report.lat,
            lng=report.lng,
            gps_accuracy_m=report.gps_accuracy_m,
            landmark_description=report.landmark_description,
            electricity_status=_enum_val(report.electricity_status),
            health_services_status=_enum_val(report.health_services_status),
            most_pressing_needs=report.most_pressing_needs,
            debris_clearing_needed=report.debris_clearing_needed,
            photo_url=report.photo_url,
            created_at=_iso(report.created_at),
            updated_at=_iso(report.updated_at),
        )


# ---------------------------------------------------------------------------
# Filter params dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExportFilterParams:
    """Mirrors the filter params accepted by ``GET /analyst/reports``."""

    crisis_type: Optional[List[str]] = field(default=None)
    damage_severity: Optional[List[str]] = field(default=None)
    infrastructure_type: Optional[List[str]] = field(default=None)
    status: Optional[List[str]] = field(default=None)
    time_from: Optional[datetime] = field(default=None)
    time_to: Optional[datetime] = field(default=None)
    min_ai_confidence: Optional[float] = field(default=None)
    include_footprints: bool = field(default=False)


# ---------------------------------------------------------------------------
# ExportService
# ---------------------------------------------------------------------------


class ExportService:
    """Generates GeoJSON, CSV, and Shapefile exports for analyst use.

    All export methods enforce anonymisation unconditionally through
    ``Anonymiser``.  No parameter, JWT, or override can expose raw
    reporter identifiers.

    Args:
        db:            Async SQLAlchemy session.
        analyst_id_hash: Anonymised analyst identifier for the audit log.
    """

    def __init__(self, db: AsyncSession, analyst_id_hash: str) -> None:
        self._db = db
        self._analyst_id_hash = analyst_id_hash
        self._anonymiser = Anonymiser()

    # ------------------------------------------------------------------
    # Public format methods
    # ------------------------------------------------------------------

    async def export_geojson(
        self,
        filters: ExportFilterParams,
        iso_date: str,
    ) -> bytes:
        """Return a GeoJSON FeatureCollection as UTF-8 bytes.

        If ``filters.include_footprints`` is True, building footprint polygons
        are appended as a second FeatureCollection in the response payload.

        Args:
            filters:  Active filter state.
            iso_date: ISO date string used in Content-Disposition filename.

        Returns:
            UTF-8-encoded JSON bytes.
        """
        records = await self._fetch_records(filters)
        await self._write_audit_log(filters, len(records), "geojson")

        features = []
        for rec in records:
            props = rec.to_dict()
            # Remove geometry fields from properties (they live in geometry).
            props.pop("lat", None)
            props.pop("lng", None)

            geometry = None
            if rec.lat is not None and rec.lng is not None:
                geometry = {
                    "type": "Point",
                    "coordinates": [rec.lng, rec.lat],
                }

            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": props,
                }
            )

        collection: Dict[str, Any] = {
            "type": "FeatureCollection",
            "features": features,
        }

        if filters.include_footprints:
            footprint_features = await self._fetch_footprint_features(records)
            footprint_collection: Dict[str, Any] = {
                "type": "FeatureCollection",
                "features": footprint_features,
            }
            payload = {
                "reports": collection,
                "footprints": footprint_collection,
            }
        else:
            payload = collection  # type: ignore[assignment]

        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    async def export_csv(self, filters: ExportFilterParams) -> bytes:
        """Return a UTF-8 CSV with header row as bytes.

        Enforces anti-formula-injection: field values beginning with
        ``=``, ``+``, ``-``, or ``@`` are prefixed with a tab character.

        Args:
            filters: Active filter state.

        Returns:
            UTF-8-encoded CSV bytes (with BOM for Excel compatibility).
        """
        records = await self._fetch_records(filters)
        await self._write_audit_log(filters, len(records), "csv")

        output = io.StringIO()
        if not records:
            writer = csv.DictWriter(
                output,
                fieldnames=list(ExportRecord.__dataclass_fields__.keys()),
            )
            writer.writeheader()
            return output.getvalue().encode("utf-8-sig")

        fieldnames = list(records[0].to_dict().keys())
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()

        for rec in records:
            row = rec.to_dict()
            # Booleans as TRUE/FALSE
            for key, val in row.items():
                if isinstance(val, bool):
                    row[key] = "TRUE" if val else "FALSE"
                elif val is None:
                    row[key] = ""
                else:
                    # Anti-formula injection
                    str_val = str(val)
                    if str_val and str_val[0] in ("=", "+", "-", "@"):
                        row[key] = "\t" + str_val
                    else:
                        row[key] = str_val
            writer.writerow(row)

        return output.getvalue().encode("utf-8-sig")

    async def export_shapefile(self, filters: ExportFilterParams) -> bytes:
        """Return a ZIP archive containing .shp/.dbf/.shx/.prj files.

        Uses GDAL (``osgeo``) with WGS84 CRS (EPSG:4326).
        DBF field names are truncated to 10 characters per ESRI constraint.

        Args:
            filters: Active filter state.

        Returns:
            ZIP archive bytes.

        Raises:
            ImportError: If ``osgeo`` (GDAL Python bindings) is not installed.
        """
        records = await self._fetch_records(filters)
        await self._write_audit_log(filters, len(records), "shapefile")
        return _build_shapefile_zip(records)

    # ------------------------------------------------------------------
    # Async job support
    # ------------------------------------------------------------------

    async def count_records(self, filters: ExportFilterParams) -> int:
        """Return the count of records matching *filters* without fetching them."""
        q = self._build_query(filters)
        count_q = sa.select(sa.func.count()).select_from(q.subquery())
        result = await self._db.execute(count_q)
        return result.scalar_one()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_records(
        self, filters: ExportFilterParams
    ) -> List[ExportRecord]:
        """Query the database and return anonymised export records."""
        query = self._build_query(filters)
        result = await self._db.execute(query)
        reports: Sequence[Report] = result.scalars().all()
        return [self._anonymiser.anonymise(r) for r in reports]

    def _build_query(self, filters: ExportFilterParams) -> sa.Select:
        """Build a SQLAlchemy SELECT applying all active filter predicates."""
        q = sa.select(Report)

        if filters.crisis_type:
            q = q.where(Report.crisis_type.in_(filters.crisis_type))
        if filters.damage_severity:
            q = q.where(Report.damage_severity.in_(filters.damage_severity))
        if filters.infrastructure_type:
            q = q.where(Report.infrastructure_type.in_(filters.infrastructure_type))
        if filters.status:
            q = q.where(Report.status.in_(filters.status))
        if filters.time_from:
            q = q.where(Report.created_at >= filters.time_from)
        if filters.time_to:
            q = q.where(Report.created_at <= filters.time_to)
        if filters.min_ai_confidence is not None:
            q = q.where(Report.ai_confidence >= filters.min_ai_confidence)

        return q.order_by(Report.created_at.desc())

    async def _fetch_footprint_features(
        self, records: List[ExportRecord]
    ) -> List[Dict[str, Any]]:
        """Fetch building footprint GeoJSON for records that have a building_id."""
        from sqlalchemy import text

        building_ids = list(
            {r.building_id for r in records if r.building_id is not None}
        )
        if not building_ids:
            return []

        rows = await self._db.execute(
            text(
                "SELECT id, ST_AsGeoJSON(footprint) AS geojson "
                "FROM building WHERE id = ANY(:ids)"
            ).bindparams(ids=building_ids)
        )
        features = []
        for row in rows:
            try:
                geom = json.loads(row.geojson)
            except (TypeError, json.JSONDecodeError):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {"building_id": str(row.id)},
                }
            )
        return features

    async def _write_audit_log(
        self,
        filters: ExportFilterParams,
        record_count: int,
        fmt: str,
    ) -> None:
        """Write an export event to the AuditLog table."""
        # Serialise filter params to a JSON-safe dict.
        filter_dict: Dict[str, Any] = {}
        if filters.crisis_type:
            filter_dict["crisis_type"] = filters.crisis_type
        if filters.damage_severity:
            filter_dict["damage_severity"] = filters.damage_severity
        if filters.infrastructure_type:
            filter_dict["infrastructure_type"] = filters.infrastructure_type
        if filters.status:
            filter_dict["status"] = filters.status
        if filters.time_from:
            filter_dict["time_from"] = filters.time_from.isoformat()
        if filters.time_to:
            filter_dict["time_to"] = filters.time_to.isoformat()
        if filters.min_ai_confidence is not None:
            filter_dict["min_ai_confidence"] = filters.min_ai_confidence

        entry = AuditLog(
            operation="export.generate",
            actor_id_hash=self._analyst_id_hash,
            record_id=uuid.uuid4(),
            before_state=None,
            after_state={
                "format": fmt,
                "record_count": record_count,
                "filters": filter_dict,
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            },
        )
        self._db.add(entry)
        await self._db.flush()


# ---------------------------------------------------------------------------
# Shapefile builder (GDAL-backed, with graceful fallback)
# ---------------------------------------------------------------------------


def _build_shapefile_zip(records: List[ExportRecord]) -> bytes:
    """Build a ZIP archive containing .shp/.dbf/.shx/.prj files.

    Uses GDAL (``osgeo``) when available.  Raises ``ImportError`` if GDAL
    is not installed (the Dockerfile must include ``libgdal-dev``).

    Args:
        records: List of anonymised export records.

    Returns:
        Raw ZIP bytes.

    Raises:
        ImportError: If the ``osgeo`` package is not available.
    """
    try:
        from osgeo import gdal, ogr, osr  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "GDAL Python bindings (osgeo) are required for Shapefile export. "
            "Add libgdal-dev to the Dockerfile and run: pip install gdal"
        ) from exc

    gdal.UseExceptions()

    with tempfile.TemporaryDirectory() as tmpdir:
        shp_path = f"{tmpdir}/crisismap_export.shp"

        driver: ogr.Driver = ogr.GetDriverByName("ESRI Shapefile")
        datasource: ogr.DataSource = driver.CreateDataSource(shp_path)

        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)

        layer: ogr.Layer = datasource.CreateLayer(
            "crisismap_export", srs=srs, geom_type=ogr.wkbPoint
        )

        # Create DBF fields — names truncated to 10 chars (ESRI constraint).
        # Keys here must match the values in DBF_FIELD_MAP exactly.
        _ogr_type_map = {
            "report_id": (ogr.OFTString, 36),
            "bldg_id": (ogr.OFTString, 36),
            "crisis_tp": (ogr.OFTString, 20),   # was "crisis_type" — fixed to match DBF_FIELD_MAP
            "infra_type": (ogr.OFTString, 20),
            "dmg_sev": (ogr.OFTString, 15),
            "ai_sev": (ogr.OFTString, 15),
            "ai_conf": (ogr.OFTReal, 0),
            "status": (ogr.OFTString, 20),
            "rptr_hash": (ogr.OFTString, 12),
            "trust_tier": (ogr.OFTInteger, 0),
            "lat": (ogr.OFTReal, 0),
            "lng": (ogr.OFTReal, 0),
            "gps_acc_m": (ogr.OFTReal, 0),
            "landmark": (ogr.OFTString, 500),
            "elec_stat": (ogr.OFTString, 20),
            "health_st": (ogr.OFTString, 20),
            "needs": (ogr.OFTString, 1000),
            "debris": (ogr.OFTString, 5),
            "photo_url": (ogr.OFTString, 500),
            "created_at": (ogr.OFTString, 30),
            "updated_at": (ogr.OFTString, 30),
        }

        for dbf_name, (ogr_type, width) in _ogr_type_map.items():
            field_defn = ogr.FieldDefn(dbf_name, ogr_type)
            if width:
                field_defn.SetWidth(width)
            layer.CreateField(field_defn)

        for rec in records:
            feature: ogr.Feature = ogr.Feature(layer.GetLayerDefn())

            if rec.lat is not None and rec.lng is not None:
                point = ogr.Geometry(ogr.wkbPoint)
                point.AddPoint(rec.lng, rec.lat)
                feature.SetGeometry(point)

            d = rec.to_dict()
            py_to_dbf = DBF_FIELD_MAP  # attribute → dbf name

            for py_attr, dbf_col in py_to_dbf.items():
                val = d.get(py_attr)
                if val is None:
                    continue
                if isinstance(val, bool):
                    feature.SetField(dbf_col, "TRUE" if val else "FALSE")
                elif isinstance(val, float):
                    feature.SetField(dbf_col, val)
                elif isinstance(val, int):
                    feature.SetField(dbf_col, val)
                else:
                    feature.SetField(dbf_col, str(val))

            layer.CreateFeature(feature)

        datasource.FlushCache()
        datasource = None  # Close the datasource

        # Collect all generated files into a ZIP archive in memory.
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for suffix in (".shp", ".dbf", ".shx", ".prj"):
                file_path = shp_path.replace(".shp", suffix)
                import os
                if os.path.exists(file_path):
                    zf.write(file_path, arcname=f"crisismap_export{suffix}")

        return zip_buffer.getvalue()