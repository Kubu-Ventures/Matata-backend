"""Stub Pydantic schema for Report — full schema defined in a later issue."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import (
    CrisisType,
    InfrastructureType,
    PhotoStatus,
    ReportDamageSeverity,
    ReportStatus,
)


class ReportRead(BaseModel):
    id: UUID
    building_id: Optional[UUID] = None
    crisis_type: CrisisType
    infrastructure_type: InfrastructureType
    damage_severity: ReportDamageSeverity
    lat: float
    lng: float
    status: ReportStatus
    photo_status: PhotoStatus
    reporter_trust_tier: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
