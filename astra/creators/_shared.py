"""
Shared utilities for the creator draft tools.

Every draft tool — deck, doc, one-pager, brand-kit — follows the
same loop:

  build prompt → call Claude → parse JSON → forbidden-phrase scan →
  regenerate once if forbidden phrases land → return parsed dict.

The bits that differ per tool are: the system prompt, the JSON schema,
and which fields to scan for forbidden phrases. This module exposes
the loop as a reusable function (`generate_json`) so each
draft_<kind>.py file can stay short and tool-specific.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable

import anthropic

from astra.config import settings

logger = logging.getLogger(__name__)


# Model selection — Sonnet for drafting (quality matters when the
# artifact is going to a Fortune-500 procurement lead or a Tier-1 VC),
# Haiku for cheaper passes (critique, image-prompt enhancement).
DRAFT_MODEL = os.environ.get("CREATOR_DRAFT_MODEL", "claude-sonnet-4-6")
CRITIQUE_MODEL = os.environ.get("CREATOR_CRITIQUE_MODEL", "claude-haiku-4-5")
IMAGE_PROMPT_MODEL = os.environ.get("CREATOR_IMAGE_PROMPT_MODEL", "claude-haiku-4-5")
DEFAULT_MAX_TOKENS = 8000


def get_anthropic_key() -> str:
    """Resolve the Anthropic API key.

    Order: settings.anthropic_api_key → ANTHROPIC_API_KEY env →
    .env file walked from this module up. Mirrors the dance used
    elsewhere so the creator tools work in dev (with .env) and on
    Railway (with Railway-injected env vars).
    """
    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def check_forbidden(text_blob: str, forbidden: Iterable[str]) -> list[str]:
    """Return the list of forbidden phrases that appear in `text_blob`.

    Case-insensitive substring match. Intentionally loose: "AI-Powered"
    matches "AI-powered" matches "ai-powered". We'd rather over-flag
    and force a regeneration than ship a draft with a forbidden phrase.
    """
    forbidden = list(forbidden) if forbidden else []
    if not forbidden:
        return []
    lower = text_blob.lower()
    return [p for p in forbidden if p.lower() in lower]


def strip_code_fences(text_out: str) -> str:
    """Strip ```json … ``` fences if the model added them despite the rule."""
    text_out = text_out.strip()
    if text_out.startswith("```"):
        text_out = re.sub(r"^```(?:json)?|```$", "", text_out, flags=re.M).strip()
    return text_out


async def generate_json(
    *,
    system: str,
    user: str,
    forbidden: list[str],
    text_blob_fn: Callable[[dict[str, Any]], str],
    model: str = DRAFT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    _retry: bool = False,
) -> dict[str, Any]:
    """Call Claude with `system` + `user`, parse JSON, regenerate once on forbidden hits.

    Args:
      system: the system prompt for this draft kind
      user:   the user message — the kit + audience + ask + context bundle
      forbidden: list of forbidden phrases from the kit's brand.yml
      text_blob_fn: given the parsed JSON, return one big string with all
        the text that should be scanned for forbidden phrases. Each tool's
        schema is different, so the caller knows which fields to flatten.
      model: defaults to DRAFT_MODEL (Sonnet); pass CRITIQUE_MODEL or
        IMAGE_PROMPT_MODEL for cheaper passes.
      _retry: internal — set True on the regeneration attempt.

    Returns: parsed dict.

    Why one regeneration attempt: the failure mode is the model
    leaking a forbidden phrase ("AI-powered") despite the system prompt
    banning it. One feedback round fixes ~95%; a second round is
    diminishing returns and the human reviewer should look at it then.
    """
    key = get_anthropic_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set; cannot draft")
    client = anthropic.AsyncAnthropic(api_key=key)

    resp = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text_out = "\n".join(b.text for b in resp.content if hasattr(b, "text")).strip()
    text_out = strip_code_fences(text_out)

    try:
        parsed = json.loads(text_out)
    except json.JSONDecodeError as e:
        logger.error("[creator] JSON parse failed: %s", e)
        logger.error("[creator] raw response head: %s", text_out[:500])
        raise

    blob = text_blob_fn(parsed)
    hits = check_forbidden(blob, forbidden)
    if hits and not _retry:
        logger.warning(
            "[creator] forbidden phrases hit, regenerating: %s", hits
        )
        feedback = (
            f"Your previous draft contained forbidden phrases: {hits}. "
            "Rewrite without using ANY of these words or close variants. "
            "Same JSON schema."
        )
        return await generate_json(
            system=system,
            user=f"{user}\n\n<previous-attempt-feedback>\n{feedback}\n</previous-attempt-feedback>",
            forbidden=forbidden,
            text_blob_fn=text_blob_fn,
            model=model,
            max_tokens=max_tokens,
            _retry=True,
        )
    if hits and _retry:
        # Surface the issue but don't fail — the caller sees the artifact
        # and can decide to regenerate manually.
        logger.error(
            "[creator] forbidden phrases STILL present after retry: %s", hits
        )
        # Layer 4 hook: log this as a self-improvement observation so
        # the queue surfaces a recurring voice-discipline failure for
        # the founder to review. Lazy import to avoid a circular import
        # via store -> db -> self_improve -> _shared.
        try:
            from astra.creators.self_improve import (
                auto_observe_persistent_forbidden,
            )
            await auto_observe_persistent_forbidden(
                forbidden_hits=hits,
            )
        except Exception as obs_err:
            logger.warning(
                "[creator] self-improve hook failed: %s", obs_err
            )

    return parsed


def join_text_fields(d: Any, fields: Iterable[str]) -> str:
    """Helper: pull a set of string-or-list-of-string fields out of a dict
    and concatenate their text for forbidden-phrase scanning. Used by
    text_blob_fn implementations across draft kinds."""
    parts: list[str] = []
    if not isinstance(d, dict):
        return ""
    for f in fields:
        v = d.get(f)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(str(x) for x in v)
    return "\n".join(parts)
