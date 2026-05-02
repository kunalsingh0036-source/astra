"""
A2A-specific exceptions.

Separate from generic Python exceptions so callers can catch
A2A protocol errors distinctly from infrastructure errors.
"""


class A2AError(Exception):
    """Base exception for all A2A protocol errors."""

    pass


class AgentNotFoundError(A2AError):
    """Agent could not be discovered at the given URL."""

    def __init__(self, url: str):
        self.url = url
        super().__init__(f"No A2A agent found at {url}")


class AgentCardInvalidError(A2AError):
    """Agent Card failed validation."""

    def __init__(self, url: str, reason: str):
        self.url = url
        self.reason = reason
        super().__init__(f"Invalid Agent Card at {url}: {reason}")


class TaskNotFoundError(A2AError):
    """Task ID not found on the target agent."""

    def __init__(self, task_id: str, agent_name: str):
        self.task_id = task_id
        self.agent_name = agent_name
        super().__init__(f"Task {task_id} not found on agent '{agent_name}'")


class TaskFailedError(A2AError):
    """Task completed with a failure state."""

    def __init__(self, task_id: str, error: str):
        self.task_id = task_id
        self.error = error
        super().__init__(f"Task {task_id} failed: {error}")


class TaskTimeoutError(A2AError):
    """Task did not complete within the allowed time."""

    def __init__(self, task_id: str, timeout_seconds: int):
        self.task_id = task_id
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Task {task_id} timed out after {timeout_seconds}s"
        )


class SkillNotFoundError(A2AError):
    """Requested skill not found on the target agent."""

    def __init__(self, skill_id: str, agent_name: str):
        self.skill_id = skill_id
        self.agent_name = agent_name
        super().__init__(
            f"Skill '{skill_id}' not found on agent '{agent_name}'"
        )


class AuthenticationError(A2AError):
    """Authentication failed when connecting to an agent."""

    def __init__(self, agent_name: str):
        self.agent_name = agent_name
        super().__init__(f"Authentication failed for agent '{agent_name}'")
