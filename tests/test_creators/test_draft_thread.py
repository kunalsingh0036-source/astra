"""Tests for astra/creators/draft_thread.py — long-form social threads."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_THREAD = json.dumps({
    "title": "Thread on X",
    "platform": "twitter",
    "thread_kind": "argument",
    "hook_post": "Here's what I learned shipping infrastructure.",
    "posts": [
        {"position": 2, "body": "First insight.", "purpose": "set the scene"},
        {"position": 3, "body": "Second insight.", "purpose": "land the data"},
    ],
    "closing_post": "What's next: try it yourself.",
    "engagement_prompt": "What would you change?",
    "best_post_time_hint": "Weekday morning",
    "estimated_read_time_seconds": 45,
})


@pytest.mark.asyncio
class TestDraftThreadHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_thread import draft_thread
        mock_anthropic(VALID_THREAD)
        artifact = await draft_thread(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="testing", platform="twitter",
        )
        assert artifact["kind"] == "thread"
        assert artifact["content"]["platform"] == "twitter"
        assert artifact["content"]["hook_post"]

    @pytest.mark.parametrize("platform", ["twitter", "linkedin"])
    async def test_accepts_supported_platforms(
        self, test_kits_dir, mock_anthropic, mock_store, platform,
    ):
        from astra.creators.draft_thread import draft_thread
        mock_anthropic(VALID_THREAD)
        artifact = await draft_thread(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="x", platform=platform,
        )
        assert artifact["kind"] == "thread"


@pytest.mark.asyncio
class TestDraftThreadErrors:
    async def test_unknown_platform_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_thread import draft_thread
        mock_anthropic(VALID_THREAD)
        with pytest.raises(ValueError, match="platform"):
            await draft_thread(
                business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
                topic="x", platform="orkut",
            )
