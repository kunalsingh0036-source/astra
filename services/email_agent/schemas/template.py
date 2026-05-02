"""Email template schemas."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class TemplateCreate(BaseModel):
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=300)
    subject_template: str = Field(..., max_length=500)
    body_template: str
    category: str | None = Field(None, max_length=50)
    variables: dict = Field(default_factory=dict)


class TemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    subject_template: str | None = None
    body_template: str | None = None
    category: str | None = None
    variables: dict | None = None


class TemplateOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    subject_template: str
    body_template: str
    category: str | None
    variables: dict
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
