"""
Shared fixtures for creator-tool tests.

Architecture decisions:

1. **Tests never hit the real Anthropic API.** A `mock_anthropic`
   fixture patches `anthropic.AsyncAnthropic` so every draft tool
   call returns a predetermined response. Real-API tests are
   marked `@pytest.mark.integration` and only run when explicitly
   requested.

2. **Tests never hit the real database.** A `mock_store` fixture
   patches `create_artifact` / `get_artifact` / etc. with in-memory
   dict-backed implementations. Faster than spinning up a test DB
   and avoids couplings between unrelated tests.

3. **Tests use a fixture kit, never the real kits.** The
   `test_kits_dir` fixture sets `BUSINESS_KITS_DIR` to point at
   `tests/fixtures/test_kit/`'s parent directory. The test kit
   slug is `testco`. Real kits in `business-kits/` are never
   loaded by the test suite.

4. **Each test that mutates the fixture kit gets a fresh copy.**
   The `tmp_kit_dir` fixture copies the test_kit fixture to a
   tempdir, points BUSINESS_KITS_DIR at it, and tears down on
   exit. Mutation tests can corrupt files freely without
   affecting other tests.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Paths ───────────────────────────────────────────────────────────


_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
_FIXTURE_KIT_NAME = "test_kit"
_FIXTURE_KIT_PATH = _FIXTURES_DIR / _FIXTURE_KIT_NAME

# When BUSINESS_KITS_DIR points here, load_kit("testco") becomes the
# fixture. Tests reference TEST_KIT_SLUG so renames are easy.
TEST_KIT_SLUG = "testco"


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the path to tests/fixtures/."""
    return _FIXTURES_DIR


@pytest.fixture
def fixture_kit_path() -> Path:
    """Path to tests/fixtures/test_kit/ (the read-only fixture)."""
    return _FIXTURE_KIT_PATH


# ── Read-only kit fixture ───────────────────────────────────────────


@pytest.fixture
def test_kits_dir(monkeypatch, tmp_path):
    """Set BUSINESS_KITS_DIR to a directory that contains ONLY the test
    kit (renamed to TEST_KIT_SLUG).

    Read-only: do NOT mutate files under here. Use `tmp_kit_dir` for
    mutation tests instead.

    Returns: Path to the kits dir.
    """
    kits_root = tmp_path / "kits"
    kits_root.mkdir()
    target = kits_root / TEST_KIT_SLUG
    shutil.copytree(_FIXTURE_KIT_PATH, target)

    monkeypatch.setenv("BUSINESS_KITS_DIR", str(kits_root))
    return kits_root


# ── Mutable kit fixture (per-test fresh copy) ───────────────────────


@pytest.fixture
def tmp_kit_dir(monkeypatch, tmp_path):
    """Mutable fixture kit. Each test gets a fresh copy of the test
    kit in tmp_path; mutations here don't affect other tests OR the
    real fixture.

    Yields: tuple (kits_root, kit_dir) where:
      kits_root = parent dir BUSINESS_KITS_DIR points at
      kit_dir = kits_root / TEST_KIT_SLUG
    """
    kits_root = tmp_path / "kits"
    kits_root.mkdir()
    kit_dir = kits_root / TEST_KIT_SLUG
    shutil.copytree(_FIXTURE_KIT_PATH, kit_dir)

    monkeypatch.setenv("BUSINESS_KITS_DIR", str(kits_root))
    yield kits_root, kit_dir


# ── Mock Anthropic client ───────────────────────────────────────────


