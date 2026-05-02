"""Research briefings — thin orchestration around the research-intel sub-agent.

The sub-agent itself is defined in astra/agents/definitions/research_intel.py.
This module provides:
  * run_research(topic, kind) — invokes the agent via Anthropic's SDK,
    persists the result as a research_briefings row, and files a memory.
  * daily_topic() — rotating topic queue for the 07:00 IST scheduled job.
"""

from astra.research.runner import run_research, run_topic_on_demand
from astra.research.topics import daily_topic

__all__ = ["run_research", "run_topic_on_demand", "daily_topic"]
