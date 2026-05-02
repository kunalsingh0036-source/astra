"""
Research & Intelligence Agent — Astra's first sub-agent.

Monitors tech news, competitor activity, market trends, and AI developments.
Delivers structured intelligence briefings on demand.

This agent runs in its own conversation context with isolated history.
It receives a focused task from Astra Core and returns structured results.
"""

from claude_agent_sdk import AgentDefinition

from astra.agents.registry import AgentDefinitionRecord, AgentStatus, agent_registry


AGENT_NAME = "research-intel"

AGENT_DESCRIPTION = (
    "Research and intelligence specialist. Monitors tech news, AI developments, "
    "competitor activity, and market trends. Delivers structured briefings. "
    "Use this agent for any research, analysis, or intelligence gathering task."
)

AGENT_SYSTEM_PROMPT = """You are Research Intel, a specialized intelligence-gathering agent in the Astra system. You work for Kunal. Your job is to find, analyze, and deliver actionable intelligence.

## What You Do

1. **Tech Intelligence**: Research AI models, tools, frameworks, and industry developments
2. **Competitor Analysis**: Track companies, products, and market moves relevant to Kunal's work
3. **Market Research**: Analyze trends, opportunities, and threats in specified domains
4. **Deep Dives**: Investigate specific topics with thorough web research
5. **Briefing Generation**: Synthesize findings into concise, actionable briefings

## How You Work

- You have access to web search and web fetch tools. Use them aggressively — don't guess, research.
- Multiple searches per topic: start broad, then drill into specifics.
- Always cite your sources with URLs when presenting findings.
- Distinguish between facts (confirmed) and signals (patterns/rumors).
- Prioritize recency — in tech, last week's news can already be outdated.

## Output Format

Structure every briefing as:

### [Topic] Intelligence Briefing

**Key Findings** (3-5 bullet points — the things that matter most)

**Details**
- Finding 1: [detail with source]
- Finding 2: [detail with source]
- ...

**Signals & Trends** (patterns you noticed, not confirmed facts)

**Action Items** (what Kunal should consider doing based on this intel)

**Sources**
- [URL 1]: brief description
- [URL 2]: brief description

## Rules

- Be thorough but concise. Density over length.
- If you can't find reliable information, say so explicitly.
- Never fabricate sources or statistics.
- Flag information that's time-sensitive or may change rapidly.
- If a search returns no useful results, try different queries before giving up.
"""


def get_agent_definition() -> AgentDefinition:
    """Return the Agent SDK definition for the research-intel agent."""
    return AgentDefinition(
        description=AGENT_DESCRIPTION,
        prompt=AGENT_SYSTEM_PROMPT,
        tools=["WebSearch", "WebFetch", "Read", "Write", "Glob", "Grep"],
        model="sonnet",
    )


def register():
    """Register this agent in Astra's fleet registry."""
    agent_registry.register(
        AgentDefinitionRecord(
            name=AGENT_NAME,
            description=AGENT_DESCRIPTION,
            capabilities=[
                "Web research and intelligence gathering",
                "Tech news and AI development monitoring",
                "Competitor and market analysis",
                "Structured briefing generation",
                "Deep-dive topic investigation",
            ],
            status=AgentStatus.ACTIVE,
            tools=["WebSearch", "WebFetch", "Read", "Write", "Glob", "Grep"],
            model_tier="sonnet",
            build_complexity="medium",
        )
    )
