"""Tests for astra/creators/critique.py — Haiku-cheap quality review."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_CRITIQUE = json.dumps({
    "overall_score": 78,
    "verdict": "revise",
    "summary": "Solid structural foundation. Some voice slips need fixing.",
    "voice_compliance": {
        "score": 75,
        "notes": "Mostly clean; one hedging phrase to remove.",
        "issues": [
            {"location": "slide 3", "issue": "hedging language",
             "fix": "remove 'we believe'"},
        ],
    },
    "audience_fit": {
        "score": 80, "notes": "Lands the audience.",
        "issues": [],
    },
    "factual_grounding": {
        "score": 78, "notes": "Numbers cited match proof points.",
        "issues": [],
    },
    "structure_and_flow": {
        "score": 80, "notes": "Builds well.",
        "issues": [],
    },
    "top_three_fixes": [
        "Remove 'we believe' on slide 3",
        "Tighten the close",
        "Add one stat to the data slide",
    ],
})


@pytest.mark.asyncio
class TestCritiqueHappyPath:
    async def test_critiques_existing_deck(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.critique import critique_artifact
        from astra.creators.store import create_artifact

        # Seed a deck to critique
        deck = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="deck",
            audience_slug="test-audience",
            title="source", ask="x",
            content={
                "title": "TestCo deck",
                "subtitle": "subtitle",
                "slides": [
                    {"type": "cover", "title": "x"},
                    {"type": "close", "title": "Talk?", "body_md": "we believe"},
                ],
            },
        )

        mock_anthropic(VALID_CRITIQUE)
        review = await critique_artifact(deck["id"])
        assert review["kind"] == "critique"
        assert review["parent_id"] == deck["id"]
        assert review["content"]["overall_score"] == 78
        assert review["content"]["verdict"] == "revise"

    async def test_critiques_one_pager(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.critique import critique_artifact
        from astra.creators.store import create_artifact

        op = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="one_pager",
            audience_slug="test-audience",
            title="op", ask="x",
            content={
                "title": "x", "subtitle": "s",
                "hero_stat": {"value": "v", "label": "l"},
                "lead": "lead",
                "sections": [{"heading": "h", "body_md": "b"}],
                "proof": [],
                "cta": {"headline": "h", "detail": "d"},
            },
        )
        mock_anthropic(VALID_CRITIQUE)
        review = await critique_artifact(op["id"])
        assert review["kind"] == "critique"


@pytest.mark.asyncio
class TestCritiqueErrors:
    async def test_unknown_artifact_id_raises(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.critique import critique_artifact
        with pytest.raises(FileNotFoundError):
            await critique_artifact(99999)
