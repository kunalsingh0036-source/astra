"""
Tests for astra/creators/render.py and templates/.

These tests exercise the Jinja2 templates with representative content
and assert: no template errors, brand colors land in output, all
section types render, image placeholders appear, etc.

Tests stop AT the WeasyPrint binary boundary. WeasyPrint requires
system libraries (pango, gobject) that aren't always available locally
— Railway's image has them. The tests verify the HTML+CSS up to that
point; the binary PDF generation is integration-tested on Railway.

R2 upload is similarly out of scope here — the upload path is patched
out so tests don't need real cloud credentials.
"""

from __future__ import annotations

import pytest


# ── Sample content fixtures ─────────────────────────────────────────


_SAMPLE_DECK = {
    "title": "TestCo Deck",
    "subtitle": "Building things that work",
    "slides": [
        {"type": "cover", "title": "TestCo", "subtitle": "Building things"},
        {"type": "section", "title": "The thesis"},
        {
            "type": "content",
            "title": "What we do",
            "body_md": "Infrastructure that doesn't break.",
            "bullets": ["one", "two", "three"],
            "image_hint": "abstract architectural illustration",
        },
        {
            "type": "data",
            "title": "$2M",
            "heading": "pre-seed open",
            "body_md": "Targeted close: Q3 2026.",
        },
        {
            "type": "quote",
            "body_md": "Test quote",
            "subtitle": "Test attribution",
        },
        {
            "type": "close",
            "title": "Let's talk",
            "body_md": "Schedule a 30-minute first-call.",
            "bullets": ["bullet a", "bullet b"],
        },
    ],
}


_SAMPLE_ONE_PAGER = {
    "title": "TestCo One-Pager",
    "subtitle": "the canonical sales sheet",
    "hero_stat": {"value": "42", "label": "widgets/sec"},
    "lead": "TestCo builds infrastructure that doesn't break.",
    "sections": [
        {"heading": "What we do", "body_md": "Build infra."},
        {"heading": "Who for", "body_md": "Anyone running tests."},
        {"heading": "How", "body_md": "Carefully."},
    ],
    "proof": ["Naval Forces approved", "AQL 2.5 standard"],
    "cta": {
        "headline": "Schedule a call",
        "detail": "Send your spec; we'll respond in 24h.",
    },
}


_SAMPLE_DOC = {
    "title": "TestCo Proposal",
    "subtitle": "for the institutional buyer",
    "doc_type": "proposal",
    "executive_summary": "TestCo proposes to ship infrastructure that doesn't break.",
    "sections": [
        {"heading": "Background", "body_md": "Some background prose here."},
        {"heading": "Approach", "body_md": "The approach is straightforward."},
        {"heading": "Pricing", "body_md": "See appendix."},
    ],
    "appendix": [
        {"heading": "Specs", "body_md": "Technical specifications."},
    ],
    "cta": {
        "headline": "Sign and return",
        "detail": "Sign by 2026-06-01 for Q3 production slot.",
    },
    "footer_note": "Confidential — © 2026 TestCo",
}


_SAMPLE_PAGE = {
    "page_slug": "home",
    "title": "TestCo — Home",
    "meta": {
        "title": "TestCo — Infrastructure that works",
        "description": "TestCo ships production-grade infrastructure",
        "og_title": "TestCo",
        "og_description": "infrastructure",
    },
    "sections": [
        {
            "type": "hero",
            "id": "hero",
            "heading": "Infrastructure that works",
            "subheading": "TestCo ships production-grade infrastructure",
            "cta_primary": {
                "label": "Schedule a call",
                "destination": "/contact",
            },
            "image_hint": "abstract architectural illustration",
            "image_aspect": "16:9",
        },
        {
            "type": "features",
            "id": "features",
            "heading": "What we ship",
            "items": [
                {"title": "Item A", "body_md": "Description A"},
                {"title": "Item B", "body_md": "Description B"},
            ],
        },
        {
            "type": "cta_block",
            "id": "cta",
            "heading": "Ready?",
            "cta_primary": {"label": "Talk to us", "destination": "/contact"},
        },
    ],
    "footer": {
        "tagline": "TestCo — building things that work",
        "columns": [
            {
                "heading": "Product",
                "links": [{"label": "Features", "destination": "#features"}],
            },
        ],
        "bottom_line": "© 2026 TestCo",
    },
    "global_ctas": {
        "primary": {"label": "Get a demo", "destination": "/demo"},
    },
}


# ── Deck template ───────────────────────────────────────────────────


