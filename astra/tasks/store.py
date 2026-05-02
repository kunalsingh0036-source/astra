"""
Task persistence helpers. Called by the `astra-tasks` MCP server and
by the web API's /api/tasks route.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from astra.db.engine import async_session
from astra.tasks.models import Task, TaskStatus


async def add_task(
    title: str,
    *,
    note: str = "",
    due_at: datetime | None = None,
    priority: int = 1,
    tags: str = "",
    source: str = "user",
) -> dict[str, Any]:
    async with async_session() as session:
        task = Task(
            title=title,
            note=note or "",
            due_at=due_at,
            priority=max(0, min(3, int(priority))),
            tags=tags or "",
            source=source,
        )
        session.add(task)
        await session.commit()
        return _to_dict(task)


async def list_tasks(
    *,
    status: str | None = None,
    limit: int = 100,
    include_done: bool = False,
) -> list[dict[str, Any]]:
    async with async_session() as session:
        stmt = select(Task).order_by(Task.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Task.status == status)
        elif not include_done:
            stmt = stmt.where(Task.status == TaskStatus.OPEN.value)
        result = await session.execute(stmt)
        return [_to_dict(t) for t in result.scalars()]


async def complete_task(task_id: int) -> dict[str, Any] | None:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return None
        task.status = TaskStatus.DONE.value
        task.completed_at = datetime.now(timezone.utc)
        await session.commit()
        return _to_dict(task)


async def update_task(
    task_id: int,
    *,
    title: str | None = None,
    note: str | None = None,
    due_at: datetime | None = None,
    priority: int | None = None,
    tags: str | None = None,
    status: str | None = None,
) -> dict[str, Any] | None:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return None
        if title is not None:
            task.title = title
        if note is not None:
            task.note = note
        if due_at is not None:
            task.due_at = due_at
        if priority is not None:
            task.priority = max(0, min(3, int(priority)))
        if tags is not None:
            task.tags = tags
        if status is not None:
            task.status = status
            if status == TaskStatus.DONE.value and task.completed_at is None:
                task.completed_at = datetime.now(timezone.utc)
        await session.commit()
        return _to_dict(task)


async def delete_task(task_id: int) -> bool:
    async with async_session() as session:
        task = await session.get(Task, task_id)
        if task is None:
            return False
        await session.delete(task)
        await session.commit()
        return True


def _to_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "note": task.note or "",
        "status": task.status,
        "priority": task.priority,
        "tags": task.tags or "",
        "source": task.source,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "due_at": task.due_at.isoformat() if task.due_at else None,
    }
