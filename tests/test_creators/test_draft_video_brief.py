"""Tests for astra/creators/draft_video_brief.py — AI-video shot list."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_VIDEO_BRIEF = json.dumps({
    "title": "TestCo 30s Reel",
    "format": "vertical_short",
    "platform": "instagram_reels",
    "runtime_seconds": 30,
    "logline": "Infrastructure that works.",
    "narrative_arc": "Hook → context → demo → cta",
    "music_vibe": "Sparse synth, 70 BPM, no drops",
    "music_reference_artists_or_genres": ["Bonobo electronic minimalism"],
    "shots": [
        {"position": 1, "duration_seconds": 3, "shot_type": "establishing",
         "voiceover_text": "", "on_screen_text": "TestCo",
         "visual_description": "Wide shot of a clean lab",
         "image_prompt": "Architectural minimal lab, deep navy",
         "negative_prompt": "stock photos, cliche AI imagery",
         "transition_in": "cut", "transition_out": "fade"},
        {"position": 2, "duration_seconds": 12, "shot_type": "talking_head",
         "voiceover_text": "We ship infrastructure that doesn't break.",
         "on_screen_text": "",
         "visual_description": "Founder talking, mid-frame",
         "image_prompt": "Portrait, soft daylight",
         "negative_prompt": "stock photos",
         "transition_in": "fade", "transition_out": "cut"},
        {"position": 3, "duration_seconds": 15, "shot_type": "title_card",
         "voiceover_text": "Talk to us.",
         "on_screen_text": "TestCo.com",
         "visual_description": "Brand logo center",
         "image_prompt": "Logo on deep navy",
         "negative_prompt": "",
         "transition_in": "cut", "transition_out": "cut"},
    ],
    "b_roll_list": [],
    "captions_burnt_in": True,
    "thumbnail_prompt": "Striking wide shot",
    "platform_specific_notes": {
        "primary": "Reels-first, vertical, hook in first 3s.",
        "repurpose": "Crop to 1:1 for feed; trim to 15s for X.",
    },
    "post_production_notes": ["Add brand color overlay on shot 3."],
})


@pytest.mark.asyncio
class TestDraftVideoBriefHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_video_brief import draft_video_brief
        mock_anthropic(VALID_VIDEO_BRIEF)
        artifact = await draft_video_brief(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="infrastructure update", runtime_seconds=30,
        )
        assert artifact["kind"] == "video_brief"
        assert artifact["content"]["runtime_seconds"] == 30
        assert len(artifact["content"]["shots"]) == 3

    async def test_runtime_clamped_to_range(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """Runtime outside [8, 180] should clamp."""
        from astra.creators.draft_video_brief import draft_video_brief
        mock_anthropic(VALID_VIDEO_BRIEF)
        # Way too long
        artifact = await draft_video_brief(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            topic="x", runtime_seconds=600,
        )
        assert artifact["kind"] == "video_brief"  # didn't error


@pytest.mark.asyncio
class TestDraftVideoBriefErrors:
    async def test_unknown_audience_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_video_brief import draft_video_brief
        mock_anthropic(VALID_VIDEO_BRIEF)
        with pytest.raises(FileNotFoundError):
            await draft_video_brief(
                business_slug=TEST_KIT_SLUG, audience_slug="missing",
                topic="x", runtime_seconds=30,
            )
