"""Tests for astra/creators/draft_doc.py — long-form documents."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_DOC = json.dumps({
    "title": "TestCo Proposal",
    "subtitle": "for the institutional buyer",
    "doc_type": "proposal",
    "executive_summary": "TestCo proposes shipping infrastructure that doesn't break.",
    "sections": [
        {"heading": "Background", "body_md": "Some background."},
        {"heading": "Approach", "body_md": "The approach."},
    ],
    "appendix": [{"heading": "Specs", "body_md": "Tech specs."}],
    "cta": {"headline": "Sign and return", "detail": "By 2026-06-01."},
    "footer_note": "Confidential — © 2026",
})


@pytest.mark.asyncio
class TestDraftDocHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_doc import draft_doc
        mock_anthropic(VALID_DOC)
        artifact = await draft_doc(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            ask="proposal ask", doc_type="proposal",
        )
        assert artifact["id"] >= 1
        assert artifact["kind"] == "doc"
        assert artifact["content"]["doc_type"] == "proposal"
        assert len(artifact["content"]["sections"]) == 2

    async def test_doc_type_passed_to_prompt(self, test_kits_dir, mock_anthropic, mock_store, monkeypatch):
        from astra.creators.draft_doc import draft_doc
        from unittest.mock import AsyncMock, MagicMock

        captured: dict = {}

        async def capturing_create(**kwargs):
            captured["user"] = kwargs["messages"][0]["content"]
            block = MagicMock(); block.text = VALID_DOC
            r = MagicMock(); r.content = [block]
            return r

        fake_client = MagicMock()
        fake_client.messages = MagicMock()
        fake_client.messages.create = AsyncMock(side_effect=capturing_create)
        monkeypatch.setattr("anthropic.AsyncAnthropic", MagicMock(return_value=fake_client))

        await draft_doc(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            ask="x", doc_type="mou_draft",
        )
        assert "mou_draft" in captured["user"]


@pytest.mark.asyncio
class TestDraftDocErrors:
    async def test_unknown_audience_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_doc import draft_doc
        mock_anthropic(VALID_DOC)
        with pytest.raises(FileNotFoundError):
            await draft_doc(
                business_slug=TEST_KIT_SLUG, audience_slug="not-real",
                ask="x", doc_type="proposal",
            )


@pytest.mark.asyncio
class TestDraftDocVoice:
    async def test_regenerates_on_forbidden(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_doc import draft_doc
        dirty = json.dumps({
            **json.loads(VALID_DOC),
            "executive_summary": "world-class infrastructure offering",  # banned
        })
        clean = VALID_DOC
        responses = iter([dirty, clean])
        mock_anthropic(lambda **kw: next(responses))

        artifact = await draft_doc(
            business_slug=TEST_KIT_SLUG, audience_slug="test-audience",
            ask="x", doc_type="proposal",
        )
        assert "world-class" not in artifact["content"]["executive_summary"]
