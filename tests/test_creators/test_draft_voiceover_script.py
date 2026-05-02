"""Tests for astra/creators/draft_voiceover_script.py — TTS-ready scripts."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_VOICEOVER = json.dumps({
    "title": "TestCo VO",
    "duration_seconds": 30,
    "voice_persona": "Founder voice — male, mid-register",
    "delivery_notes": "Conversational, unhurried.",
    "segments": [
        {"position": 1, "duration_seconds": 10,
         "spoken_text": "We ship infrastructure that doesn't break.",
         "delivery_cue": "land flat", "emphasis_words": ["doesn't"],
         "pronunciation_notes": []},
        {"position": 2, "duration_seconds": 20,
         "spoken_text": "Talk to us if you want production-grade systems.",
         "delivery_cue": "warmer", "emphasis_words": [],
         "pronunciation_notes": [{"word": "TestCo", "ipa_or_hint": "TEST-co"}]},
    ],
    "estimated_total_words": 20,
    "estimated_speaking_seconds": 30,
    "tts_recommendations": {
        "best_voice_style": "warm-male-baritone",
        "speaking_rate": "medium",
        "ssml_hints": "",
    },
})


@pytest.mark.asyncio
class TestDraftVoiceoverScriptHappyPath:
    async def test_standalone_mode(self, test_kits_dir, mock_anthropic, mock_store):
        """Mode 1: topic + business + audience (no source artifact)."""
        from astra.creators.draft_voiceover_script import draft_voiceover_script
        mock_anthropic(VALID_VOICEOVER)
        artifact = await draft_voiceover_script(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            duration_seconds=30, topic="infrastructure update",
        )
        assert artifact["kind"] == "voiceover_script"
        assert artifact["content"]["duration_seconds"] == 30

    async def test_from_source_artifact(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """Mode 2: convert an existing artifact to spoken form."""
        from astra.creators.draft_voiceover_script import draft_voiceover_script
        from astra.creators.store import create_artifact

        # Seed a deck artifact for the voiceover to convert
        deck = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="deck",
            audience_slug="test-audience",
            title="source deck", ask="x",
            content={
                "title": "Source Deck",
                "subtitle": "for VO",
                "slides": [
                    {"type": "cover", "title": "X"},
                    {"type": "content", "title": "What",
                     "body_md": "We ship infrastructure."},
                ],
            },
        )

        mock_anthropic(VALID_VOICEOVER)
        artifact = await draft_voiceover_script(
            source_artifact_id=deck["id"], duration_seconds=30,
        )
        assert artifact["kind"] == "voiceover_script"
        # parent_id wires it to the source deck
        assert artifact["parent_id"] == deck["id"]


@pytest.mark.asyncio
class TestDraftVoiceoverScriptErrors:
    async def test_no_business_no_source_raises(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft_voiceover_script import draft_voiceover_script
        mock_anthropic(VALID_VOICEOVER)
        with pytest.raises(ValueError, match="business_slug or source_artifact_id"):
            await draft_voiceover_script(duration_seconds=30, topic="x")

    async def test_business_without_topic_raises(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft_voiceover_script import draft_voiceover_script
        mock_anthropic(VALID_VOICEOVER)
        with pytest.raises(ValueError, match="topic required"):
            await draft_voiceover_script(
                business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
                duration_seconds=30,
            )
