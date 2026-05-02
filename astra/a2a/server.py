"""
A2A Server — base class that makes any agent A2A-compatible.

Any agent you build inherits from A2AServer and gets:
- Agent Card endpoint (/.well-known/agent.json)
- Task management endpoints (create, get, cancel, message)
- Health check endpoint
- Task lifecycle management
- Built-in FastAPI router ready to mount

Usage:
    class MyAgent(A2AServer):
        async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
            # Do the actual work
            result = await self.do_research(params.message)
            task.complete(MessagePart(type="text", content=result))
            return task

    agent = MyAgent(
        card=AgentCard(name="my-agent", description="...", url="..."),
    )

    # Mount in a FastAPI app:
    app = FastAPI()
    app.include_router(agent.router)
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from astra.a2a.models import (
    AgentCard,
    Message,
    MessagePart,
    MessageRole,
    Task,
    TaskSendParams,
    TaskState,
)

logger = logging.getLogger(__name__)


class A2AServer(ABC):
    """Base class for A2A-compatible agent servers.

    Provides the HTTP interface (as a FastAPI router) and task lifecycle
    management. Subclasses only need to implement handle_task().

    The server manages:
    - Task storage (in-memory, backed by dict)
    - Task state transitions
    - Agent Card serving
    - Health checks
    - Message routing to tasks
    """

    def __init__(self, card: AgentCard, max_concurrent_tasks: int = 10):
        self.card = card
        self._tasks: dict[str, Task] = {}
        self._max_concurrent = max_concurrent_tasks
        self._active_count = 0
        self._router = APIRouter(prefix="/a2a", tags=["A2A Protocol"])

        # Register routes
        self._register_routes()

        # Also register the well-known route (no prefix)
        self._well_known_router = APIRouter(tags=["A2A Discovery"])
        self._well_known_router.add_api_route(
            "/.well-known/agent.json",
            self._get_agent_card,
            methods=["GET"],
            summary="Agent Card (A2A Discovery)",
        )

    @property
    def router(self) -> APIRouter:
        """The FastAPI router for A2A endpoints.

        Mount this on your FastAPI app:
            app.include_router(agent.router)
            app.include_router(agent.well_known_router)
        """
        return self._router

    @property
    def well_known_router(self) -> APIRouter:
        """Router for the /.well-known/agent.json endpoint."""
        return self._well_known_router

    @abstractmethod
    async def handle_task(self, task: Task, params: TaskSendParams) -> Task:
        """Process a task. Subclasses implement the actual agent logic here.

        The task is already created with state=WORKING when this is called.
        The implementation should:
        1. Read the params.message for the instruction
        2. Do the work (call LLMs, search the web, etc.)
        3. Set the result via task.complete(result_part) or task.fail(error)
        4. Return the task

        Args:
            task: The task object (state=WORKING, messages populated)
            params: The original send parameters

        Returns:
            The task with updated state (completed or failed)
        """
        ...

    async def handle_message(self, task: Task, message: Message) -> Task:
        """Handle a follow-up message on an existing task.

        Override this for multi-turn agent interactions.
        Default implementation adds the message and returns the task unchanged.

        Args:
            task: The existing task
            message: The new message from the client

        Returns:
            Updated task
        """
        task.add_message(message)
        return task

    # ----- FastAPI route handlers -----

    def _register_routes(self) -> None:
        """Register all A2A protocol routes on the router."""

        self._router.add_api_route(
            "/health",
            self._health_check,
            methods=["GET"],
            summary="Health check",
        )
        self._router.add_api_route(
            "/tasks",
            self._create_task,
            methods=["POST"],
            summary="Create a new task",
        )
        self._router.add_api_route(
            "/tasks/{task_id}",
            self._get_task,
            methods=["GET"],
            summary="Get task status",
        )
        self._router.add_api_route(
            "/tasks/{task_id}/cancel",
            self._cancel_task,
            methods=["POST"],
            summary="Cancel a task",
        )
        self._router.add_api_route(
            "/tasks/{task_id}/messages",
            self._add_message,
            methods=["POST"],
            summary="Send follow-up message to a task",
        )
        self._router.add_api_route(
            "/tasks",
            self._list_tasks,
            methods=["GET"],
            summary="List all tasks",
        )

    async def _get_agent_card(self) -> dict:
        """Serve the Agent Card at /.well-known/agent.json."""
        return self.card.model_dump(mode="json")

    async def _health_check(self) -> dict:
        """Health check endpoint."""
        return {
            "status": "healthy",
            "agent": self.card.name,
            "active_tasks": self._active_count,
            "max_concurrent": self._max_concurrent,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _create_task(self, params: TaskSendParams) -> dict:
        """Create and start processing a new task.

        The task is created immediately and returned to the client.
        If the agent supports synchronous completion (fast tasks),
        the result may be included in the response.
        """
        # Check capacity
        if self._active_count >= self._max_concurrent:
            raise HTTPException(
                status_code=429,
                detail=f"Agent at capacity ({self._max_concurrent} concurrent tasks)",
            )

        # Validate skill if specified
        if params.skill_id:
            skill_ids = [s.id for s in self.card.skills]
            if params.skill_id not in skill_ids:
                raise HTTPException(
                    status_code=404,
                    detail=f"Skill '{params.skill_id}' not found. "
                    f"Available: {skill_ids}",
                )

        # Create the task
        task = Task(
            agent_name=self.card.name,
            state=TaskState.WORKING,
            messages=[params.message],
            priority=params.priority,
            timeout_seconds=params.timeout_seconds,
            metadata=params.metadata,
        )
        self._tasks[task.id] = task
        self._active_count += 1

        logger.info(
            f"Task {task.id[:8]} created on '{self.card.name}' "
            f"(skill={params.skill_id or 'auto'})"
        )

        # Execute the task
        try:
            task = await asyncio.wait_for(
                self.handle_task(task, params),
                timeout=params.timeout_seconds,
            )
        except asyncio.TimeoutError:
            task.state = TaskState.EXPIRED
            task.error = f"Task exceeded {params.timeout_seconds}s timeout"
            logger.warning(f"Task {task.id[:8]} timed out")
        except Exception as e:
            task.fail(str(e))
            logger.error(f"Task {task.id[:8]} failed: {e}")
        finally:
            self._active_count -= 1
            task.updated_at = datetime.now(timezone.utc)
            self._tasks[task.id] = task

        return task.model_dump(mode="json")

    async def _get_task(self, task_id: str) -> dict:
        """Get current state of a task."""
        task = self._tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
        return task.model_dump(mode="json")

    async def _cancel_task(self, task_id: str) -> dict:
        """Cancel a running task."""
        task = self._tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if task.is_terminal():
            raise HTTPException(
                status_code=409,
                detail=f"Task already in terminal state: {task.state.value}",
            )

        task.cancel()
        self._tasks[task.id] = task
        logger.info(f"Task {task.id[:8]} cancelled")
        return task.model_dump(mode="json")

    async def _add_message(self, task_id: str, message: Message) -> dict:
        """Add a follow-up message to an existing task."""
        task = self._tasks.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        if task.is_terminal():
            raise HTTPException(
                status_code=409,
                detail=f"Cannot message a task in state: {task.state.value}",
            )

        task = await self.handle_message(task, message)
        self._tasks[task.id] = task
        return task.model_dump(mode="json")

    async def _list_tasks(
        self,
        state: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """List tasks with optional state filter."""
        tasks = list(self._tasks.values())

        if state:
            try:
                target_state = TaskState(state)
                tasks = [t for t in tasks if t.state == target_state]
            except ValueError:
                pass

        # Most recent first
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return [t.model_dump(mode="json") for t in tasks[:limit]]