class TestDeckTemplate:
    def test_renders_without_error(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("deck.html.j2")
        html = template.render(
            title=_SAMPLE_DECK["title"],
            subtitle=_SAMPLE_DECK["subtitle"],
            slides=_SAMPLE_DECK["slides"],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            footer_enabled=True,
            slide_numbers=True,
        )
        assert len(html) > 1000  # non-trivial output
        assert "<!DOCTYPE html>" in html

    def test_includes_brand_colors(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("deck.html.j2")
        html = template.render(
            title=_SAMPLE_DECK["title"],
            subtitle=_SAMPLE_DECK["subtitle"],
            slides=_SAMPLE_DECK["slides"],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            footer_enabled=True,
            slide_numbers=True,
        )
        # Brand primary color appears in output
        assert kit.colors["primary"] in html

    def test_renders_each_slide_type(self, test_kits_dir):
        """Smoke test: every slide_type in the schema must render
        without template errors."""
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("deck.html.j2")
        html = template.render(
            title=_SAMPLE_DECK["title"],
            subtitle=_SAMPLE_DECK["subtitle"],
            slides=_SAMPLE_DECK["slides"],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            footer_enabled=True,
            slide_numbers=True,
        )
        # Each slide type leaves a trace
        assert "slide cover" in html
        assert "slide section" in html
        assert "slide content" in html
        assert "slide data" in html
        assert "slide quote" in html
        assert "slide close" in html

    def test_image_hint_rendered_as_placeholder(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("deck.html.j2")
        html = template.render(
            title=_SAMPLE_DECK["title"],
            subtitle=_SAMPLE_DECK["subtitle"],
            slides=_SAMPLE_DECK["slides"],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            footer_enabled=True,
            slide_numbers=True,
        )
        assert "image-hint" in html
        assert "abstract architectural illustration" in html


# ── One-pager template ──────────────────────────────────────────────


class TestOnePagerTemplate:
    def test_renders_without_error(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("one_pager.html.j2")
        html = template.render(
            title=_SAMPLE_ONE_PAGER["title"],
            subtitle=_SAMPLE_ONE_PAGER["subtitle"],
            hero_stat=_SAMPLE_ONE_PAGER["hero_stat"],
            lead=_SAMPLE_ONE_PAGER["lead"],
            sections=_SAMPLE_ONE_PAGER["sections"],
            proof=_SAMPLE_ONE_PAGER["proof"],
            cta=_SAMPLE_ONE_PAGER["cta"],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            tagline=kit.tagline_short,
        )
        assert "<!DOCTYPE html>" in html
        assert _SAMPLE_ONE_PAGER["title"] in html

    def test_hero_stat_rendered(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("one_pager.html.j2")
        html = template.render(
            title="t", subtitle="s",
            hero_stat={"value": "999", "label": "test units"},
            lead="lead",
            sections=[{"heading": "h", "body_md": "b"}],
            proof=[],
            cta={"headline": "h", "detail": "d"},
            colors=kit.colors, fonts=kit.fonts,
            company_name=kit.name, tagline=kit.tagline_short,
        )
        assert "999" in html
        assert "test units" in html


# ── Doc template ────────────────────────────────────────────────────


class TestDocTemplate:
    def test_renders_without_error(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("doc.html.j2")
        html = template.render(
            title=_SAMPLE_DOC["title"],
            subtitle=_SAMPLE_DOC["subtitle"],
            doc_type=_SAMPLE_DOC["doc_type"],
            executive_summary=_SAMPLE_DOC["executive_summary"],
            sections=_SAMPLE_DOC["sections"],
            appendix=_SAMPLE_DOC["appendix"],
            cta=_SAMPLE_DOC["cta"],
            footer_note=_SAMPLE_DOC["footer_note"],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            tagline=kit.tagline_short,
        )
        assert "Executive Summary" in html
        assert _SAMPLE_DOC["executive_summary"] in html
        # CTA and appendix render
        assert "Sign and return" in html
        assert "Specs" in html


# ── Site preview template ───────────────────────────────────────────


class TestSitePreviewTemplate:
    def test_renders_without_error(self, test_kits_dir):
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("site_preview.html.j2")
        html = template.render(
            page=_SAMPLE_PAGE,
            meta=_SAMPLE_PAGE["meta"],
            sitemap=[
                {"slug": "home", "title": "Home", "kind": "home"},
                {"slug": "features", "title": "Features", "kind": "product"},
            ],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            tagline=kit.tagline_short,
        )
        assert "<!DOCTYPE html>" in html
        # Hero heading appears
        assert "Infrastructure that works" in html
        # Features items appear
        assert "Item A" in html and "Item B" in html
        # Cross-page nav link generated
        assert "features.html" in html

    def test_section_items_use_dict_key_access(self, test_kits_dir):
        """Regression test: 'section.items' triggered Python's dict.items()
        method when 'items' wasn't a key. The fix uses section['items'].
        Verify the template doesn't regress to attribute access."""
        from astra.creators.kits import load_kit
        from astra.creators.render import _jinja_env
        from tests.test_creators.conftest import TEST_KIT_SLUG

        kit = load_kit(TEST_KIT_SLUG)
        env = _jinja_env()
        template = env.get_template("site_preview.html.j2")
        # A page WITHOUT items key — should render without raising
        page_no_items = {
            **_SAMPLE_PAGE,
            "sections": [
                {
                    "type": "hero",
                    "id": "hero",
                    "heading": "Bare hero",
                    # No 'items', no 'bullets'
                },
            ],
        }
        html = template.render(
            page=page_no_items,
            meta=page_no_items["meta"],
            sitemap=[{"slug": "home", "title": "Home", "kind": "home"}],
            colors=kit.colors,
            fonts=kit.fonts,
            company_name=kit.name,
            tagline=kit.tagline_short,
        )
        # No template error, hero rendered
        assert "Bare hero" in html
