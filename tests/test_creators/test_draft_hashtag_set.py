"""Tests for astra/creators/draft_hashtag_set.py — three-layer hashtag system."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_HASHTAGS = json.dumps({
    "topic": "infrastructure",
    "primary_platform": "linkedin",
    "brand_tags": ["TestCo", "BuildingThings"],
    "topical_tags": ["infrastructure", "production"],
    "reach_tags": ["B2BSaaS", "DeepTech"],
    "platform_recommendations": {
        "linkedin": {"use": ["TestCo", "infrastructure"], "rationale": "tight focus"},
        "instagram": {"use": ["TestCo", "infrastructure", "production", "B2BSaaS"],
                       "rationale": "broader reach allowed"},
        "twitter": {"use": [], "rationale": "X doesn't reward hashtags"},
    },
    "avoid": [],
    "notes": "Brand tags worth establishing.",
})


@pytest.mark.asyncio
class TestDraftHashtagSetHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_hashtag_set import draft_hashtag_set
        mock_anthropic(VALID_HASHTAGS)
        artifact = await draft_hashtag_set(
            business_slug=TEST_KIT_SLUG, topic="infrastructure",
            primary_platform="linkedin",
        )
        assert artifact["kind"] == "hashtag_set"
        assert len(artifact["content"]["brand_tags"]) >= 1
        assert "linkedin" in artifact["content"]["platform_recommendations"]

    async def test_audience_optional(self, test_kits_dir, mock_anthropic, mock_store):
        """Unlike most draft tools, hashtag_set accepts no audience."""
        from astra.creators.draft_hashtag_set import draft_hashtag_set
        mock_anthropic(VALID_HASHTAGS)
        # Without audience
        artifact = await draft_hashtag_set(
            business_slug=TEST_KIT_SLUG, topic="x",
        )
        assert artifact["kind"] == "hashtag_set"

    async def test_with_audience_loads_persona(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft_hashtag_set import draft_hashtag_set
        mock_anthropic(VALID_HASHTAGS)
        artifact = await draft_hashtag_set(
            business_slug=TEST_KIT_SLUG, topic="x", audience_slug="test-audience",
        )
        assert artifact["audience_slug"] == "test-audience"
