"""
Unit tests for astra/creators/_shared.py

Pure-function helpers that all draft tools depend on. Cheap and fast
to test exhaustively, so coverage here should be tight.
"""

from __future__ import annotations

import pytest

from astra.creators._shared import (
    check_forbidden,
    join_text_fields,
    strip_code_fences,
)


# ── check_forbidden ─────────────────────────────────────────────────


class TestCheckForbidden:
    def test_empty_forbidden_returns_empty_list(self):
        assert check_forbidden("any text", []) == []
        assert check_forbidden("any text", None) == []  # type: ignore[arg-type]

    def test_finds_simple_match(self):
        hits = check_forbidden("we are world-class at this", ["world-class"])
        assert hits == ["world-class"]

    def test_case_insensitive(self):
        # The whole point of the loose match: catch "World-Class",
        # "WORLD-CLASS", "world-class" all the same.
        for variant in ("World-Class", "WORLD-CLASS", "World-class"):
            assert check_forbidden(f"we are {variant}", ["world-class"]) == ["world-class"]

    def test_returns_multiple_hits(self):
        text = "we leverage synergy to be world-class"
        hits = check_forbidden(text, ["leverage", "synergy", "world-class"])
        assert set(hits) == {"leverage", "synergy", "world-class"}

    def test_no_hit_returns_empty(self):
        assert check_forbidden("clean prose, no banned words", ["world-class"]) == []

    def test_substring_match_includes_partial_matches(self):
        # The match is intentionally substring-based so close variants
        # like "AI-Powered" / "AI-powered" are both caught.
        assert check_forbidden("our AI-powered platform", ["AI-powered"]) == ["AI-powered"]

    def test_handles_empty_text(self):
        assert check_forbidden("", ["anything"]) == []


# ── strip_code_fences ───────────────────────────────────────────────


class TestStripCodeFences:
    def test_strips_json_fences(self):
        text = '```json\n{"key": "value"}\n```'
        assert strip_code_fences(text) == '{"key": "value"}'

    def test_strips_plain_fences(self):
        text = '```\n{"key": "value"}\n```'
        assert strip_code_fences(text) == '{"key": "value"}'

    def test_no_fences_returns_unchanged(self):
        text = '{"key": "value"}'
        assert strip_code_fences(text) == '{"key": "value"}'

    def test_strips_leading_whitespace(self):
        text = '  \n  {"key": "value"}  '
        assert strip_code_fences(text) == '{"key": "value"}'

    def test_handles_fences_with_extra_whitespace(self):
        text = '```json\n\n{"key": "value"}\n\n```'
        result = strip_code_fences(text)
        assert '{"key": "value"}' in result
        assert "```" not in result


# ── join_text_fields ────────────────────────────────────────────────


class TestJoinTextFields:
    def test_pulls_string_fields(self):
        d = {"title": "Hello", "subtitle": "World", "body": "Body text"}
        result = join_text_fields(d, ("title", "subtitle"))
        assert "Hello" in result
        assert "World" in result
        assert "Body text" not in result  # not requested

    def test_pulls_list_fields(self):
        d = {"bullets": ["a", "b", "c"]}
        result = join_text_fields(d, ("bullets",))
        assert "a" in result and "b" in result and "c" in result

    def test_handles_missing_fields_silently(self):
        d = {"title": "Hello"}
        result = join_text_fields(d, ("title", "missing", "also_missing"))
        assert "Hello" in result
        # No exception, no None in output
        assert "None" not in result

    def test_handles_non_dict_input(self):
        # Defensive: caller may accidentally pass None or a list
        assert join_text_fields(None, ("anything",)) == ""  # type: ignore[arg-type]
        assert join_text_fields([1, 2, 3], ("anything",)) == ""  # type: ignore[arg-type]

    def test_mixed_types_concatenated(self):
        d = {"text": "hello", "items": [1, 2, "three"]}
        result = join_text_fields(d, ("text", "items"))
        assert "hello" in result
        assert "three" in result
        assert "1" in result and "2" in result


# ── generate_json (mocked-LLM contract) ─────────────────────────────


@pytest.mark.asyncio
class TestGenerateJsonContract:
    """The generate_json helper is async and calls Anthropic. These
    tests verify its contract — happy path, JSON parse failure,
    forbidden-phrase regeneration loop — using a mocked client."""

    async def test_happy_path_parses_returned_json(self, mock_anthropic):
        from astra.creators._shared import generate_json

        mock_anthropic('{"title": "Test", "body": "Clean copy"}')
        result = await generate_json(
            system="sys",
            user="user",
            forbidden=[],
            text_blob_fn=lambda d: str(d),
        )
        assert result == {"title": "Test", "body": "Clean copy"}

    async def test_strips_code_fences_before_parse(self, mock_anthropic):
        from astra.creators._shared import generate_json

        mock_anthropic('```json\n{"title": "Fenced"}\n```')
        result = await generate_json(
            system="sys",
            user="user",
            forbidden=[],
            text_blob_fn=lambda d: str(d),
        )
        assert result == {"title": "Fenced"}

    async def test_invalid_json_raises(self, mock_anthropic):
        import json
        from astra.creators._shared import generate_json

        mock_anthropic("this is not JSON at all")
        with pytest.raises(json.JSONDecodeError):
            await generate_json(
                system="sys",
                user="user",
                forbidden=[],
                text_blob_fn=lambda d: str(d),
            )

    async def test_regenerates_once_on_forbidden_hit(self, mock_anthropic):
        """The contract: if forbidden phrases land, the model gets ONE
        retry with explicit feedback. If the retry is also dirty, we
        return the dirty result and log loudly (caller decides).

        Verify: a sequence of (dirty, clean) responses produces the
        clean result; the regeneration was triggered."""
        from astra.creators._shared import generate_json

        responses = iter([
            '{"body": "we are world-class"}',  # first attempt — dirty
            '{"body": "we are excellent"}',     # retry — clean
        ])

        def vary(**kwargs):
            return next(responses)

        mock_anthropic(vary)

        result = await generate_json(
            system="sys",
            user="user",
            forbidden=["world-class"],
            text_blob_fn=lambda d: d.get("body", ""),
        )
        assert result == {"body": "we are excellent"}

    async def test_forbidden_persists_through_retry_returns_dirty(self, mock_anthropic, caplog):
        """If retry is also dirty, return the result and log loudly.
        This is the safety net — caller sees the artifact and can
        decide to regenerate manually rather than fail outright."""
        from astra.creators._shared import generate_json

        # Both responses dirty — retry doesn't fix it
        mock_anthropic('{"body": "still world-class after retry"}')

        with caplog.at_level("ERROR"):
            result = await generate_json(
                system="sys",
                user="user",
                forbidden=["world-class"],
                text_blob_fn=lambda d: d.get("body", ""),
            )

        # Result is still returned (not raised)
        assert "world-class" in result["body"]
        # And the error was logged
        assert any("STILL present" in r.message for r in caplog.records)

    async def test_no_anthropic_key_raises(self, monkeypatch):
        from astra.creators._shared import generate_json

        # Wipe both env and settings
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # Monkey-patch settings to also be empty
        from astra.config import settings
        monkeypatch.setattr(settings, "anthropic_api_key", "", raising=False)
        # And block the .env file fallback by patching get_anthropic_key directly
        monkeypatch.setattr(
            "astra.creators._shared.get_anthropic_key", lambda: ""
        )

        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            await generate_json(
                system="sys",
                user="user",
                forbidden=[],
                text_blob_fn=lambda d: "",
            )
