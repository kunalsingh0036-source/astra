"""Tests for astra/creators/draft_one_pager.py — single-page sales sheet."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_ONE_PAGER = json.dumps({
    "title": "TestCo One-Pager",
    "subtitle": "production-grade infrastructure",
    "hero_stat": {"value": "42", "label": "widgets/sec"},
    "lead": "TestCo ships infrastructure that doesn't break.",
    "sections": [
        {"heading": "What we do", "body_md": "Build infra."},
        {"heading": "Who for", "body_md": "Teams running tests."},
    ],
    "proof": ["TBD — fixture data"],
    "cta": {"headline": "Schedule a call", "detail": "Email us today."},
})


@pytest.mark.asyncio
class TestDraftOnePagerHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_one_pager import draft_one_pager
        mock_anthropic(VALID_ONE_PAGER)
        artifact = await draft_one_pager(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience", ask="test ask",
        )
        assert artifact["id"] >= 1
        assert artifact["kind"] == "one_pager"
        assert artifact["business_slug"] == TEST_KIT_SLUG
        # Hero stat survived
        assert artifact["content"]["hero_stat"]["value"] == "42"
        assert len(artifact["content"]["sections"]) == 2

    async def test_includes_kit_in_prompt(self, test_kits_dir, mock_anthropic, mock_store, monkeypatch):
        """Audience markdown + kit content must reach the LLM prompt."""
        from astra.creators.draft_one_pager import draft_one_pager
        from unittest.mock import AsyncMock, MagicMock

        captured: dict = {}

        async def capturing_create(**kwargs):
            captured["user"] = kwargs["messages"][0]["content"]
            block = MagicMock()
            block.text = VALID_ONE_PAGER
            r = MagicMock()
            r.content = [block]
            return r

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=capturing_create)
        monkeypatch.setattr("anthropic.AsyncAnthropic", MagicMock(return_value=fake_client))

        await draft_one_pager(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            ask="The Specific Ask",
        )
        assert "Test audience" in captured["user"]
        assert "TestCo" in captured["user"]
        assert "The Specific Ask" in captured["user"]


@pytest.mark.asyncio
class TestDraftOnePagerErrors:
    async def test_unknown_audience_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_one_pager import draft_one_pager
        mock_anthropic(VALID_ONE_PAGER)
        with pytest.raises(FileNotFoundError):
            await draft_one_pager(
                business_slug=TEST_KIT_SLUG, audience_slug="not-a-real-persona", ask="x",
            )

    async def test_unknown_business_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_one_pager import draft_one_pager
        mock_anthropic(VALID_ONE_PAGER)
        with pytest.raises(FileNotFoundError):
            await draft_one_pager(
                business_slug="does-not-exist", audience_slug="test-audience", ask="x",
            )


@pytest.mark.asyncio
class TestDraftOnePagerVoice:
    async def test_regenerates_on_forbidden(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_one_pager import draft_one_pager

        dirty = json.dumps({
            "title": "TestCo",
            "subtitle": "world-class!",  # banned
            "hero_stat": {"value": "x", "label": "y"},
            "lead": "x", "sections": [], "proof": [],
            "cta": {"headline": "x", "detail": "y"},
        })
        clean = json.dumps({
            "title": "TestCo",
            "subtitle": "production-grade",
            "hero_stat": {"value": "x", "label": "y"},
            "lead": "x", "sections": [], "proof": [],
            "cta": {"headline": "x", "detail": "y"},
        })
        responses = iter([dirty, clean])
        mock_anthropic(lambda **kw: next(responses))

        artifact = await draft_one_pager(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience", ask="x",
        )
        assert "world-class" not in artifact["content"]["subtitle"]
