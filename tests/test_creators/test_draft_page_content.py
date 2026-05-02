"""Tests for astra/creators/draft_page_content.py — per-page copy."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_PAGE = json.dumps({
    "page_slug": "home",
    "title": "TestCo Home",
    "meta": {
        "title": "TestCo — infrastructure",
        "description": "production-grade infrastructure",
        "og_title": "TestCo",
        "og_description": "infrastructure",
        "og_image_hint": "logo on dark",
    },
    "sections": [
        {"type": "hero", "id": "hero",
         "heading": "Infrastructure that works",
         "subheading": "production-grade",
         "body_md": "We ship.",
         "cta_primary": {"label": "Talk", "intent": "open form",
                          "destination": "/contact"},
         "image_hint": "minimal abstract", "image_aspect": "16:9"},
    ],
    "footer": {
        "tagline": "TestCo — production-grade",
        "columns": [],
        "bottom_line": "© 2026",
    },
    "global_ctas": {"primary": {"label": "Talk", "destination": "/contact"}},
})


def make_brief_artifact():
    """Helper: return a site_brief content dict with a 'home' page."""
    return {
        "title": "TestCo site",
        "site_kind": "marketing_site",
        "primary_goal": "convert",
        "sitemap": [
            {"slug": "home", "title": "Home", "kind": "home",
             "sections": [
                 {"type": "hero", "content_brief": "open with the headline"},
             ]},
            {"slug": "platform", "title": "Platform", "kind": "product",
             "sections": [
                 {"type": "features", "content_brief": "list features"},
             ]},
        ],
    }


@pytest.mark.asyncio
class TestDraftPageContentHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_page_content import draft_page_content
        from astra.creators.store import create_artifact

        brief = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="site_brief",
            audience_slug="test-audience",
            title="site brief", ask="convert",
            content=make_brief_artifact(),
        )

        mock_anthropic(VALID_PAGE)
        artifact = await draft_page_content(
            site_brief_id=brief["id"], page_slug="home",
        )
        assert artifact["kind"] == "page_content"
        assert artifact["parent_id"] == brief["id"]
        assert len(artifact["content"]["sections"]) >= 1

    async def test_inherits_business_and_audience(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft_page_content import draft_page_content
        from astra.creators.store import create_artifact

        brief = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="site_brief",
            audience_slug="test-audience",
            title="b", ask="x", content=make_brief_artifact(),
        )
        mock_anthropic(VALID_PAGE)
        artifact = await draft_page_content(
            site_brief_id=brief["id"], page_slug="home",
        )
        # Inherits from the brief
        assert artifact["business_slug"] == TEST_KIT_SLUG
        assert artifact["audience_slug"] == "test-audience"


@pytest.mark.asyncio
class TestDraftPageContentErrors:
    async def test_unknown_brief_id_raises(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_page_content import draft_page_content
        mock_anthropic(VALID_PAGE)
        with pytest.raises(FileNotFoundError):
            await draft_page_content(site_brief_id=99999, page_slug="home")

    async def test_wrong_artifact_kind_raises(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """Passing a non-site_brief artifact id must raise with a clear error."""
        from astra.creators.draft_page_content import draft_page_content
        from astra.creators.store import create_artifact

        wrong = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="deck",
            title="x", ask="x", content={},
        )
        mock_anthropic(VALID_PAGE)
        with pytest.raises(ValueError, match="not 'site_brief'"):
            await draft_page_content(site_brief_id=wrong["id"], page_slug="home")

    async def test_unknown_page_slug_raises_with_available(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        from astra.creators.draft_page_content import draft_page_content
        from astra.creators.store import create_artifact

        brief = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="site_brief",
            audience_slug="test-audience",
            title="b", ask="x", content=make_brief_artifact(),
        )
        mock_anthropic(VALID_PAGE)
        with pytest.raises(FileNotFoundError) as exc:
            await draft_page_content(
                site_brief_id=brief["id"], page_slug="missing-page",
            )
        # Error lists available slugs
        assert "home" in str(exc.value)
        assert "platform" in str(exc.value)
