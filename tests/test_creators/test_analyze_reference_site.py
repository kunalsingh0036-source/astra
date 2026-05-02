"""Tests for astra/creators/analyze_reference_site.py — URL → IA analysis."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


VALID_ANALYSIS = json.dumps({
    "url": "https://example.com",
    "page_intent": "Marketing site for fictional company",
    "page_kind": "marketing_home",
    "ia_summary": "Hero, then features, then footer.",
    "sections": [
        {"position": 1, "type": "hero", "summary": "branded hero",
         "components_observed": ["headline"], "copy_quality": "fine"},
        {"position": 2, "type": "footer", "summary": "minimal footer",
         "components_observed": ["link"], "copy_quality": "fine"},
    ],
    "style_system": {
        "tone": "minimal",
        "color_palette": ["#000000"],
        "fonts": ["Inter"],
        "density": "minimal",
        "motion_cues": "static",
        "imagery_style": "documentary",
    },
    "functionality_observed": [],
    "what_works": ["clean hero"],
    "what_doesnt": ["no clear CTA"],
    "borrowable_patterns": [
        {"pattern": "clean-hero-only-fold", "context_for_use": "for any minimal brand"},
    ],
    "warnings": [],
})


SAMPLE_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Example Site</title>
    <meta name="description" content="A fictional example">
</head>
<body>
    <nav><a href="/">Home</a><a href="/about">About</a></nav>
    <section><h1>Welcome</h1><p>Welcome to the example site.</p></section>
    <section><h2>Features</h2><p>Clean. Minimal.</p></section>
    <footer><p>© 2026</p></footer>
</body>
</html>
"""


@pytest.fixture
def mock_http(monkeypatch):
    """Patch httpx.Client so the analyzer doesn't make real network calls."""
    fake_response = MagicMock()
    fake_response.text = SAMPLE_HTML
    fake_response.content = SAMPLE_HTML.encode()
    fake_response.headers = {"content-type": "text/html; charset=utf-8"}
    fake_response.url = "https://example.com"
    fake_response.status_code = 200

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    fake_client.get = MagicMock(return_value=fake_response)

    monkeypatch.setattr("httpx.Client", MagicMock(return_value=fake_client))
    return fake_response


@pytest.mark.asyncio
class TestAnalyzeReferenceSiteHappyPath:
    async def test_analyzes_fetched_html(
        self, test_kits_dir, mock_anthropic, mock_store, mock_http,
    ):
        from astra.creators.analyze_reference_site import analyze_reference_site
        mock_anthropic(VALID_ANALYSIS)

        artifact = await analyze_reference_site("https://example.com")
        assert artifact["kind"] == "site_analysis"
        # Lives under top-studios (research artifacts)
        assert artifact["business_slug"] == "top-studios"
        assert "sections" in artifact["content"]

    async def test_url_without_scheme_gets_https(
        self, test_kits_dir, mock_anthropic, mock_store, mock_http,
    ):
        """URLs like 'example.com' should be auto-prefixed with https://."""
        from astra.creators.analyze_reference_site import analyze_reference_site
        mock_anthropic(VALID_ANALYSIS)
        artifact = await analyze_reference_site("example.com")
        assert artifact["kind"] == "site_analysis"


@pytest.mark.asyncio
class TestAnalyzeReferenceSiteErrors:
    async def test_non_html_content_type_raises(
        self, test_kits_dir, mock_anthropic, mock_store, monkeypatch,
    ):
        from astra.creators.analyze_reference_site import analyze_reference_site

        # Fake response with JSON content-type
        fake_response = MagicMock()
        fake_response.text = "{}"
        fake_response.content = b"{}"
        fake_response.headers = {"content-type": "application/json"}
        fake_response.url = "https://example.com/api"
        fake_response.status_code = 200

        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)
        fake_client.get = MagicMock(return_value=fake_response)
        monkeypatch.setattr("httpx.Client", MagicMock(return_value=fake_client))

        mock_anthropic(VALID_ANALYSIS)
        with pytest.raises(RuntimeError, match="Failed to fetch"):
            await analyze_reference_site("https://example.com/api")
