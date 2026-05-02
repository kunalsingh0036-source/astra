"""
Template management endpoints.

CRUD for Meta-approved WhatsApp message templates.
Includes sync from Meta API.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.engine import get_session
from gateway.models.template import Template, TemplateStatus
from gateway.services.meta_api import MetaAPIClient

router = APIRouter(prefix="/api/v1/templates", tags=["Templates"])


@router.get("/")
async def list_templates(
    status: str | None = None,
    agent_tag: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """List all templates with optional filters."""
    query = select(Template)

    if status:
        try:
            query = query.where(Template.meta_status == TemplateStatus(status))
        except ValueError:
            pass

    result = await session.execute(query.order_by(Template.name))
    templates = result.scalars().all()

    # Filter by agent tag in Python (JSONB contains)
    if agent_tag:
        templates = [
            t for t in templates
            if not t.agent_tags or agent_tag in t.agent_tags
        ]

    return [
        {
            "id": str(t.id),
            "name": t.name,
            "language": t.language,
            "category": t.category,
            "status": t.meta_status.value,
            "agent_tags": t.agent_tags or [],
            "last_synced": t.last_synced_at.isoformat() if t.last_synced_at else None,
        }
        for t in templates
    ]


class UpdateTemplateRequest(BaseModel):
    agent_tags: list[str] | None = None


@router.put("/{template_name}")
async def update_template(
    template_name: str,
    req: UpdateTemplateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Update template metadata (e.g., which agents can use it)."""
    result = await session.execute(
        select(Template).where(Template.name == template_name)
    )
    template = result.scalar_one_or_none()

    if not template:
        raise HTTPException(404, f"Template '{template_name}' not found")

    if req.agent_tags is not None:
        template.agent_tags = req.agent_tags

    await session.commit()
    return {"status": "updated", "name": template_name}


@router.post("/sync")
async def sync_templates(
    session: AsyncSession = Depends(get_session),
):
    """Sync templates from Meta API.

    Fetches all templates from Meta and updates the local registry.
    """
    client = MetaAPIClient()
    try:
        meta_templates = await client.get_templates()
    finally:
        await client.close()

    if not meta_templates:
        return {"synced": 0, "message": "No templates found or API not configured"}

    synced = 0
    now = datetime.now(timezone.utc)

    for mt in meta_templates:
        name = mt.get("name", "")
        if not name:
            continue

        result = await session.execute(
            select(Template).where(Template.name == name)
        )
        existing = result.scalar_one_or_none()

        status_map = {
            "APPROVED": TemplateStatus.APPROVED,
            "PENDING": TemplateStatus.PENDING,
            "REJECTED": TemplateStatus.REJECTED,
        }
        meta_status = status_map.get(
            mt.get("status", "PENDING"), TemplateStatus.PENDING
        )

        if existing:
            existing.language = mt.get("language", "en")
            existing.category = mt.get("category", "marketing").lower()
            existing.components = mt.get("components", [])
            existing.meta_status = meta_status
            existing.last_synced_at = now
        else:
            template = Template(
                name=name,
                language=mt.get("language", "en"),
                category=mt.get("category", "marketing").lower(),
                components=mt.get("components", []),
                meta_status=meta_status,
                last_synced_at=now,
            )
            session.add(template)

        synced += 1

    await session.commit()
    return {"synced": synced}
