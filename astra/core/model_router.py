"""
Model router — decides which Claude model handles each task.

Routes tasks to the cheapest model that can handle them:
- Haiku: Simple classification, yes/no, formatting, routing
- Sonnet: Standard agent work, code, summarization, medium reasoning
- Opus: Complex planning, multi-step reasoning, critical decisions

The router uses heuristics based on task characteristics.
Can be overridden per-task when needed.
"""

from astra.config import settings


class ModelTier:
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


# Keywords that suggest different complexity levels
OPUS_SIGNALS = [
    "plan", "strategy", "analyze", "architect", "design",
    "complex", "multi-step", "trade-off", "compare",
    "financial", "legal", "critical", "important decision",
]

HAIKU_SIGNALS = [
    "classify", "categorize", "yes or no", "true or false",
    "format", "convert", "simple", "quick", "list",
    "extract", "parse", "summarize briefly",
]


def get_model_for_task(task_description: str, force_tier: str | None = None) -> str:
    """Select the best model for a given task.

    Args:
        task_description: Natural language description of what needs to be done.
        force_tier: Override to force a specific tier ("haiku", "sonnet", "opus").

    Returns:
        Model ID string (e.g., "claude-sonnet-4-6").
    """
    if force_tier:
        return _tier_to_model(force_tier)

    description_lower = task_description.lower()

    # Check for Opus signals (complex tasks)
    opus_score = sum(1 for s in OPUS_SIGNALS if s in description_lower)
    if opus_score >= 2:
        return settings.model_opus

    # Check for Haiku signals (simple tasks)
    haiku_score = sum(1 for s in HAIKU_SIGNALS if s in description_lower)
    if haiku_score >= 2:
        return settings.model_haiku

    # Default to Sonnet for everything else
    return settings.model_sonnet


def get_effort_for_task(task_description: str) -> str:
    """Determine the effort level for adaptive thinking.

    Returns: "low", "medium", "high", or "max".
    """
    description_lower = task_description.lower()

    opus_score = sum(1 for s in OPUS_SIGNALS if s in description_lower)
    if opus_score >= 3:
        return "max"
    if opus_score >= 2:
        return "high"

    haiku_score = sum(1 for s in HAIKU_SIGNALS if s in description_lower)
    if haiku_score >= 2:
        return "low"

    return "medium"


def _tier_to_model(tier: str) -> str:
    """Convert a tier name to a model ID."""
    mapping = {
        ModelTier.HAIKU: settings.model_haiku,
        ModelTier.SONNET: settings.model_sonnet,
        ModelTier.OPUS: settings.model_opus,
    }
    return mapping.get(tier, settings.model_sonnet)