def _make_mock_response(text: str):
    """Build a fake anthropic Response with one text block."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.fixture
def mock_anthropic(monkeypatch):
    """Patch `anthropic.AsyncAnthropic` to return a controllable mock.

    Yields a callable: `set_response(text_or_callable)` that controls
    what the next `messages.create()` call returns.

      - If `text` is a str, every call returns that text.
      - If callable, it's called with (system, user, model, max_tokens)
        kwargs each call and the return value becomes the response text.
        Useful for varying responses per call (e.g. testing the
        regeneration loop).
    """
    state: dict[str, Any] = {"response": '{}'}

    def set_response(text_or_callable):
        state["response"] = text_or_callable

    async def fake_create(**kwargs):
        r = state["response"]
        if callable(r):
            text = r(**kwargs)
        else:
            text = r
        return _make_mock_response(text)

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=fake_create)

    fake_class = MagicMock(return_value=fake_client)

    # Patch in BOTH places that import it. Each draft module does
    # `import anthropic` then references `anthropic.AsyncAnthropic`
    # — patching the attribute on the anthropic module covers all
    # call sites.
    monkeypatch.setattr("anthropic.AsyncAnthropic", fake_class)
    # And ensure get_anthropic_key returns a non-empty value so the
    # generate_json path doesn't bail out.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    return set_response


# ── Mock store (in-memory artifact persistence) ─────────────────────


@pytest.fixture
def mock_store(monkeypatch):
    """Patch creator store functions with in-memory dict-backed versions.

    Yields the in-memory store dict. Tests can inspect it to assert
    what was saved.
    """
    artifacts: dict[int, dict[str, Any]] = {}
    counter = {"next_id": 1}

    async def fake_create(**kwargs):
        aid = counter["next_id"]
        counter["next_id"] += 1
        from datetime import datetime, timezone
        row = {
            "id": aid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "business_slug": kwargs.get("business_slug"),
            "kind": kwargs.get("kind"),
            "audience_slug": kwargs.get("audience_slug"),
            "title": kwargs.get("title"),
            "ask": kwargs.get("ask", ""),
            "content": kwargs.get("content"),
            "parent_id": kwargs.get("parent_id"),
            "r2_pdf_key": "",
            "r2_pptx_key": "",
            "updated_at": None,
        }
        artifacts[aid] = row
        return dict(row)  # caller gets a copy

    async def fake_get(artifact_id):
        row = artifacts.get(int(artifact_id))
        return dict(row) if row else None

    async def fake_list(*, business_slug=None, kind=None, limit=25):
        rows = list(artifacts.values())
        if business_slug:
            rows = [r for r in rows if r["business_slug"] == business_slug]
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def fake_update(artifact_id, *, kind, key):
        col = {"pdf": "r2_pdf_key", "pptx": "r2_pptx_key"}.get(kind)
        if not col:
            raise ValueError(f"unknown render kind: {kind}")
        if int(artifact_id) in artifacts:
            artifacts[int(artifact_id)][col] = key
            return True
        return False

    # Patch the store functions in every module that imports them.
    # Using setattr on the source module catches all import-time
    # references because they call `from astra.creators.store import X`
    # at the module level — patching there means the imported
    # references point to our mocks.
    monkeypatch.setattr("astra.creators.store.create_artifact", fake_create)
    monkeypatch.setattr("astra.creators.store.get_artifact", fake_get)
    monkeypatch.setattr("astra.creators.store.list_artifacts", fake_list)
    monkeypatch.setattr("astra.creators.store.update_artifact_render_key", fake_update)

    # Also patch in modules that imported them at the top level
    # (these are needed because Python module-level imports bind names
    # at import time; patching the source module is necessary but not
    # always sufficient).
    for path in [
        "astra.creators.draft.create_artifact",
        "astra.creators.draft_one_pager.create_artifact",
        "astra.creators.draft_doc.create_artifact",
        "astra.creators.draft_brand_kit.create_artifact",
        "astra.creators.draft_carousel.create_artifact",
        "astra.creators.draft_thread.create_artifact",
        "astra.creators.draft_caption_set.create_artifact",
        "astra.creators.draft_hashtag_set.create_artifact",
        "astra.creators.draft_video_brief.create_artifact",
        "astra.creators.draft_voiceover_script.create_artifact",
        "astra.creators.draft_subtitle_set.create_artifact",
        "astra.creators.draft_site_brief.create_artifact",
        "astra.creators.draft_page_content.create_artifact",
        "astra.creators.draft_component_spec.create_artifact",
        "astra.creators.analyze_reference_site.create_artifact",
        "astra.creators.critique.create_artifact",
        "astra.creators.image.create_artifact",
        "astra.creators.draft_voiceover_script.get_artifact",
        "astra.creators.draft_subtitle_set.get_artifact",
        "astra.creators.draft_page_content.get_artifact",
        "astra.creators.draft_component_spec.get_artifact",
        "astra.creators.draft_site_brief.get_artifact",
        "astra.creators.critique.get_artifact",
    ]:
        try:
            monkeypatch.setattr(path, fake_create if path.endswith("create_artifact") else fake_get)
        except AttributeError:
            # Module may not have imported it; that's fine
            pass

    return artifacts
