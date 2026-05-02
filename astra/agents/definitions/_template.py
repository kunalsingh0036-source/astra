"""
Template for creating new sub-agent definitions.

To create a new agent:
1. Copy this file and rename it (e.g., research_intel.py)
2. Fill in the agent's identity, tools, and system prompt
3. Register it in the agent registry
4. Add it to the core agent's subagent definitions

Each sub-agent runs in its own conversation context with isolated history.
It receives only its own system prompt and the task delegated to it.
"""

# from claude_agent_sdk import AgentDefinition
# from astra.agents.registry import AgentDefinitionRecord, AgentStatus, agent_registry


# --- AGENT METADATA ---
# AGENT_NAME = "your-agent-name"
# AGENT_DESCRIPTION = "One-line description of what this agent does."

# --- SYSTEM PROMPT ---
# AGENT_SYSTEM_PROMPT = """
# You are [Agent Name], a specialized sub-agent in the Astra system.
# Your role is [specific role].
#
# ## Capabilities
# - [capability 1]
# - [capability 2]
#
# ## Rules
# - [rule 1]
# - [rule 2]
# """

# --- AGENT SDK DEFINITION ---
# def get_agent_definition() -> AgentDefinition:
#     """Return the Agent SDK definition for this agent."""
#     return AgentDefinition(
#         description=AGENT_DESCRIPTION,
#         prompt=AGENT_SYSTEM_PROMPT,
#         tools=["Read", "Grep", "Glob"],  # restrict tools to what's needed
#         model="sonnet",  # or "haiku", "opus"
#     )

# --- REGISTRY ENTRY ---
# def register():
#     """Register this agent in Astra's fleet registry."""
#     agent_registry.register(AgentDefinitionRecord(
#         name=AGENT_NAME,
#         description=AGENT_DESCRIPTION,
#         capabilities=["capability 1", "capability 2"],
#         status=AgentStatus.ACTIVE,
#         tools=["Read", "Grep", "Glob"],
#         model_tier="sonnet",
#         build_complexity="medium",
#     ))
