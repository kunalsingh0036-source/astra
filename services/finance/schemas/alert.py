"""Alert schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from finance.models.alert import AlertSeverity, AlertType


class AlertOut(BaseModel):
    id: uuid.UUID
    business_id: uuid.UUID
    type: AlertType
    severity: AlertSeverity
    title: str
    message: str
    is_read: bool
    is_resolved: bool
    related_entity_type: str | None
    related_entity_id: uuid.UUID | None
    extra_data: dict
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class AlertUpdate(BaseModel):
    is_read: bool | None = None
    is_resolved: bool | None = None
