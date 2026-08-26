"""Provider failover — a REAL fallback, not a second copy of the same key.

Why this exists: on 2026-08-26 the Anthropic credit balance ran out and
every LLM-shaped operation in the fleet stopped. The variable named
ANTHROPIC_API_KEY_CLAUDE_BACKUP turned out to hold the SAME credential as
the primary, so it failed at the same instant. A backup that shares the
failure mode of the thing it backs up is not a backup.

Design:
  - Claude stays PRIMARY. Its stricter API contract is what surfaced the
    thinking-signature, strict-JSON and truncation bugs that the laxer
    provider had been hiding, so we do not want to live on the fallback.
  - Kimi (Moonshot's anthropic-compatible endpoint) is the BRIDGE. It
    engages only on provider-level failure, and it announces itself in
    the logs every single time so a silent degradation is impossible.
  - We fail over on: credit exhaustion, auth failure, rate limiting,
    overload and 5xx. We do NOT fail over on 400s caused by our own
    malformed request, because retrying a bad request on another
    provider just produces a second, more confusing error.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_FAILOVER_MARKERS = (
    "credit balance is too low",
    "rate_limit",
    "overloaded",
    "authentication_error",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "timeout",
)


def _fallback_config() -> tuple[str, str, str] | None:
    """(api_key, base_url, model) for the bridge provider, or None."""
    key = (os.environ.get("ANTHROPIC_API_KEY_KIMI_PARKED")
           or os.environ.get("KIMI_API_KEY") or "").strip()
    if not key:
        return None
    base = (os.environ.get("KIMI_BASE_URL") or "https://api.moonshot.ai/anthropic").strip()
    model = (os.environ.get("KIMI_MODEL") or "kimi-k2.6").strip()
    return key, base, model


def _should_failover(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 429, 500, 502, 503, 504):
        return True
    # A 400 is normally OUR bug, except the credit-balance case which
    # Anthropic also returns as 400.
    return any(m in text for m in _FAILOVER_MARKERS)


async def acreate(**kwargs: Any) -> Any:
    """messages.create() with a provider bridge behind it.

    Drop-in for `client.messages.create(**kwargs)`. The caller does not
    need to know which provider served the request; the log line does.
    """
    import anthropic

    primary_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    try:
        client = anthropic.AsyncAnthropic(api_key=primary_key) if primary_key \
            else anthropic.AsyncAnthropic()
        return await client.messages.create(**kwargs)
    except Exception as exc:
        if not _should_failover(exc):
            raise
        cfg = _fallback_config()
        if cfg is None:
            logger.error("[llm] primary failed (%s) and NO bridge configured", exc)
            raise
        key, base, model = cfg
        logger.warning(
            "[llm] PRIMARY FAILED (%s) -> failing over to bridge provider "
            "%s model=%s. This is a degraded path; fix the primary.",
            str(exc)[:160], base, model,
        )
        kw = dict(kwargs)
        kw["model"] = model
        # The bridge does not implement Anthropic's server-side tools.
        # Sending them produces a confusing 400 on top of an outage.
        if kw.get("tools") and any(
            isinstance(t, dict) and str(t.get("type", "")).startswith("web_search")
            for t in kw["tools"]
        ):
            kw.pop("tools", None)
            logger.warning("[llm] bridge lacks server-side web_search; dropped tools")
        fb = anthropic.AsyncAnthropic(api_key=key, base_url=base)
        return await fb.messages.create(**kw)


def bridge_status() -> dict[str, Any]:
    """For fleet_status / diagnostics. Never returns the key itself."""
    cfg = _fallback_config()
    return {
        "primary_key_set": bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip()),
        "bridge_configured": cfg is not None,
        "bridge_base_url": cfg[1] if cfg else None,
        "bridge_model": cfg[2] if cfg else None,
    }


# ── Streaming paths ────────────────────────────────────────────────
#
# The agent loop STREAMS rather than calling create(), and failing over
# mid-stream would mean restructuring the most delicate code in the
# system while a turn is already emitting text to the user. Instead we
# decide the provider BEFORE the stream opens, using a cached health
# probe. Cost is one ~10-token call per _PROBE_TTL, not per turn.

import time as _time

_PROBE_TTL = 120.0
_probe_cache: dict[str, Any] = {"at": 0.0, "primary_ok": True}


async def _primary_healthy() -> bool:
    now = _time.monotonic()
    if now - float(_probe_cache["at"]) < _PROBE_TTL:
        return bool(_probe_cache["primary_ok"])
    import anthropic

    ok = True
    try:
        key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        c = anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()
        await c.messages.create(
            model=(os.environ.get("MODEL_HAIKU") or "claude-haiku-4-5-20251001"),
            max_tokens=1,
            messages=[{"role": "user", "content": "."}],
        )
    except Exception as exc:
        if _should_failover(exc):
            ok = False
            logger.warning("[llm] primary provider unhealthy: %s", str(exc)[:160])
        # a non-failover error (our own bad request) says nothing about health
    _probe_cache.update(at=now, primary_ok=ok)
    return ok


async def maybe_bridge(client: Any, default_model: str) -> tuple[Any, str, bool]:
    """Swap `client` for the bridge ONLY if a bridge is configured AND the
    primary is known-unhealthy. Otherwise the caller's client is returned
    untouched.

    The configured-bridge check comes FIRST and deliberately: with no
    bridge (local dev, CI, any test that patches AsyncAnthropic) this
    function performs no network call and changes nothing, so wiring it
    into the agent loop cannot alter behaviour where it cannot help.
    """
    cfg = _fallback_config()
    if cfg is None:
        return client, default_model, False
    if await _primary_healthy():
        return client, default_model, False
    key, base, model = cfg
    logger.warning("[llm] primary unhealthy — this turn runs on the BRIDGE (%s, %s)", base, model)
    import anthropic

    return anthropic.AsyncAnthropic(api_key=key, base_url=base), model, True
