"""
Tests for A2A Server — the base class for agent services.

Uses a concrete test implementation to verify the server's HTTP
endpoints and task lifecycle management.
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from astra.a2a.server import A2AServer
from astra.a2a.models import (
    AgentCard,
    AgentSkill,
    Message,
    MessagePart,
    MessageRole,
    Task,
    TaskSendParams,
    TaskState,
)


# ---------------------------------------------------------------------------
# Test agent implementation
# ---------------------------------------------------------------------------


class EchoAgent(A2AServer):
    """A simple agent that echoes back the input. For testing."""

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        # Extract the text from the first message part
        input_text = ""
        for part in params.message.parts:
            if part.type == "text" and isinstance(part.content, str):
                input_text += part.content

        # Echo it back
        result = MessagePart(
            type="text",
            content=f"Echo: {input_text}",
        )
        task.complete(result)
        return task


class FailingAgent(A2AServer):
    """An agent that always fails. For testing error handling."""

    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        raise ValueError("Intentional failure for testing")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def echo_card():
    return AgentCard(
        name="echo-agent",
        description="Echoes back input for testing",
        url="http://localhost:8200",
        skills=[
            AgentSkill(
                id="echo",
                name="Echo",
                description="Echoes the input back",
            )
        ],
    )


@pytest.fixture
def echo_agent(echo_card):
    return EchoAgent(card=echo_card)


@pytest.fixture
def echo_app(echo_agent):
    app = FastAPI()
    app.include_router(echo_agent.router)
    app.include_router(echo_agent.well_known_router)
    return app


@pytest.fixture
def failing_app():
    card = AgentCard(
        name="failing-agent",
        description="Always fails",
        url="http://localhost:8201",
    )
    agent = FailingAgent(card=card)
    app = FastAPI()
    app.include_router(agent.router)
    app.include_router(agent.well_known_router)
    return app


# ---------------------------------------------------------------------------
# Agent Card endpoint
# ---------------------------------------------------------------------------


class TestAgentCardEndpoint:
    @pytest.mark.asyncio
    async def test_get_agent_card(self, echo_app):
        """Serves the Agent Card at /.well-known/agent.json."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/.well-known/agent.json")

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "echo-agent"
        assert len(data["skills"]) == 1
        assert data["skills"][0]["id"] == "echo"


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_check(self, echo_app):
        """Health endpoint returns healthy status."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/a2a/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["agent"] == "echo-agent"
        assert data["active_tasks"] == 0


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_create_and_complete_task(self, echo_app):
        """Send a task and get a completed result."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            params = TaskSendParams(
                message=Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="Hello agent")],
                ),
            )
            response = await client.post(
                "/a2a/tasks",
                json=params.model_dump(mode="json"),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "completed"
        assert data["result"]["content"] == "Echo: Hello agent"
        assert data["agent_name"] == "echo-agent"

    @pytest.mark.asyncio
    async def test_create_task_with_skill(self, echo_app):
        """Send a task targeting a specific skill."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            params = TaskSendParams(
                skill_id="echo",
                message=Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="Skill test")],
                ),
            )
            response = await client.post(
                "/a2a/tasks",
                json=params.model_dump(mode="json"),
            )

        assert response.status_code == 200
        assert response.json()["state"] == "completed"

    @pytest.mark.asyncio
    async def test_create_task_invalid_skill(self, echo_app):
        """Requesting a nonexistent skill returns 404."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            params = TaskSendParams(
                skill_id="nonexistent-skill",
                message=Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="test")],
                ),
            )
            response = await client.post(
                "/a2a/tasks",
                json=params.model_dump(mode="json"),
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_task(self, echo_app):
        """Retrieve a task by ID after creation."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Create
            params = TaskSendParams(
                message=Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="Get test")],
                ),
            )
            create_response = await client.post(
                "/a2a/tasks",
                json=params.model_dump(mode="json"),
            )
            task_id = create_response.json()["id"]

            # Get
            get_response = await client.get(f"/a2a/tasks/{task_id}")

        assert get_response.status_code == 200
        assert get_response.json()["id"] == task_id
        assert get_response.json()["state"] == "completed"

    @pytest.mark.asyncio
    async def test_get_task_not_found(self, echo_app):
        """Getting a nonexistent task returns 404."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/a2a/tasks/nonexistent-id")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_list_tasks(self, echo_app):
        """List all tasks."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Create two tasks
            for text in ["task 1", "task 2"]:
                params = TaskSendParams(
                    message=Message(
                        role=MessageRole.CLIENT,
                        parts=[MessagePart(type="text", content=text)],
                    ),
                )
                await client.post(
                    "/a2a/tasks",
                    json=params.model_dump(mode="json"),
                )

            # List
            response = await client.get("/a2a/tasks")

        assert response.status_code == 200
        tasks = response.json()
        assert len(tasks) == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_task_failure_captured(self, failing_app):
        """Agent failure is captured as task.state=failed."""
        transport = ASGITransport(app=failing_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            params = TaskSendParams(
                message=Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="will fail")],
                ),
            )
            response = await client.post(
                "/a2a/tasks",
                json=params.model_dump(mode="json"),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "failed"
        assert "Intentional failure" in data["error"]


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_completed_task_fails(self, echo_app):
        """Cannot cancel a task that's already completed."""
        transport = ASGITransport(app=echo_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Create (completes immediately)
            params = TaskSendParams(
                message=Message(
                    role=MessageRole.CLIENT,
                    parts=[MessagePart(type="text", content="done")],
                ),
            )
            create_response = await client.post(
                "/a2a/tasks",
                json=params.model_dump(mode="json"),
            )
            task_id = create_response.json()["id"]

            # Try to cancel
            cancel_response = await client.post(
                f"/a2a/tasks/{task_id}/cancel"
            )

        assert cancel_response.status_code == 409  # Conflict
