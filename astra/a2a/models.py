"""
A2A protocol data models.

These define the wire format for agent-to-agent communication.
Based on the open A2A specification with pragmatic extensions for Astra.

All models are Pydantic v2 — they serialize to/from JSON cleanly,
validate on construction, and generate JSON Schema for documentation.
"""

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent Card — how agents advertise themselves
# ---------------------------------------------------------------------------


class AgentSkill(BaseModel):
    """A specific capability an agent offers.

    Skills are the unit of work routing — when Astra needs to decide
    which agent handles a task, it matches the task against skill descriptions.
    """

    id: str = Field(description="Unique skill identifier (e.g., 'tech-briefing')")
    name: str = Field(description="Human-readable skill name")
    description: str = Field(description="What this skill does — used for routing")
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema describing expected input (optional)",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema describing expected output (optional)",
    )
    tags: list[str] = Field(
        default_factory=list,
        description="Tags for categorization and search",
    )


class AgentAuthentication(BaseModel):
    """How to authenticate with this agent."""

    schemes: list[str] = Field(
        default_factory=lambda: ["bearer"],
        description="Supported auth schemes (bearer, api_key, none)",
    )
    required: bool = Field(
        default=False,
        description="Whether authentication is required",
    )


class AgentCapabilities(BaseModel):
    """What protocol features this agent supports."""

    streaming: bool = Field(
        default=False,
        description="Can stream partial results as the task progresses",
    )
    push_notifications: bool = Field(
        default=False,
        description="Can send webhook notifications on task state changes",
    )
    batch: bool = Field(
        default=False,
        description="Can accept multiple tasks in a single request",
    )
    cancellation: bool = Field(
        default=True,
        description="Supports task cancellation",
    )


class AgentCard(BaseModel):
    """Describes an agent's identity, endpoint, and capabilities.

    Published at /.well-known/agent.json by each A2A-compatible agent.
    Astra fetches these to discover what agents exist and what they can do.

    This is the A2A equivalent of an API spec — but for agents, not endpoints.
    """

    name: str = Field(description="Agent's unique name in the fleet")
    description: str = Field(description="What this agent does — 1-2 sentences")
    url: str = Field(description="Base URL of the agent's A2A endpoint")
    version: str = Field(default="0.1.0", description="Agent version")
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    skills: list[AgentSkill] = Field(
        default_factory=list,
        description="List of skills this agent offers",
    )
    authentication: AgentAuthentication = Field(
        default_factory=AgentAuthentication,
    )
    model_tier: str = Field(
        default="sonnet",
        description="Default LLM tier this agent uses",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary metadata (owner, build date, etc.)",
    )


# ---------------------------------------------------------------------------
# Task — the unit of work
# ---------------------------------------------------------------------------


class TaskState(str, enum.Enum):
    """Lifecycle states for a task."""

    SUBMITTED = "submitted"    # Received, not yet started
    WORKING = "working"        # Agent is actively working on it
    COMPLETED = "completed"    # Done successfully
    FAILED = "failed"          # Done with error
    CANCELLED = "cancelled"    # Cancelled by the client
    EXPIRED = "expired"        # Timed out


class MessageRole(str, enum.Enum):
    """Who sent the message."""

    CLIENT = "client"   # Astra (the requester)
    AGENT = "agent"     # The agent handling the task


class MessagePart(BaseModel):
    """A single piece of content within a message.

    Messages can contain multiple parts — text + files + structured data.
    This is more flexible than plain text and supports rich agent output.
    """

    type: str = Field(
        description="Content type: 'text', 'file', 'json', 'error'",
    )
    content: str | dict[str, Any] = Field(
        description="The actual content — text string, file path, or JSON object",
    )
    mime_type: str | None = Field(
        default=None,
        description="MIME type for files (e.g., 'application/pdf')",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Part-specific metadata",
    )


class Message(BaseModel):
    """A message within a task conversation.

    Tasks are not single request-response — they can involve back-and-forth.
    The client sends the initial message, the agent responds with progress
    updates and final results.
    """

    role: MessageRole = Field(description="Who sent this message")
    parts: list[MessagePart] = Field(description="Content parts")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Message-level metadata (e.g., model used, tokens consumed)",
    )


class TaskSendParams(BaseModel):
    """Parameters for creating/sending a new task to an agent.

    This is what Astra sends when it wants an agent to do something.
    """

    skill_id: str | None = Field(
        default=None,
        description="Target a specific skill. None = let the agent decide.",
    )
    message: Message = Field(description="The initial message/instruction")
    priority: int = Field(
        default=5,
        description="1 (lowest) to 10 (highest). Affects queue ordering.",
    )
    timeout_seconds: int = Field(
        default=300,
        description="Max time for the agent to complete the task",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Task-level metadata (e.g., requester context)",
    )


class Task(BaseModel):
    """A task — the fundamental unit of work in A2A.

    Created when Astra sends work to an agent. Tracks the full lifecycle
    from submission through completion, including all messages exchanged.
    """

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique task identifier",
    )
    agent_name: str = Field(description="Which agent is handling this task")
    state: TaskState = Field(
        default=TaskState.SUBMITTED,
        description="Current lifecycle state",
    )
    messages: list[Message] = Field(
        default_factory=list,
        description="Full conversation history for this task",
    )
    result: MessagePart | None = Field(
        default=None,
        description="Final result (set when state=completed)",
    )
    error: str | None = Field(
        default=None,
        description="Error message (set when state=failed)",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    priority: int = Field(default=5)
    timeout_seconds: int = Field(default=300)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Whether this task is in a final state (no more updates expected)."""
        return self.state in (
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.EXPIRED,
        )

    def add_message(self, message: Message) -> None:
        """Append a message and update the timestamp."""
        self.messages.append(message)
        self.updated_at = datetime.now(timezone.utc)

    def complete(self, result: MessagePart) -> None:
        """Mark task as completed with a result."""
        self.state = TaskState.COMPLETED
        self.result = result
        self.updated_at = datetime.now(timezone.utc)

    def fail(self, error: str) -> None:
        """Mark task as failed with an error message."""
        self.state = TaskState.FAILED
        self.error = error
        self.updated_at = datetime.now(timezone.utc)

    def cancel(self) -> None:
        """Cancel the task."""
        self.state = TaskState.CANCELLED
        self.updated_at = datetime.now(timezone.utc)
