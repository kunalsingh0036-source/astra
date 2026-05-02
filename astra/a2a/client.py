"""
A2A Client — how Astra sends tasks to other agents.

This is the outbound side of A2A. When Astra decides a task should be
handled by a sub-agent, it uses this client to:

1. Look up the agent via discovery
2. Send the task with parameters
3. Poll or wait for completion
4. Return the result to Astra Core

The client handles retries, timeouts, and error mapping so the rest
of Astra doesn't need to know about HTTP details.

Usage:
    client = A2AClient(discovery)
    task = await client.send_task(
        agent_name="research-intel",
        message="Research latest AI agent developments",
        skill_id="tech-briefing",
    )
    # task.state == TaskState.COMPLETED
    # task.result.content == "Here's what I found..."
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from astra.a2a.discovery import AgentDiscovery, agent_discovery
from astra.a2a.exceptions import (
    AgentNotFoundError,
    SkillNotFoundError,
    TaskFailedError,
    TaskNotFoundError,
    TaskTimeoutError,
)
from astra.a2a.models import (
    Message,
    MessagePart,
    MessageRole,
    Task,
    TaskSendParams,
    TaskState,
)

logger = logging.getLogger(__name__)


class A2AClient:
    """Astra's client for communicating with A2A agents.

    Handles the full task lifecycle:
    - Creating tasks on remote agents
    - Polling for task completion
    - Retrieving results
    - Cancelling tasks
    - Following up with additional messages

    The client is stateless between tasks — all task state lives on the
    agent's server side, and we store completed tasks locally for audit.
    """

    def __init__(
        self,
        discovery: AgentDiscovery | None = None,
        poll_interval: float = 1.0,
        max_poll_interval: float = 10.0,
    ):
        self._discovery = discovery or agent_discovery
        self._poll_interval = poll_interval
        self._max_poll_interval = max_poll_interval
        self._http_client: httpx.AsyncClient | None = None

        # Local cache of tasks we've sent (task_id → Task)
        self._tasks: dict[str, Task] = {}

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-init HTTP client."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self._http_client

    async def close(self) -> None:
        """Clean up."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def send_task(
        self,
        agent_name: str,
        message: str,
        skill_id: str | None = None,
        priority: int = 5,
        timeout_seconds: int = 300,
        wait: bool = True,
        metadata: dict | None = None,
    ) -> Task:
        """Send a task to an agent and optionally wait for completion.

        This is the primary method. Most callers will use this.

        Args:
            agent_name: Name of the target agent (must be discovered first)
            message: The task instruction/prompt as text
            skill_id: Target a specific skill (None = let agent decide)
            priority: 1-10, higher = more urgent
            timeout_seconds: Max time to wait for completion
            wait: If True, poll until complete. If False, return immediately.
            metadata: Optional metadata to attach to the task

        Returns:
            The completed (or submitted, if wait=False) Task

        Raises:
            AgentNotFoundError: Agent not in discovery cache
            SkillNotFoundError: Requested skill doesn't exist on agent
            TaskFailedError: Agent returned a failure
            TaskTimeoutError: Timed out waiting for completion
        """
        # Resolve agent
        agent = self._discovery.get(agent_name)
        if not agent:
            raise AgentNotFoundError(agent_name)

        # Validate skill exists if specified
        if skill_id:
            skill_ids = [s.id for s in agent.card.skills]
            if skill_id not in skill_ids:
                raise SkillNotFoundError(skill_id, agent_name)

        # Build the task params
        params = TaskSendParams(
            skill_id=skill_id,
            message=Message(
                role=MessageRole.CLIENT,
                parts=[MessagePart(type="text", content=message)],
            ),
            priority=priority,
            timeout_seconds=timeout_seconds,
            metadata=metadata or {},
        )

        # Send to agent
        client = await self._get_client()
        base_url = agent.card.url.rstrip("/")

        try:
            response = await client.post(
                f"{base_url}/a2a/tasks",
                json=params.model_dump(mode="json"),
            )
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise AgentNotFoundError(base_url) from e
        except httpx.HTTPStatusError as e:
            raise AgentNotFoundError(base_url) from e

        # Parse the created task
        task = Task(**response.json())
        self._tasks[task.id] = task

        logger.info(
            f"Task {task.id} sent to '{agent_name}' "
            f"(skill={skill_id or 'auto'}, priority={priority})"
        )

        if wait:
            return await self._poll_until_complete(
                agent_name, task.id, timeout_seconds
            )

        return task

    async def _poll_until_complete(
        self,
        agent_name: str,
        task_id: str,
        timeout_seconds: int,
    ) -> Task:
        """Poll a task until it reaches a terminal state.

        Uses exponential backoff: starts at poll_interval, doubles each
        iteration up to max_poll_interval.

        Args:
            agent_name: Agent handling the task
            task_id: Task to poll
            timeout_seconds: Max total wait time

        Returns:
            The completed/failed/cancelled Task

        Raises:
            TaskTimeoutError: If timeout is exceeded
            TaskFailedError: If the task failed
        """
        elapsed = 0.0
        interval = self._poll_interval

        while elapsed < timeout_seconds:
            await asyncio.sleep(interval)
            elapsed += interval

            task = await self.get_task(agent_name, task_id)

            if task.is_terminal():
                if task.state == TaskState.FAILED:
                    raise TaskFailedError(task_id, task.error or "Unknown error")
                return task

            # Exponential backoff, capped
            interval = min(interval * 1.5, self._max_poll_interval)

        raise TaskTimeoutError(task_id, timeout_seconds)

    async def get_task(self, agent_name: str, task_id: str) -> Task:
        """Get the current state of a task.

        Args:
            agent_name: Agent handling the task
            task_id: Task to check

        Returns:
            Current Task state

        Raises:
            AgentNotFoundError: Agent not found
            TaskNotFoundError: Task not found on agent
        """
        agent = self._discovery.get(agent_name)
        if not agent:
            raise AgentNotFoundError(agent_name)

        client = await self._get_client()
        base_url = agent.card.url.rstrip("/")

        try:
            response = await client.get(f"{base_url}/a2a/tasks/{task_id}")
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise TaskNotFoundError(task_id, agent_name)
            raise

        task = Task(**response.json())
        self._tasks[task.id] = task  # Update local cache
        return task

    async def cancel_task(self, agent_name: str, task_id: str) -> Task:
        """Cancel a running task.

        Args:
            agent_name: Agent handling the task
            task_id: Task to cancel

        Returns:
            The cancelled Task
        """
        agent = self._discovery.get(agent_name)
        if not agent:
            raise AgentNotFoundError(agent_name)

        client = await self._get_client()
        base_url = agent.card.url.rstrip("/")

        response = await client.post(
            f"{base_url}/a2a/tasks/{task_id}/cancel"
        )
        response.raise_for_status()

        task = Task(**response.json())
        self._tasks[task.id] = task
        return task

    async def send_message(
        self,
        agent_name: str,
        task_id: str,
        message: str,
    ) -> Task:
        """Send a follow-up message to an in-progress task.

        Use this for multi-turn agent interactions — when the agent
        needs more information or you want to refine the task.

        Args:
            agent_name: Agent handling the task
            task_id: Task to send the message to
            message: The follow-up message text

        Returns:
            Updated Task
        """
        agent = self._discovery.get(agent_name)
        if not agent:
            raise AgentNotFoundError(agent_name)

        msg = Message(
            role=MessageRole.CLIENT,
            parts=[MessagePart(type="text", content=message)],
        )

        client = await self._get_client()
        base_url = agent.card.url.rstrip("/")

        response = await client.post(
            f"{base_url}/a2a/tasks/{task_id}/messages",
            json=msg.model_dump(mode="json"),
        )
        response.raise_for_status()

        task = Task(**response.json())
        self._tasks[task.id] = task
        return task

    def get_local_tasks(
        self,
        agent_name: str | None = None,
        state: TaskState | None = None,
    ) -> list[Task]:
        """Get locally cached tasks with optional filtering.

        Args:
            agent_name: Filter by agent
            state: Filter by state

        Returns:
            List of matching tasks
        """
        tasks = list(self._tasks.values())

        if agent_name:
            tasks = [t for t in tasks if t.agent_name == agent_name]
        if state:
            tasks = [t for t in tasks if t.state == state]

        return sorted(tasks, key=lambda t: t.created_at, reverse=True)

    def get_task_history(self, limit: int = 20) -> list[dict]:
        """Get a summary of recent tasks for audit/display.

        Returns:
            List of task summaries (most recent first)
        """
        tasks = sorted(
            self._tasks.values(),
            key=lambda t: t.created_at,
            reverse=True,
        )[:limit]

        return [
            {
                "id": t.id[:8],  # Short ID for display
                "agent": t.agent_name,
                "state": t.state.value,
                "created": t.created_at.isoformat(),
                "priority": t.priority,
                "messages": len(t.messages),
                "has_result": t.result is not None,
            }
            for t in tasks
        ]
