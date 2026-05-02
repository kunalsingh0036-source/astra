"""Tests for astra/creators/draft_site_brief.py — site IA + style brief."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_SITE_BRIEF = json.dumps({
    "title": "TestCo site",
    "subtitle": "infrastructure that works",
    "site_kind": "marketing_site",
    "primary_goal": "Convert visitors into pilot conversations.",
    "secondary_goals": ["Brand recognition"],
    "sitemap": [
        {"slug": "home", "title": "Home", "intent": "convert", "kind": "home",
         "sections": [
             {"type": "hero", "intent": "open", "components": ["headline"],
              "content_brief": "Strong headline."},
             {"type": "cta_block", "intent": "convert", "components": ["cta"],
              "content_brief": "CTA at the end."},
         ]},
        {"slug": "platform", "title": "Platform", "intent": "explain", "kind": "product",
         "sections": [
             {"type": "features", "intent": "explain", "components": ["card"],
              "content_brief": "Three features."},
         ]},
    ],
    "style_direction": {
        "tone": "institutional",
        "density": "minimal",
        "motion": "Static; subtle hover only.",
        "navigation_grammar": "Sticky top nav with logo + 4 links.",
        "imagery_direction": "Documentary photography.",
        "extra_palette_notes": "Add semantic-success #10B981.",
    },
    "functionality": [
        {"name": "contact form", "scope": "lead capture", "complexity": "low",
         "third_party_recommendation": "custom"},
    ],
    "reference_notes": [],
    "performance_budget": {
        "lcp_target_seconds": 2.0,
        "image_optimization": "WebP + lazy load",
        "third_party_budget": "max 3",
    },
    "accessibility_baseline": "WCAG 2.1 AA, keyboard navigable, semantic HTML.",
})


@pytest.mark.asyncio
class TestDraftSiteBriefHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_site_brief import draft_site_brief
        mock_anthropic(VALID_SITE_BRIEF)
        artifact = await draft_site_brief(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            primary_goal="Convert visitors", site_kind="marketing_site",
        )
        assert artifact["kind"] == "site_brief"
        assert len(artifact["content"]["sitemap"]) == 2

    async def test_resolves_reference_analyses(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """When reference_analysis_ids are passed, those analysis
        artifacts must exist and get included in the prompt context."""
        from astra.creators.draft_site_brief import draft_site_brief
        from astra.creators.store import create_artifact

        # Seed a reference analysis
        ref = await create_artifact(
            business_slug="top-studios", kind="site_analysis",
            title="ref", ask="x",
            content={"url": "example.com", "page_intent": "test"},
        )

        mock_anthropic(VALID_SITE_BRIEF)
        artifact = await draft_site_brief(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            primary_goal="x", reference_analysis_ids=[ref["id"]],
        )
        assert artifact["kind"] == "site_brief"


@pytest.mark.asyncio
class TestDraftSiteBriefErrors:
    async def test_invalid_reference_id_raises(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """Reference id that doesn't point to a site_analysis artifact
        should raise."""
        from astra.creators.draft_site_brief import draft_site_brief
        from astra.creators.store import create_artifact

        # Wrong kind — pretend it's a deck
        not_a_ref = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="deck",
            title="x", ask="x", content={},
        )
        mock_anthropic(VALID_SITE_BRIEF)
        with pytest.raises(FileNotFoundError):
            await draft_site_brief(
                business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
                primary_goal="x", reference_analysis_ids=[not_a_ref["id"]],
            )
