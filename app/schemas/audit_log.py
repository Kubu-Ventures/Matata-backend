"""Stub Pydantic schema for AuditLog."""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


class AuditLogRead(BaseModel):
    id: UUID
    operation: str
    actor_id_hash: str
    record_id: UUID
    before_state: Optional[Any] = None
    after_state: Any
    created_at: datetime

    model_config = {"from_attributes": True}
