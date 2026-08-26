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
    """Every gather helper degrades to an honest clause with the DB and
    agents unreachable — never a crash, never a fabricated number.

    Three of these return (text, facts) rather than a bare string: the
    facts dict carries the exact counts so a synthesised brief can be
    reconciled against them (added after the 2026-06-13 audit found a
    brief hallucinating "41 action-needed / 10 drafts" from a correct
    "9 / 0"). The test asserts the real contract per helper."""
    import astra.scheduler.briefing_v2 as bv

    monkeypatch.setenv("EMAIL_AGENT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("FLEET_HEALTH_URLS", "stream=http://127.0.0.1:1/health")

    for fn in (
        bv._calendar_today,
        bv._research_line,
        bv._recent_turn_topics,
        bv._calendar_tomorrow,
    ):
        out = await fn()
        # A string, always. An EMPTY string is legitimate here: an
        # optional section with nothing to report is omitted rather than
        # padded. Only a genuinely dead source must speak up, and those
        # are the (text, facts) helpers below.
        assert isinstance(out, str), f"{fn.__name__} must degrade to a string"

    for fn in (bv._inbox_state, bv._fleet_line, bv._training_state):
        out = await fn()
        assert isinstance(out, tuple) and len(out) == 2, (
            f"{fn.__name__} returns (text, facts)")
        text, facts = out
        assert isinstance(text, str) and text.strip()
        assert isinstance(facts, dict)


def test_phase_b_imports():
    import email_agent.services.triage  # noqa: F401
    import gateway.api.notify  # noqa: F401
    from astra.scheduler.jobs import inbox_triage, run_inbox_triage  # noqa: F401
