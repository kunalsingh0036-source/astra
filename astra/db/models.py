"""
Central model registry.

Import all SQLAlchemy models here so Alembic can discover them
for autogenerate migrations. Any new model added to the project
must be imported in this file.
"""

from astra.autonomy.models import AuditEvent
from astra.db.engine import Base
from astra.memory.models import Memory
from astra.notes.models import AppleNote
from astra.tasks.models import Task
from astra.telemetry.models import UsageEvent

__all__ = ["AppleNote", "AuditEvent", "Base", "Memory", "Task", "UsageEvent"]
