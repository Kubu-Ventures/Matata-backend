"""Pydantic schemas for analyst dashboard endpoints.

Defined separately from reporter-facing schemas (``app/schemas/report_submission.py``)
so that analyst-only fields (AI scores, building timeline, notes) are never
accidentally exposed on reporter-facing routes.

Spec §13.4 — analyst endpoint response contracts.

Privacy rules enforced here
---------------------------
* ``reporter_trust_tier`` is the **only** reporter identifier ever exported —
  never the hash, never the raw token.
* ``AnalystNoteOut`` omits ``analyst_id_hash`` — notes are visible to all
  analysts but author identity is never exported (spec §11, issue #15).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field

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
# Shared building-timeline item
# ---------------------------------------------------------------------------


class TimelineReportItem(BaseModel):
    """A single report in a building's damage timeline.

    Returned inside ``ReportDetailSchema.building_timeline``.
    Only the fields needed for timeline display are included.
    """

    id: UUID
    damage_severity: ReportDamageSeverity
    status: ReportStatus
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Analyst note (output only — no author identity)
# ---------------------------------------------------------------------------


class AnalystNoteOut(BaseModel):
    """Analyst note as returned by ``POST /analyst/reports/{id}/notes``.

    The ``analyst_id_hash`` field is intentionally absent from this schema.
    Notes are visible to all analysts but author identity must not be exported
    (spec §11 / issue #15 acceptance criteria).
    """

    id: UUID
    body: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Report summary (paginated list)
# ---------------------------------------------------------------------------


class ReportSummarySchema(BaseModel):
    """Condensed report representation for the paginated analyst feed.

    Returned by ``GET /api/v1/analyst/reports``.
    """

    id: UUID
    building_id: Optional[UUID] = None
    crisis_type: CrisisType
    infrastructure_type: InfrastructureType
    damage_severity: ReportDamageSeverity
    status: ReportStatus
    photo_status: PhotoStatus
    lat: Optional[float] = None
    lng: Optional[float] = None
    ai_confidence: Optional[float] = None
    ai_severity_prediction: Optional[ReportDamageSeverity] = None
    ai_divergence: Optional[bool] = None
    analyst_severity_override: Optional[ReportDamageSeverity] = None
    reporter_trust_tier: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Paginated wrapper
# ---------------------------------------------------------------------------


class PaginatedReports(BaseModel):
    """Paginated response envelope for ``GET /api/v1/analyst/reports``."""

    total: int = Field(..., description="Total reports matching the active filters.")
    page: int = Field(..., ge=1)
    limit: int = Field(..., ge=1, le=200)
    items: List[ReportSummarySchema]


# ---------------------------------------------------------------------------
# Full report detail (analyst view)
# ---------------------------------------------------------------------------


class ReportDetailSchema(BaseModel):
    """Full report detail returned by ``GET /api/v1/analyst/reports/{id}``.

    Includes AI results, matched building footprint GeoJSON, analyst notes
    (body + timestamp only — no author identity), and the building damage
    timeline ordered by ``created_at``.

    Privacy: ``reporter_trust_tier`` (int) is the only reporter identifier
    ever exposed — never the hash or raw token (spec §14, issue #15).
    """

    id: UUID
    building_id: Optional[UUID] = None
    footprint_geojson: Optional[str] = None

    # Reporter classification
    crisis_type: CrisisType
    infrastructure_type: InfrastructureType
    damage_severity: ReportDamageSeverity

    # Location
    lat: Optional[float] = None
    lng: Optional[float] = None
    gps_accuracy_m: Optional[float] = None
    landmark_description: Optional[str] = None

    # Optional operational detail
    electricity_status: Optional[ElectricityStatus] = None
    health_services_status: Optional[HealthServicesStatus] = None
    most_pressing_needs: Optional[str] = None
    debris_clearing_needed: Optional[bool] = None

    # Photo pipeline
    photo_url: Optional[str] = None
    photo_status: PhotoStatus

    # AI results (Stage 3)
    ai_quality_score: Optional[float] = None
    ai_severity_prediction: Optional[ReportDamageSeverity] = None
    ai_confidence: Optional[float] = None
    ai_divergence: Optional[bool] = None

    # Analyst AI correction (None = analyst has not overridden the AI)
    analyst_severity_override: Optional[ReportDamageSeverity] = None

    # Workflow
    status: ReportStatus
    possible_duplicate_of_id: Optional[UUID] = None
    duplicate_score: Optional[float] = None

    # Reporter — trust tier only; no hash/token
    reporter_trust_tier: int

    # Analyst notes — body + timestamp only (no author identity)
    analyst_notes: List[AnalystNoteOut] = Field(default_factory=list)

    # Building damage timeline — all associated reports ordered by created_at
    building_timeline: List[TimelineReportItem] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Status transition request
# ---------------------------------------------------------------------------


class StatusTransitionRequest(BaseModel):
    """Request body for ``PATCH /api/v1/analyst/reports/{id}/status``."""

    status: ReportStatus = Field(
        ...,
        description="Target status: verified | rejected | duplicate.",
    )
    reason_code: Optional[str] = Field(
        default=None,
        description=(
            "Required when status == 'rejected'. "
            "Enum: inaccurate | duplicate | poor_quality | out_of_scope | other."
        ),
    )
    notes: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="Optional analyst notes to attach with this transition.",
    )


_REJECTION_REASON_CODES = frozenset(
    {"inaccurate", "duplicate", "poor_quality", "out_of_scope", "other"}
)


# ---------------------------------------------------------------------------
# Manual merge request / response
# ---------------------------------------------------------------------------


class MergeRequest(BaseModel):
    """Request body for ``POST /api/v1/analyst/reports/merge``."""

    primary_id: UUID = Field(..., description="UUID of the primary (surviving) report.")
    duplicate_ids: List[UUID] = Field(
        ...,
        min_length=1,
        description="One or more report UUIDs to merge into the primary.",
    )


class MergeResponse(BaseModel):
    """Response body for ``POST /api/v1/analyst/reports/merge``."""

    primary_id: UUID
    merged_count: int


# ---------------------------------------------------------------------------
# Analyst note creation request
# ---------------------------------------------------------------------------


class AnalystNoteCreateRequest(BaseModel):
    """Request body for ``POST /api/v1/analyst/reports/{id}/notes``."""

    body: str = Field(..., min_length=1, max_length=5000)


# ---------------------------------------------------------------------------
# Stats schemas (public endpoints)
# ---------------------------------------------------------------------------


class SeverityBreakdown(BaseModel):
    minimal: int = 0
    partial: int = 0
    destroyed: int = 0


class CrisisTypeBreakdown(BaseModel):
    flood: int = 0
    earthquake: int = 0
    conflict: int = 0
    wildfire: int = 0
    other: int = 0


class StatsSummaryResponse(BaseModel):
    """Response body for ``GET /api/v1/stats/summary``."""

    total: int
    by_severity: SeverityBreakdown
    by_crisis_type: CrisisTypeBreakdown
    last_updated: datetime


# ---------------------------------------------------------------------------
# Analyst severity override
# ---------------------------------------------------------------------------


class SeverityOverrideRequest(BaseModel):
    """Request body for ``POST /analyst/reports/{id}/severity-override``."""

    analyst_severity_override: ReportDamageSeverity = Field(
        ...,
        description=(
            "Analyst's corrected damage severity assessment. "
            "Does not modify the reporter's damage_severity or the AI's "
            "ai_severity_prediction — stored as a separate field."
        ),
    )


class SeverityOverrideResponse(BaseModel):
    """Response body for ``POST /analyst/reports/{id}/severity-override``."""

    id: UUID
    analyst_severity_override: ReportDamageSeverity


# ---------------------------------------------------------------------------
# Pending merge review — confirm / reject
# ---------------------------------------------------------------------------


class ConfirmMergeResponse(BaseModel):
    """Response body for ``POST /analyst/reports/{id}/confirm-merge``."""

    id: UUID
    status: str
    merged_into: UUID


class RejectMergeResponse(BaseModel):
    """Response body for ``POST /analyst/reports/{id}/reject-merge``."""

    id: UUID
    status: str


# ---------------------------------------------------------------------------
# AI accuracy (active learning metrics)
# ---------------------------------------------------------------------------


class FeedbackTypeBreakdown(BaseModel):
    count: int
    agreement_rate: Optional[float] = None


class AIAccuracyResponse(BaseModel):
    """Response body for ``GET /analyst/ai-accuracy``.

    Summarises how often the AI's severity prediction has agreed with analyst
    decisions across all recorded feedback entries.  Drives calibration of the
    divergence threshold over time.
    """

    total_feedback: int = Field(..., description="Total analyst feedback entries recorded.")
    agreement_rate: Optional[float] = Field(
        None,
        description="Fraction of cases where AI prediction matched analyst decision (0.0–1.0).",
    )
    high_confidence_agreement_rate: Optional[float] = Field(
        None,
        description="Agreement rate restricted to cases where ai_confidence > 0.7.",
    )
    avg_ai_confidence: Optional[float] = Field(
        None,
        description="Mean AI confidence across all feedback entries.",
    )
    by_feedback_type: dict = Field(
        default_factory=dict,
        description="Per-type breakdown: {'verify': {...}, 'reject': {...}, 'severity_override': {...}}.",
    )
    recommended_divergence_threshold: Optional[float] = Field(
        None,
        description=(
            "Suggested divergence confidence threshold derived from observed accuracy. "
            "When high-confidence agreement rate drops below 0.6, a lower threshold "
            "flags more reports for review."
        ),
    )
