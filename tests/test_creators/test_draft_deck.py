"""
Tests for astra/creators/draft.py — draft_deck.

The canonical pattern for draft-tool tests. Other draft tools
(draft_one_pager, draft_doc, draft_brand_kit, draft_carousel, etc.)
follow this same shape. When new draft tools are added, copy this
file as the template.

Pattern:
1. Mock the Anthropic client (returns a fixed JSON response).
2. Mock the store (in-memory artifact persistence).
3. Call the draft function with kit + audience + ask.
4. Assert: artifact saved with correct kind, content, parent links;
   forbidden-phrase regeneration loop fired when expected; error
   paths return clear errors.

Real LLM calls are NOT made by this test. For an integration test
that hits real Sonnet, use `pytest -m integration` (not in this file).
"""

from __future__ import annotations

import json

import pytest


# ── Sample fixtures ─────────────────────────────────────────────────


VALID_DECK_JSON = json.dumps({
    "title": "TestCo — Pre-seed deck",
    "subtitle": "Building things that work",
    "slides": [
        {
            "type": "cover",
            "title": "TestCo",
            "subtitle": "Building things that work",
        },
        {
            "type": "content",
            "title": "What we do",
            "body_md": "We make infrastructure that doesn't break.",
            "bullets": ["fact one", "fact two", "fact three"],
        },
        {
            "type": "data",
            "title": "$2M",
            "heading": "pre-seed open",
            "body_md": "Targeted close: Q3 2026.",
        },
        {
            "type": "close",
            "title": "Let's talk.",
            "body_md": "Schedule a 30-minute first-call.",
        },
    ],
})


# ── Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDraftDeckHappyPath:
    async def test_returns_saved_artifact(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        mock_anthropic(VALID_DECK_JSON)

        artifact = await draft_deck(
            business_slug=TEST_KIT_SLUG,
            audience_slug="test-audience",
            ask="Schedule a 30-minute first-call to scope a pilot.",
            context="",
        )

        # Artifact has an id from the mock store
        assert artifact["id"] >= 1
        assert artifact["kind"] == "deck"
        assert artifact["business_slug"] == TEST_KIT_SLUG
        assert artifact["audience_slug"] == "test-audience"

    async def test_persists_full_deck_content(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        mock_anthropic(VALID_DECK_JSON)
        artifact = await draft_deck(
            business_slug=TEST_KIT_SLUG,
            audience_slug="test-audience",
            ask="Test ask",
        )
        # Content matches what the mocked LLM returned
        content = artifact["content"]
        assert content["title"] == "TestCo — Pre-seed deck"
        assert len(content["slides"]) == 4
        # The final slide is the close with the ask
        assert content["slides"][-1]["type"] == "close"

    async def test_uses_audience_in_prompt(
        self, test_kits_dir, mock_anthropic, mock_store, monkeypatch,
    ):
        """Verify the audience persona's content makes it into the user
        prompt sent to the LLM. This is the main contract — kit + audience
        are rendered into the prompt, not just stuffed into a JSON arg."""
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        captured: dict = {}

        async def capturing_create(**kwargs):
            captured["system"] = kwargs.get("system")
            captured["user"] = kwargs["messages"][0]["content"]
            # Fake response with the same fixed JSON
            from unittest.mock import MagicMock
            block = MagicMock()
            block.text = VALID_DECK_JSON
            resp = MagicMock()
            resp.content = [block]
            return resp

        # Override the mock_anthropic fixture's setup to capture instead
        import anthropic
        from unittest.mock import MagicMock, AsyncMock
        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=capturing_create)
        monkeypatch.setattr("anthropic.AsyncAnthropic", MagicMock(return_value=fake_client))

        await draft_deck(
            business_slug=TEST_KIT_SLUG,
            audience_slug="test-audience",
            ask="Test ask",
            context="extra framing context",
        )

        # The audience markdown is included
        assert "Test audience" in captured["user"]
        # Kit is included
        assert "TestCo" in captured["user"]
        # The ask is in there
        assert "Test ask" in captured["user"]
        # The context is included
        assert "extra framing context" in captured["user"]
        # Forbidden phrases are surfaced via the kit's render_for_prompt
        assert "world-class" in captured["user"]


# ── Error paths ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDraftDeckErrors:
    async def test_unknown_audience_raises_with_available_list(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        mock_anthropic(VALID_DECK_JSON)
        with pytest.raises(FileNotFoundError) as exc:
            await draft_deck(
                business_slug=TEST_KIT_SLUG,
                audience_slug="nonexistent-persona",
                ask="x",
            )
        # Error message lists available audiences (test-audience)
        assert "test-audience" in str(exc.value)

    async def test_unknown_business_raises(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft import draft_deck

        mock_anthropic(VALID_DECK_JSON)
        with pytest.raises(FileNotFoundError):
            await draft_deck(
                business_slug="does-not-exist",
                audience_slug="test-audience",
                ask="x",
            )

    async def test_invalid_json_propagates(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        # Not JSON
        mock_anthropic("This is not JSON. The model went off-script.")
        with pytest.raises(json.JSONDecodeError):
            await draft_deck(
                business_slug=TEST_KIT_SLUG,
                audience_slug="test-audience",
                ask="x",
            )


# ── Voice discipline ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDraftDeckVoiceDiscipline:
    async def test_regenerates_once_when_forbidden_phrase_lands(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """The whole point of voice discipline: a draft with a forbidden
        phrase triggers ONE regeneration with feedback. Verify the
        regenerated draft is what gets saved."""
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        # First response is dirty (contains "world-class" — banned in fixture)
        dirty_deck = json.dumps({
            "title": "TestCo",
            "subtitle": "World-class infrastructure",
            "slides": [
                {"type": "cover", "title": "TestCo"},
                {"type": "close", "title": "Talk?", "body_md": "ask"},
            ],
        })

        # Second response is clean
        clean_deck = json.dumps({
            "title": "TestCo",
            "subtitle": "Reliable infrastructure",
            "slides": [
                {"type": "cover", "title": "TestCo"},
                {"type": "close", "title": "Talk?", "body_md": "ask"},
            ],
        })

        responses = iter([dirty_deck, clean_deck])

        def vary(**kwargs):
            return next(responses)

        mock_anthropic(vary)

        artifact = await draft_deck(
            business_slug=TEST_KIT_SLUG,
            audience_slug="test-audience",
            ask="Test ask",
        )

        # The clean (regenerated) version is what got saved
        assert "Reliable" in artifact["content"]["subtitle"]
        assert "World-class" not in artifact["content"]["subtitle"]

    async def test_strips_code_fences_from_response(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """Some models add ```json fences despite the system prompt
        forbidding them. The strip_code_fences helper must un-wrap
        before parsing."""
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        fenced = "```json\n" + VALID_DECK_JSON + "\n```"
        mock_anthropic(fenced)

        artifact = await draft_deck(
            business_slug=TEST_KIT_SLUG,
            audience_slug="test-audience",
            ask="x",
        )
        assert artifact["content"]["title"] == "TestCo — Pre-seed deck"


# ── Title fallback ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDraftDeckTitleFallback:
    async def test_synthesizes_title_when_missing(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """If the model returns a deck without a title, the artifact's
        title is auto-generated from kit name + ask."""
        from astra.creators.draft import draft_deck
        from tests.test_creators.conftest import TEST_KIT_SLUG

        no_title = json.dumps({
            "subtitle": "subtitle",
            "slides": [
                {"type": "cover", "title": "x"},
                {"type": "close", "title": "x", "body_md": "ask"},
            ],
        })
        mock_anthropic(no_title)

        artifact = await draft_deck(
            business_slug=TEST_KIT_SLUG,
            audience_slug="test-audience",
            ask="The Test Ask",
        )
        # Fallback title contains kit name AND the ask
        assert "TestCo" in artifact["title"]
        assert "The Test Ask" in artifact["title"]
