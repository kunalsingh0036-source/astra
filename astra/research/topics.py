"""
Rotating topic queue for daily research.

Principle: every day advances one compass vector. No two consecutive
days hit the same business, so Kunal doesn't get four HelmTech briefs
in a row. Saturday is deliberately the meta-review day — the moment
we step back and ask what Astra should build / subtract.

Adjusting the queue: edit `WEEKDAY_TOPICS`. Sunday is a free slot for
whatever Kunal explicitly asks that morning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


IST = timezone(timedelta(hours=5, minutes=30))


@dataclass
class Topic:
    slug: str
    title: str
    prompt_focus: str        # what the researcher should pursue today
    business_tags: str       # CSV of business codes (helmtech/bay/apex/topstudios/meta)
    depth: str = "standard"  # "standard" | "deep"


# Indexed by Python weekday() — Mon=0 … Sun=6
WEEKDAY_TOPICS: list[Topic] = [
    # Monday — HelmTech competitive & ecosystem
    Topic(
        slug="helmtech-ecosystem",
        title="HelmTech competitive landscape + India AI policy",
        prompt_focus=(
            "Scan this week's moves by Claude/OpenAI/Perplexity/"
            "India-native AI platforms. Surface anything that affects "
            "Shotgun's positioning as India's execution-first AI platform "
            "or MCP protocol adoption. Flag funding rounds, policy "
            "announcements, enterprise wins. Evaluate HelmTech's $2M "
            "pre-seed positioning against the latest reference prices."
        ),
        business_tags="helmtech",
    ),
    # Tuesday — Squash performance / Nov 2026 champion path
    Topic(
        slug="squash-competitive-intel",
        title="Squash: opponents' form + training science",
        prompt_focus=(
            "What have Abhay Singh, Velavan, Ramit, Veer been doing "
            "recently — tournament results, published training notes, "
            "coaching changes? Any emerging strength/conditioning or "
            "tactical research that Kunal's Nov 2026 Nationals prep "
            "should absorb? Focus on things actionable inside his "
            "current phase (Phase 1-2 Strength Base through Jun 15)."
        ),
        business_tags="bay",
    ),
    # Wednesday — Apex + viral mechanics
    Topic(
        slug="apex-viral-apparel",
        title="Apex Experimental: viral mechanics + apparel trends",
        prompt_focus=(
            "Trend scan: what's winning on Instagram in the "
            "user-generated-fix / tag-a-brand space in apparel? "
            "Pricing signals on 220 GSM combed cotton in Indian B2B? "
            "Any new competitive moves in corporate-merchandise "
            "manufacturing? Any IG algorithmic changes that affect "
            "the tag-then-fulfill playbook?"
        ),
        business_tags="apex",
    ),
    # Thursday — BAY ecosystem + Indian squash infra
    Topic(
        slug="bay-ecosystem",
        title="BAY vertical: Indian squash infrastructure & wellness tourism",
        prompt_focus=(
            "Indian squash ecosystem updates — any new centres, "
            "corporate sponsors moving money, PSA events in India. "
            "Wellness-tourism sector reads relevant to BAY Experience "
            "Centres (Coorg/Mulshi/Alibaug). Any new PPP / CSR grants "
            "for Olympic pathway sports? Outdoor / modular court "
            "technology advancements."
        ),
        business_tags="bay",
    ),
    # Friday — AI agent architecture / MCP protocol / Claude updates
    Topic(
        slug="agent-arch",
        title="Agent architecture + MCP / Claude platform",
        prompt_focus=(
            "What's new in agent frameworks — Agent SDK updates, "
            "new MCP server patterns worth copying, long-horizon "
            "agent research papers (last 2 weeks), orchestration "
            "patterns that handle multi-day workflows. Anything "
            "Astra should absorb or that affects Shotgun's product "
            "direction."
        ),
        business_tags="helmtech,meta",
    ),
    # Saturday — ASTRA META-REVIEW (the self-guidance day)
    Topic(
        slug="astra-meta-review",
        title="Astra meta-review — what to build, what to subtract",
        prompt_focus=(
            "Full self-audit. Reading the compass + Astra's internal "
            "state + this week's briefings + meetings + tasks + "
            "commits. Produce: (1) what advanced the compass this week, "
            "(2) what stalled or got abandoned, (3) what to build "
            "next week with priority + compass-tie, (4) what to "
            "SUBTRACT — dormant code, stale features, half-built "
            "paths. This is the most important briefing of the week."
        ),
        business_tags="meta",
        depth="deep",
    ),
    # Sunday — placeholder; the Sunday job is typically Kunal-driven
    Topic(
        slug="sunday-open",
        title="Sunday open research",
        prompt_focus=(
            "No fixed agenda — scan the compass, notice what's "
            "underserved by this week's briefings, pick one angle "
            "Kunal hasn't been fed intel on recently."
        ),
        business_tags="meta",
    ),
]


def daily_topic(for_date: date | None = None) -> Topic:
    """Return today's topic in IST. Monday → index 0, … Sunday → 6."""
    if for_date is None:
        for_date = datetime.now(IST).date()
    return WEEKDAY_TOPICS[for_date.weekday()]


def topic_by_slug(slug: str) -> Topic | None:
    for t in WEEKDAY_TOPICS:
        if t.slug == slug:
            return t
    return None
