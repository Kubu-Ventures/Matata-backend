"""Pydantic schemas for report submission endpoints.

Defines request and response models for:
* ``POST /api/v1/reports``           — report creation
* ``PATCH /api/v1/reports/{id}/photo`` — photo upload (offline sync)
* ``GET /api/v1/reports/{id}``       — reporter's own report
* ``GET /api/v1/reports/nearby``     — pre-submission duplicate check

All schemas are strict about field types and length constraints.
Sanitisation (HTML stripping) is performed in the service layer — these
schemas handle structural validation only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.models.enums import (
    CrisisType,
    ElectricityStatus,
    HealthServicesStatus,
    InfrastructureType,
    PhotoStatus,
    ReportDamageSeverity,
    ReportStatus,
)

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class ReportCreateSchema(BaseModel):
    """Metadata JSON object submitted alongside a photo in ``POST /reports``.

    Sent as a JSON string in the ``metadata`` field of the multipart form.
    The validation here is structural; the service layer performs sanitisation.
    """

    crisis_type: CrisisType = Field(
        ...,
        description="Type of crisis that caused the damage.",
    )
    infrastructure_type: InfrastructureType = Field(
        ...,
        description="Category of the affected structure or infrastructure.",
    )
    damage_severity: ReportDamageSeverity = Field(
        ...,
        description="Reporter-assessed degree of damage.",
    )

    # ── Location — one of lat/lng or landmark_description must be present ───
    lat: Optional[float] = Field(
        default=None,
        ge=-90.0,
        le=90.0,
        description="WGS84 latitude in decimal degrees.",
    )
    lng: Optional[float] = Field(
        default=None,
        ge=-180.0,
        le=180.0,
        description="WGS84 longitude in decimal degrees.",
    )
    gps_accuracy_m: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Device-reported horizontal GPS accuracy in metres.",
    )
    landmark_description: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Textual landmark when GPS is unavailable. Max 500 characters.",
    )

    # ── Optional operational fields ─────────────────────────────────────────
    electricity_status: Optional[ElectricityStatus] = Field(
        default=None,
        description="Current electricity status at the location.",
    )
    health_services_status: Optional[HealthServicesStatus] = Field(
        default=None,
        description="Accessibility of health services.",
    )
    most_pressing_needs: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Free text describing the most urgent needs. Max 1,000 characters.",
    )
    debris_clearing_needed: Optional[bool] = Field(
        default=None,
        description="Whether debris clearing is required.",
    )
    offline_queued_at: Optional[datetime] = Field(
        default=None,
        description="ISO 8601 timestamp — present only for offline-synced submissions.",
    )

    @model_validator(mode="after")
    def require_location(self) -> "ReportCreateSchema":
        """Enforce: either (lat AND lng) or landmark_description must be present.

        Raises:
            ValueError: If neither coordinate pair nor landmark is provided.
        """
        has_coords = self.lat is not None and self.lng is not None
        has_landmark = bool(self.landmark_description)
        if not has_coords and not has_landmark:
            raise ValueError(
                "Either (lat, lng) coordinates or a landmark_description must be provided."
            )
        return self


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ReportCreateResponse(BaseModel):
    """Response body for a successful ``POST /reports``."""

    id: UUID = Field(..., description="UUID of the created report.")
    status: ReportStatus = Field(
        ..., description="Initial workflow status (always 'pending')."
    )
    building_id: Optional[UUID] = Field(
        default=None,
        description="Matched building UUID, or null until the GIS worker resolves it.",
    )


class ReportPhotoResponse(BaseModel):
    """Response body for a successful ``PATCH /reports/{id}/photo``."""

    id: UUID = Field(..., description="UUID of the updated report.")
    photo_url: str = Field(..., description="Object storage key of the uploaded photo.")
    status: str = Field(..., description="Photo pipeline status after upload.")


class ReportDetailResponse(BaseModel):
    """Full report detail returned by ``GET /reports/{id}``."""

    id: UUID
    building_id: Optional[UUID] = None
    crisis_type: CrisisType
    infrastructure_type: InfrastructureType
    damage_severity: ReportDamageSeverity
    lat: Optional[float] = None
    lng: Optional[float] = None
    gps_accuracy_m: Optional[float] = None
    landmark_description: Optional[str] = None
    electricity_status: Optional[ElectricityStatus] = None
    health_services_status: Optional[HealthServicesStatus] = None
    most_pressing_needs: Optional[str] = None
    debris_clearing_needed: Optional[bool] = None
    photo_url: Optional[str] = None
    photo_status: PhotoStatus
    status: ReportStatus
    reporter_trust_tier: int
    ai_severity_prediction: Optional[ReportDamageSeverity] = None
    ai_confidence: Optional[float] = None
    ai_quality_score: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class NearbyReportItem(BaseModel):
    """A single nearby report returned by ``GET /reports/nearby``."""

    id: UUID
    lat: float
    lng: float
    status: ReportStatus
    damage_severity: ReportDamageSeverity
    created_at: datetime
    similarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="GPS-proximity-based similarity score (0 = distant, 1 = identical location).",
    )
