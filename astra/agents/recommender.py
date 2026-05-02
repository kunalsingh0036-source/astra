"""
Agent recommender — Astra's self-awareness about what agents to build next.

Analyzes current fleet gaps and proposes new agents with:
- Clear scope and capabilities
- Estimated build complexity
- Expected ROI (time saved vs build effort)
- Priority ranking

This is a static recommendation engine for now. As Astra accumulates
usage data and memory, the recommendations become more personalized.
"""

from astra.agents.registry import AgentDefinitionRecord, AgentStatus, agent_registry


# Pre-defined agent proposals with analysis
AGENT_PROPOSALS = [
    AgentDefinitionRecord(
        name="research-intel",
        description=(
            "Monitors tech news, competitor activity, market trends, and AI developments. "
            "Delivers daily intelligence briefings. Tracks specific topics you care about."
        ),
        capabilities=[
            "Web scraping and monitoring",
            "News aggregation and summarization",
            "Competitor tracking",
            "Trend analysis",
            "Daily/weekly briefing generation",
        ],
        status=AgentStatus.PROPOSED,
        tools=["WebSearch", "WebFetch", "Read", "Write"],
        model_tier="sonnet",
        build_complexity="medium",
    ),
    AgentDefinitionRecord(
        name="email-ops",
        description=(
            "Manages email triage, drafts responses, prioritizes inbox, "
            "and handles routine email workflows via Gmail API."
        ),
        capabilities=[
            "Email inbox triage and prioritization",
            "Draft response generation",
            "Follow-up tracking",
            "Meeting scheduling from email",
            "Email template management",
        ],
        status=AgentStatus.PROPOSED,
        tools=["WebFetch", "Read", "Write"],
        model_tier="sonnet",
        build_complexity="high",
    ),
    AgentDefinitionRecord(
        name="code-engineer",
        description=(
            "Writes, reviews, tests, and deploys code across all projects. "
            "Handles PRs, code reviews, and CI/CD management."
        ),
        capabilities=[
            "Code writing and refactoring",
            "Code review and quality analysis",
            "Test generation and execution",
            "Git and GitHub operations",
            "Deployment and CI/CD",
        ],
        status=AgentStatus.PROPOSED,
        tools=["Read", "Edit", "Write", "Bash", "Glob", "Grep"],
        model_tier="opus",
        build_complexity="high",
    ),
    AgentDefinitionRecord(
        name="finance-tracker",
        description=(
            "Tracks revenue, expenses, invoicing, and financial metrics. "
            "Generates reports and alerts on financial health."
        ),
        capabilities=[
            "Revenue and expense tracking",
            "Invoice generation",
            "Financial reporting",
            "Budget monitoring and alerts",
            "Tax preparation data",
        ],
        status=AgentStatus.PROPOSED,
        tools=["Read", "Write", "WebFetch"],
        model_tier="sonnet",
        build_complexity="high",
    ),
    AgentDefinitionRecord(
        name="content-creator",
        description=(
            "Creates and manages content: blog posts, social media, "
            "documentation, presentations, and communications."
        ),
        capabilities=[
            "Blog post and article drafting",
            "Social media content generation",
            "Technical documentation",
            "Presentation creation",
            "Content calendar management",
        ],
        status=AgentStatus.PROPOSED,
        tools=["Read", "Write", "WebSearch"],
        model_tier="sonnet",
        build_complexity="medium",
    ),
    AgentDefinitionRecord(
        name="calendar-scheduler",
        description=(
            "Manages calendar, optimizes scheduling, handles meeting prep, "
            "and ensures time is allocated to priorities."
        ),
        capabilities=[
            "Calendar management",
            "Meeting scheduling and optimization",
            "Meeting prep (briefing docs)",
            "Time block management",
            "Schedule conflict resolution",
        ],
        status=AgentStatus.PROPOSED,
        tools=["WebFetch", "Read", "Write"],
        model_tier="haiku",
        build_complexity="medium",
    ),
]


def get_recommendations(max_results: int = 5) -> list[dict]:
    """Get prioritized agent recommendations.

    Considers:
    1. What's already in the fleet (don't recommend what exists)
    2. Build complexity vs expected impact
    3. Dependencies between agents

    Returns sorted list of recommendations.
    """
    existing = {a["name"] for a in agent_registry.list_all()}

    recommendations = []
    for proposal in AGENT_PROPOSALS:
        if proposal.name in existing:
            continue

        # Priority scoring
        priority_score = _calculate_priority(proposal)

        rec = proposal.to_dict()
        rec["priority_score"] = priority_score
        rec["rationale"] = _get_rationale(proposal)
        recommendations.append(rec)

    # Sort by priority (highest first)
    recommendations.sort(key=lambda x: x["priority_score"], reverse=True)
    return recommendations[:max_results]


def _calculate_priority(agent: AgentDefinitionRecord) -> float:
    """Calculate priority score (0-1) for building this agent.

    Higher = should build sooner.
    """
    complexity_scores = {"low": 0.9, "medium": 0.6, "high": 0.3}
    complexity = complexity_scores.get(agent.build_complexity, 0.5)

    # Research agent is highest priority — provides intelligence for everything else
    name_boosts = {
        "research-intel": 0.3,
        "email-ops": 0.2,
        "calendar-scheduler": 0.15,
        "code-engineer": 0.1,
        "content-creator": 0.1,
        "finance-tracker": 0.05,
    }
    boost = name_boosts.get(agent.name, 0.0)

    return min(1.0, complexity + boost)


def _get_rationale(agent: AgentDefinitionRecord) -> str:
    """Generate a human-readable rationale for why this agent should be built."""
    rationales = {
        "research-intel": (
            "Highest ROI first agent. Provides intelligence that feeds every other "
            "agent and decision. Low-to-medium complexity. You'd use this daily."
        ),
        "email-ops": (
            "Email is a major time sink. Automating triage and drafts saves 30-60 "
            "minutes/day. Requires Gmail API integration (medium-high complexity)."
        ),
        "code-engineer": (
            "You're building multiple projects. A code agent accelerates all of them. "
            "High complexity but compounds across every project."
        ),
        "finance-tracker": (
            "Critical for business growth but can wait until revenue scales. "
            "High complexity due to financial API integrations."
        ),
        "content-creator": (
            "Valuable for building public presence and thought leadership. "
            "Medium complexity, can start with simple text generation."
        ),
        "calendar-scheduler": (
            "Time management multiplier. Relatively simple to build. "
            "Requires Google Calendar API integration."
        ),
    }
    return rationales.get(agent.name, "Recommended based on workflow analysis.")
