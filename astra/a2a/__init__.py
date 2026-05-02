"""
A2A (Agent-to-Agent) protocol implementation for Astra.

This package implements the A2A open standard for inter-agent communication.
It provides both a client (for Astra Core to talk to agents) and a server
base (for agents to receive tasks from Astra or other A2A clients).

Key concepts:
- AgentCard: Describes an agent's identity, capabilities, and endpoint
- Task: A unit of work with lifecycle (submitted → working → completed/failed)
- Message: Communication within a task (text, files, structured data)
- Skill: A specific capability an agent advertises
"""

from astra.a2a.models import (
    AgentCard,
    AgentSkill,
    Message,
    MessagePart,
    Task,
    TaskState,
    TaskSendParams,
)

__all__ = [
    "AgentCard",
    "AgentSkill",
    "Message",
    "MessagePart",
    "Task",
    "TaskState",
    "TaskSendParams",
]
