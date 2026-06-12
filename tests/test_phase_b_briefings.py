"""Phase B locks — briefing v2 assembly/degradation + triage imports."""

from __future__ import annotations

import pytest


def test_compass_file_exists_and_loads():
    """briefing_v2 reads the repo compass file — the laptop-path read
    it replaced returned nothing forever in the cloud."""
    from astra.scheduler.briefing_v2 import _compass_text

    text = _compass_text()
    assert "HelmTech" in text
    assert "National Champ" in text or "Olympic" in text


@pytest.mark.asyncio
async def test_synthesize_falls_back_without_llm(monkeypatch):
    """Claude down ≠ no briefing. The raw assembled digest ships."""
    import astra.scheduler.briefing_v2 as bv

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no api key")

    import anthropic

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Boom)
    out = await bv._synthesize(
        "morning", {"compass": "c", "inbox": "5 unread", "calendar": "free"}
    )
    assert "5 unread" in out
    assert "morning" in out.lower()


@pytest.mark.asyncio
async def test_sections_degrade_to_strings_never_raise(monkeypatch):
    """Every gather helper returns a string even with the DB/agents
    unreachable — a dead source is one honest clause, not a crash."""
    import astra.scheduler.briefing_v2 as bv

    monkeypatch.setenv("EMAIL_AGENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FLEET_HEALTH_URLS", "stream=http://127.0.0.1:1/health")
    for fn in (
        bv._calendar_today,
        bv._inbox_state,
        bv._fleet_line,
        bv._training_state,
        bv._research_line,
        bv._recent_turn_topics,
        bv._calendar_tomorrow,
    ):
        out = await fn()
        assert isinstance(out, str)


def test_phase_b_imports():
    import email_agent.services.triage  # noqa: F401
    import gateway.api.notify  # noqa: F401
    from astra.scheduler.jobs import inbox_triage, run_inbox_triage  # noqa: F401
