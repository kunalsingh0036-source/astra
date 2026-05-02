"""
Tests for A2A protocol data models.

Validates that all models serialize/deserialize correctly,
enforce constraints, and handle lifecycle transitions properly.
"""

import pytest
from datetime import datetime, timezone

from astra.a2a.models import (
    AgentCard,
    AgentCapabilities,
    AgentAuthentication,
    AgentSkill,
    Message,
    MessagePart,
    MessageRole,
    Task,
    TaskSendParams,
    TaskState,
)


# ---------------------------------------------------------------------------
# AgentCard
# ---------------------------------------------------------------------------


class TestAgentCard:
    def test_minimal_card(self):
        """An Agent Card with only required fields."""
        card = AgentCard(
            name="test-agent",
            description="A test agent",
            url="http://localhost:8100",
        )
        assert card.name == "test-agent"
        assert card.version == "0.1.0"
        assert card.capabilities.streaming is False
        assert card.skills == []
        assert card.authentication.required is False

    def test_full_card(self):
        """An Agent Card with all fields populated."""
        card = AgentCard(
            name="research-intel",
            description="Research and intelligence specialist",
            url="http://localhost:8100",
            version="1.0.0",
            capabilities=AgentCapabilities(
                streaming=True,
                push_notifications=True,
                batch=False,
                cancellation=True,
            ),
            skills=[
                AgentSkill(
                    id="tech-briefing",
                    name="Technology Briefing",
                    description="Research latest tech developments",
                    tags=["research", "tech", "ai"],
                ),
                AgentSkill(
                    id="competitor-analysis",
                    name="Competitor Analysis",
                    description="Deep analysis of a company or product",
                    tags=["research", "competitive"],
                ),
            ],
            authentication=AgentAuthentication(
                schemes=["bearer"],
                required=True,
            ),
            model_tier="sonnet",
            metadata={"owner": "kunal", "build_date": "2026-04-01"},
        )
        assert len(card.skills) == 2
        assert card.skills[0].id == "tech-briefing"
        assert card.capabilities.streaming is True
        assert card.authentication.required is True
        assert card.metadata["owner"] == "kunal"

    def test_card_serialization(self):
        """Agent Card round-trips through JSON correctly."""
        card = AgentCard(
            name="test",
            description="Test",
            url="http://localhost:8100",
            skills=[
                AgentSkill(
                    id="skill-1",
                    name="Skill One",
                    description="Does thing one",
                )
            ],
        )
        data = card.model_dump(mode="json")
        restored = AgentCard(**data)
        assert restored.name == card.name
        assert restored.skills[0].id == "skill-1"


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


class TestTask:
    def test_create_task(self):
        """New task starts in SUBMITTED state."""
        task = Task(agent_name="test-agent")
        assert task.state == TaskState.SUBMITTED
        assert task.result is None
        assert task.error is None
        assert task.messages == []
        assert task.id  # UUID generated

    def test_task_complete(self):
        """Task.complete() sets state and result."""
        task = Task(agent_name="test-agent", state=TaskState.WORKING)
        result = MessagePart(type="text", content="Here are the results")

        task.complete(result)

        assert task.state == TaskState.COMPLETED
        assert task.result.content == "Here are the results"
        assert task.is_terminal() is True

    def test_task_fail(self):
        """Task.fail() sets state and error."""
        task = Task(agent_name="test-agent", state=TaskState.WORKING)

        task.fail("Something went wrong")

        assert task.state == TaskState.FAILED
        assert task.error == "Something went wrong"
        assert task.is_terminal() is True

    def test_task_cancel(self):
        """Task.cancel() sets terminal state."""
        task = Task(agent_name="test-agent", state=TaskState.WORKING)

        task.cancel()

        assert task.state == TaskState.CANCELLED
        assert task.is_terminal() is True

    def test_task_add_message(self):
        """Messages accumulate on the task."""
        task = Task(agent_name="test-agent")

        msg1 = Message(
            role=MessageRole.CLIENT,
            parts=[MessagePart(type="text", content="Do this")],
        )
        msg2 = Message(
            role=MessageRole.AGENT,
            parts=[MessagePart(type="text", content="Done")],
        )

        task.add_message(msg1)
        task.add_message(msg2)

        assert len(task.messages) == 2
        assert task.messages[0].role == MessageRole.CLIENT
        assert task.messages[1].role == MessageRole.AGENT

    def test_terminal_states(self):
        """All terminal states are correctly identified."""
        for state in [
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.EXPIRED,
        ]:
            task = Task(agent_name="test", state=state)
            assert task.is_terminal() is True

        for state in [TaskState.SUBMITTED, TaskState.WORKING]:
            task = Task(agent_name="test", state=state)
            assert task.is_terminal() is False

    def test_task_serialization(self):
        """Task round-trips through JSON."""
        task = Task(
            agent_name="test-agent",
            state=TaskState.COMPLETED,
            messages=[
                Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="Research AI")],
                )
            ],
            result=MessagePart(type="text", content="Results here"),
            priority=8,
        )

        data = task.model_dump(mode="json")
        restored = Task(**data)

        assert restored.agent_name == "test-agent"
        assert restored.state == TaskState.COMPLETED
        assert restored.result.content == "Results here"
        assert len(restored.messages) == 1
        assert restored.priority == 8


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


class TestMessage:
    def test_text_message(self):
        """Simple text message."""
        msg = Message(
            role=MessageRole.CLIENT,
            parts=[MessagePart(type="text", content="Hello agent")],
        )
        assert msg.role == MessageRole.CLIENT
        assert msg.parts[0].content == "Hello agent"
        assert msg.timestamp is not None

    def test_multi_part_message(self):
        """Message with multiple parts (text + structured data)."""
        msg = Message(
            role=MessageRole.AGENT,
            parts=[
                MessagePart(type="text", content="Here's what I found:"),
                MessagePart(
                    type="json",
                    content={"companies": ["OpenAI", "Anthropic"]},
                ),
            ],
        )
        assert len(msg.parts) == 2
        assert msg.parts[1].type == "json"
        assert isinstance(msg.parts[1].content, dict)

    def test_file_part(self):
        """Message with a file attachment."""
        msg = Message(
            role=MessageRole.AGENT,
            parts=[
                MessagePart(
                    type="file",
                    content="/tmp/report.pdf",
                    mime_type="application/pdf",
                )
            ],
        )
        assert msg.parts[0].mime_type == "application/pdf"


# ---------------------------------------------------------------------------
# TaskSendParams
# ---------------------------------------------------------------------------


class TestTaskSendParams:
    def test_minimal_params(self):
        """Params with just a message."""
        params = TaskSendParams(
            message=Message(
                role=MessageRole.CLIENT,
                parts=[MessagePart(type="text", content="Do research")],
            ),
        )
        assert params.skill_id is None
        assert params.priority == 5
        assert params.timeout_seconds == 300

    def test_full_params(self):
        """Params with all fields set."""
        params = TaskSendParams(
            skill_id="tech-briefing",
            message=Message(
                role=MessageRole.CLIENT,
                parts=[MessagePart(type="text", content="Research AI agents")],
            ),
            priority=9,
            timeout_seconds=600,
            metadata={"requester": "astra-core"},
        )
        assert params.skill_id == "tech-briefing"
        assert params.priority == 9
        assert params.metadata["requester"] == "astra-core"
