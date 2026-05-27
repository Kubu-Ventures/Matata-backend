"""Stub Pydantic schema for Notification."""

from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel

from app.models.enums import NotificationStatus, NotificationType


class NotificationRead(BaseModel):
    id: UUID
    type: NotificationType
    recipient_hash: str
    report_id: UUID
    status: NotificationStatus
    sent_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}
