"""Tests for astra/creators/draft_component_spec.py — component specs."""
from __future__ import annotations

import json
import pytest

from tests.test_creators.conftest import TEST_KIT_SLUG


VALID_COMPONENT_SPEC = json.dumps({
    "component_type": "hero",
    "context": "home > hero",
    "intent": "open with the value prop",
    "structure": {
        "layout": "Two-column 60/40 on desktop. Stacks on tablet.",
        "slots": [
            {"name": "headline", "type": "text", "purpose": "main heading",
             "max_chars": 80, "voice_register": "institutional",
             "required": True, "default_state": "show placeholder"},
            {"name": "cta", "type": "button", "purpose": "primary action",
             "max_chars": None, "voice_register": "institutional",
             "required": True, "default_state": "Schedule a call"},
        ],
    },
    "interaction": {
        "default_state": "static",
        "hover_state": "CTA fills brand color",
        "active_state": "depressed",
        "focus_state": "outline",
        "scroll_behavior": "static",
        "transitions": "180ms ease-out",
    },
    "responsive": {
        "desktop_breakpoint": ">=1024px: two-column",
        "tablet_breakpoint": "768-1023px: stacked",
        "mobile_breakpoint": "<768px: stacked, condensed",
        "minimum_supported_width": "320px",
    },
    "accessibility": {
        "semantic_html": "<section role='banner'>",
        "aria_attributes": ["aria-labelledby on section"],
        "keyboard_pattern": "Tab to CTA",
        "screen_reader_notes": "Reads headline first",
        "contrast_requirements": "4.5:1 minimum",
        "motion_safety": "Honors prefers-reduced-motion",
    },
    "image_direction": {
        "needed": True, "aspect_ratio": "16:9",
        "subject": "abstract architectural",
        "treatment": "high contrast",
        "anti_patterns": ["stock photos"],
    },
    "implementation_notes": ["Use CSS grid for layout"],
    "recommended_libraries": [],
    "states_and_edge_cases": [],
})


@pytest.mark.asyncio
class TestDraftComponentSpecHappyPath:
    async def test_returns_saved_artifact(self, test_kits_dir, mock_anthropic, mock_store):
        from astra.creators.draft_component_spec import draft_component_spec
        mock_anthropic(VALID_COMPONENT_SPEC)
        artifact = await draft_component_spec(
            business_slug=TEST_KIT_SLUG, component_type="hero",
            intent="open with the value prop",
            page_context="home > hero",
        )
        assert artifact["kind"] == "component_spec"
        assert len(artifact["content"]["structure"]["slots"]) == 2

    async def test_no_audience_required(self, test_kits_dir, mock_anthropic, mock_store):
        """Unlike most draft tools, component_spec doesn't require an
        audience persona (the kit's voice is used directly)."""
        from astra.creators.draft_component_spec import draft_component_spec
        mock_anthropic(VALID_COMPONENT_SPEC)
        # Without audience_slug
        artifact = await draft_component_spec(
            business_slug=TEST_KIT_SLUG, component_type="hero",
            intent="x",
        )
        assert artifact["kind"] == "component_spec"

    async def test_with_parent_artifacts(
        self, test_kits_dir, mock_anthropic, mock_store,
    ):
        """When site_brief_id and page_content_id are given, the parent
        chain is set."""
        from astra.creators.draft_component_spec import draft_component_spec
        from astra.creators.store import create_artifact

        brief = await create_artifact(
            business_slug=TEST_KIT_SLUG, kind="site_brief",
            title="b", ask="x", content={"sitemap": []},
        )

        mock_anthropic(VALID_COMPONENT_SPEC)
        artifact = await draft_component_spec(
            business_slug=TEST_KIT_SLUG, component_type="hero",
            intent="x", site_brief_id=brief["id"],
        )
        assert artifact["parent_id"] == brief["id"]
