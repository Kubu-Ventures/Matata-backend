"""Stub Pydantic schema for Building — full schema defined in a later issue."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import BuildingSource, DamageSeverity


class BuildingRead(BaseModel):
    id: UUID
    source: BuildingSource
    external_id: str
    current_severity: DamageSeverity
    last_report_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
