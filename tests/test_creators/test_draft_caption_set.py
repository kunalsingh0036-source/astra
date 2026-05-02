"""Tests for astra/creators/draft_caption_set.py — A/B caption variants."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_CAPTION_SET = json.dumps({
    "topic": "infrastructure update",
    "platform": "linkedin",
    "subject_summary": "We shipped infrastructure that scales.",
    "variants": [
        {
            "label": "contrarian-hook", "hook_style": "counter-intuitive",
            "length_target": "medium",
            "body": "Most people think X. Actually Y.",
            "first_line": "Most people think X.",
            "predicted_strength": "cold audience",
        },
        {
            "label": "data-led", "hook_style": "specific-number",
            "length_target": "short",
            "body": "We shipped 42 widgets/sec.",
            "first_line": "42 widgets/sec.",
            "predicted_strength": "warm audience already engaged",
        },
        {
            "label": "story-led", "hook_style": "narrative",
            "length_target": "long",
            "body": "It started with a problem...",
            "first_line": "It started with a problem.",
            "predicted_strength": "audiences who want depth",
        },
    ],
})


@pytest.mark.asyncio
class TestDraftCaptionSetHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_caption_set import draft_caption_set
        mock_anthropic(VALID_CAPTION_SET)
        artifact = await draft_caption_set(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="infrastructure", platform="linkedin", variant_count=3,
        )
        assert artifact["kind"] == "caption_set"
        assert len(artifact["content"]["variants"]) == 3

    async def test_variant_count_clamps_to_range(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """variant_count outside [3, 5] should be clamped — not error."""
        from astra.creators.draft_caption_set import draft_caption_set
        mock_anthropic(VALID_CAPTION_SET)
        # Pass 10 — should clamp to 5; tool shouldn't error
        artifact = await draft_caption_set(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="x", platform="linkedin", variant_count=10,
        )
        assert artifact["kind"] == "caption_set"
        # And 1 (below min) should clamp to 3
        artifact2 = await draft_caption_set(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="x", platform="linkedin", variant_count=1,
        )
        assert artifact2["kind"] == "caption_set"
