"""
The autonomous research agent — spins up on demand, research ONLY.

Why this exists (2026-07-22): the single-shot runner fabricated a
negative claim ("No PSA India events detected") with zero sources, it
survived to a drafted public post, and Kunal caught it (India hosted
~15). Root causes: one call doing plan+search+synthesis, no source
enforcement in code, no verification pass. This agent is the class fix,
and the scoped answer to "spawn agents": purpose-built research
sub-agents only, no general spawning.

Pipeline (each stage a separate Claude call, sub-searchers parallel):

  PLAN        decompose the query into sub-questions + search strategy
  SEARCH  ×N  one sub-researcher per sub-question, web_search enabled,
              findings MUST carry source URLs (parallel, bounded)
  GUARD       code, not vibes: sourceless findings demoted to open
              questions; negative claims quarantined unless a source
              explicitly establishes the absence
  VERIFY      adversarial re-check of load-bearing findings ("try to
              refute; find a primary source") with fresh searches
  SYNTHESIZE  final briefing composed from surviving material only,
              every finding labeled VERIFIED / REPORTED / UNCONFIRMED

Also exposes claim_check() — verdict per claim against fresh research —
used by the content pipeline so no public claim ships unsourced.

Cost: standard ≈ 6-9 Claude calls / ~20 searches; deep ≈ 10-14 calls /
~40 searches. "quick" preserves the old single-shot path via runner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Bounded parallelism for sub-researchers: enough to be fast, low
# enough to stay inside API rate limits alongside the rest of Astra.
_MAX_PARALLEL = 4

_NEGATIVE_RX = re.compile(
    r"\b(no|none|never|nobody|nothing|zero)\b.{0,60}\b(exist|exists|"
    r"available|scheduled|detected|found|offers?|has|have|hosts?|held)\b"
    r"|\bdoes not exist\b|\bno longer\b|\bthere (?:is|are) no\b",
    re.IGNORECASE,
)


def _looks_negative(text: str) -> bool:
    return bool(_NEGATIVE_RX.search(text or ""))


async def _call(
    *,
    prompt: str,
    max_tokens: int = 3000,
    searches: int = 0,
    model: str | None = None,
) -> str:
    """One Claude call; web_search enabled when searches > 0."""
    import anthropic  # noqa: F401  (client kwargs shape)

    from astra.llm.failover import acreate

    from astra.config import settings
    from astra.research.runner import _get_api_key

    key = _get_api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not available")
    tools: list[dict] = []
    if searches > 0:
        tools.append({
            "type": "web_search_20250305",
            "name": "web_search",
            "max_uses": searches,
        })
    resp = await acreate(
        model=model or settings.model_sonnet,
        max_tokens=max_tokens,
        tools=tools or anthropic.NOT_GIVEN,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    return "\n".join(parts).strip()


def _json_of(raw: str) -> dict[str, Any] | None:
    from astra.research.runner import _safe_json

    return _safe_json(raw)


# ──────────────────────────────────────────────────────────────────
# Stage 1 — PLAN
# ──────────────────────────────────────────────────────────────────

_PLAN_PROMPT = """You are the planning stage of a research agent. Decompose the research question into focused sub-questions a searcher can answer with web searches.

QUESTION: {topic}
FOCUS: {focus}

Rules:
- {n_lo}-{n_hi} sub-questions, each independently searchable.
- Cover the question's core, its counter-evidence ("what would prove the obvious answer wrong?"), and recency ("what changed in the last 90 days?").
- For any expected NEGATIVE conclusion ("X does not exist / nobody does X"), write the sub-question POSITIVELY ("list all instances of X") — searching for presence, never assuming absence.
- Name the authoritative primary sources a searcher should prefer (official calendars, regulator sites, company pages, filings).

STRICT JSON only:
{{"sub_questions": [{{"q": "<the sub-question>", "why": "<what it derisks>", "prefer_sources": ["<domain or source type>"]}}]}}"""


# ──────────────────────────────────────────────────────────────────
# Stage 2 — SEARCH (sub-researcher)
# ──────────────────────────────────────────────────────────────────

_SEARCH_PROMPT = """You are a research sub-agent answering ONE question with web searches. Search first, then report. Today (IST): {today}.

QUESTION: {q}
PREFER SOURCES: {prefer}

Search strategy:
- Try MULTIPLE phrasings: the direct question, entity-first ("<entity> 2026 calendar/list"), and source-first ("<preferred-domain> <topic>") queries. If a preferred source exists (official calendar, federation, regulator), query it BY NAME before settling for blogs.
- For list/calendar/count questions, hunt for the authoritative index page, not news mentions.

Iron rules:
- EVERY finding must cite at least one URL you actually saw in search results. A finding you cannot source does not go in findings; put it in open_questions instead.
- NEVER conclude absence from failed searches. If you searched for X and found nothing, report it in not_found ("searched N times, queries used, nothing surfaced") — that is an observation about your search, not a fact about the world.
- Prefer primary sources over blogs. Note the date of each source if visible.
- Quote numbers exactly as the source states them.

STRICT JSON only:
{{"findings": [{{"finding": "<one factual statement>", "detail": "<2-3 sentences>", "sources": ["<url>"], "source_dates": ["<date or unknown>"], "confidence": "<high|medium|low>"}}],
"not_found": [{{"looked_for": "<what>", "queries_tried": <int>, "note": "<short>"}}],
"open_questions": ["<what remains unanswered>"]}}"""


# ──────────────────────────────────────────────────────────────────
# Stage 4 — VERIFY (adversarial re-check)
# ──────────────────────────────────────────────────────────────────

_VERIFY_PROMPT = """You are the verification stage of a research agent. Your job is to try to REFUTE the finding below with fresh web searches. Be adversarial: assume it is wrong or stale and hunt for the primary source that settles it. Today (IST): {today}.

FINDING: {finding}
DETAIL: {detail}
CLAIMED SOURCES: {sources}

STRICT JSON only:
{{"verdict": "<VERIFIED|REFUTED|UNCONFIRMED>",
"correction": "<if REFUTED: the corrected fact, sourced. else empty>",
"primary_source": "<best URL found, or empty>",
"note": "<1-2 sentences on what you found>"}}"""


# ──────────────────────────────────────────────────────────────────
# Stage 5 — SYNTHESIZE
# ──────────────────────────────────────────────────────────────────

_SYNTH_PROMPT = """You are the synthesis stage of a research agent. Compose the final research briefing from the VERIFIED MATERIAL below. Today (IST): {today}.

QUESTION: {topic}
FOCUS: {focus}

VERIFIED MATERIAL (the only facts you may use — do not add outside knowledge):
{material}

NOT FOUND (searched, nothing surfaced — you may report these ONLY as "not found in our searches", never as "does not exist"):
{not_found}

OPEN QUESTIONS:
{open_qs}

Compose the briefing in this STRICT JSON schema (same contract as Astra's research briefings; omit sections that don't apply as empty arrays):
{schema}

Rules:
- Every finding's sources array must be copied from the material verbatim. No finding without sources.
- Findings carry their verification status in detail, phrased naturally.
- gist: 2-3 sentences, short declarative, no filler.
- Keep the whole JSON under ~11000 characters."""


async def _plan(topic: str, focus: str, depth: str) -> list[dict[str, Any]]:
    n_lo, n_hi = (3, 5) if depth != "deep" else (5, 8)
    raw = await _call(
        prompt=_PLAN_PROMPT.format(topic=topic, focus=focus or "(none)",
                                   n_lo=n_lo, n_hi=n_hi),
        max_tokens=1500,
    )
    parsed = _json_of(raw) or {}
    subs = [s for s in (parsed.get("sub_questions") or [])
            if isinstance(s, dict) and (s.get("q") or "").strip()]
    if not subs:  # degrade to the topic itself as one sub-question
        subs = [{"q": topic, "why": "direct", "prefer_sources": []}]
    return subs[:n_hi]


async def _search_one(sub: dict[str, Any], *, today: str, searches: int) -> dict[str, Any]:
    raw = await _call(
        prompt=_SEARCH_PROMPT.format(
            q=sub.get("q", ""),
            prefer=", ".join(sub.get("prefer_sources") or []) or "any credible",
            today=today,
        ),
        max_tokens=2500,
        searches=searches,
    )
    parsed = _json_of(raw) or {}
    parsed["_sub_q"] = sub.get("q", "")
    return parsed


def _guard(results: list[dict[str, Any]]) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[str]
]:
    """Code-level enforcement. Returns (findings, not_found, open_qs).

    - A finding with zero sources is NOT a finding: demoted to an open
      question. This is the exact gate the PSA fabrication walked
      through when it lived in prompt-rules only.
    - A negative-phrased finding is quarantined to not_found unless its
      own sources exist AND it explicitly cites where absence is
      established.
    """
    findings: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    open_qs: list[str] = []
    for r in results:
        for nf in (r.get("not_found") or []):
            if isinstance(nf, dict):
                not_found.append(nf)
        for q in (r.get("open_questions") or []):
            if isinstance(q, str) and q.strip():
                open_qs.append(q.strip())
        for f in (r.get("findings") or []):
            if not isinstance(f, dict):
                continue
            text_f = f"{f.get('finding','')} {f.get('detail','')}"
            srcs = [s for s in (f.get("sources") or [])
                    if isinstance(s, str) and s.startswith("http")]
            if not srcs:
                open_qs.append(
                    f"UNSOURCED (demoted from finding): {f.get('finding','')}"
                )
                continue
            if _looks_negative(text_f):
                # negative claim: keep only as a search observation
                not_found.append({
                    "looked_for": f.get("finding", ""),
                    "queries_tried": 0,
                    "note": ("negative claim quarantined; sources: "
                             + ", ".join(srcs[:2])),
                })
                continue
            f["sources"] = srcs
            findings.append(f)
    return findings, not_found, open_qs


async def _verify_top(
    findings: list[dict[str, Any]], *, today: str, k: int,
) -> list[dict[str, Any]]:
    """Adversarially re-check the top-k findings; annotate in place.
    REFUTED findings are replaced by their sourced correction or dropped."""
    if not findings:
        return findings
    sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def check(f: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        async with sem:
            try:
                raw = await _call(
                    prompt=_VERIFY_PROMPT.format(
                        finding=f.get("finding", ""),
                        detail=f.get("detail", ""),
                        sources=", ".join(f.get("sources") or []),
                        today=today,
                    ),
                    max_tokens=1200,
                    searches=3,
                )
                return f, (_json_of(raw) or {})
            except Exception as e:  # verification must never kill the run
                logger.warning("[research-agent] verify failed: %s", e)
                return f, {}

    head = findings[:k]
    checked = await asyncio.gather(*(check(f) for f in head))
    out: list[dict[str, Any]] = []
    for f, v in checked:
        verdict = (v.get("verdict") or "UNCONFIRMED").upper()
        if verdict == "REFUTED":
            corr = (v.get("correction") or "").strip()
            src = (v.get("primary_source") or "").strip()
            if corr and src.startswith("http"):
                out.append({
                    "finding": corr,
                    "detail": f"Corrected during verification: {v.get('note','')}",
                    "sources": [src],
                    "confidence": "high",
                    "verified": "VERIFIED",
                })
            # refuted with no sourced correction → dropped entirely
            continue
        f["verified"] = "VERIFIED" if verdict == "VERIFIED" else "REPORTED"
        src = (v.get("primary_source") or "").strip()
        if src.startswith("http") and src not in (f.get("sources") or []):
            f.setdefault("sources", []).append(src)
        out.append(f)
    out.extend(
        {**f, "verified": "REPORTED"} for f in findings[k:]
    )
    return out


async def run_agent(
    *,
    topic: str,
    focus: str = "",
    depth: str = "standard",
    schema_json: str,
    today: str,
) -> dict[str, Any]:
    """The full research pipeline. Returns the parsed briefing dict
    (same schema as the runner's single-shot path) + agent telemetry
    under "_agent"."""
    per_search = 8 if depth == "deep" else 6
    verify_k = 8 if depth == "deep" else 5

    subs = await _plan(topic, focus, depth)
    logger.info("[research-agent] %d sub-questions for %r", len(subs), topic)

    sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def bounded(sub: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                return await _search_one(sub, today=today, searches=per_search)
            except Exception as e:
                logger.warning("[research-agent] sub-search failed: %s", e)
                return {"findings": [], "not_found": [], "open_questions":
                        [f"sub-search errored: {sub.get('q','')}"]}

    results = await asyncio.gather(*(bounded(s) for s in subs))
    findings, not_found, open_qs = _guard(list(results))
    logger.info(
        "[research-agent] guard: %d findings kept, %d not-found, %d open",
        len(findings), len(not_found), len(open_qs),
    )

    # Thin-result retry: web_search is high-variance run to run — one
    # round found the JSW Indian Open, the next found only a 2021 covid
    # notice. If the guard kept almost nothing, re-run every sub-
    # question ONCE with explicit reformulation pressure before
    # concluding the world is empty.
    if len(findings) < 2 and subs:
        logger.info("[research-agent] thin results — one reformulated retry round")
        retry_subs = [
            {**sub, "q": sub.get("q", "") + " (previous search round found "
             "almost nothing — reformulate aggressively: different phrasings, "
             "entity names, the official/primary source by name)"}
            for sub in subs
        ]
        retry_results = await asyncio.gather(*(bounded(s) for s in retry_subs))
        f2, nf2, oq2 = _guard(list(retry_results))
        findings.extend(f2)
        not_found.extend(nf2)
        open_qs.extend(oq2)
        logger.info("[research-agent] after retry: %d findings", len(findings))
    findings = await _verify_top(findings, today=today, k=verify_k)

    material = json.dumps(findings, indent=1)[:16000]
    raw = await _call(
        prompt=_SYNTH_PROMPT.format(
            topic=topic, focus=focus or "(none)", today=today,
            material=material,
            not_found=json.dumps(not_found, indent=1)[:4000],
            open_qs=json.dumps(open_qs, indent=1)[:3000],
            schema=schema_json,
        ),
        max_tokens=6000,
    )
    parsed = _json_of(raw)
    if parsed is None:
        raise RuntimeError("research-agent synthesis JSON parse failed")

    # Final code gate on the synthesized output too: no sourceless
    # finding survives synthesis either.
    kept = []
    for f in (parsed.get("findings") or []):
        srcs = [s for s in (f.get("sources") or [])
                if isinstance(s, str) and s.startswith("http")]
        if srcs:
            f["sources"] = srcs
            kept.append(f)
    parsed["findings"] = kept
    parsed["_agent"] = {
        "sub_questions": [s.get("q", "") for s in subs],
        "kept_findings": len(kept),
        "not_found": len(not_found),
        "open_questions": open_qs[:12],
        "depth": depth,
    }
    return parsed


# ──────────────────────────────────────────────────────────────────
# claim_check — the content pipeline's truth gate
# ──────────────────────────────────────────────────────────────────

_CLAIM_CHECK_PROMPT = """You are a fact-checking research agent. For the claim below, search the web and deliver a verdict. Be adversarial: hunt for the primary source that settles it either way. Today (IST): {today}.

CLAIM: {claim}
CONTEXT (where it will be published): {context}

STRICT JSON only:
{{"verdict": "<SUPPORTED|UNSUPPORTED|CONTRADICTED>",
"sources": ["<url>"],
"correction": "<if CONTRADICTED: the accurate version, else empty>",
"note": "<1-2 sentences>"}}"""


async def claim_check(
    claims: list[str], *, context: str = "", today: str = "",
) -> list[dict[str, Any]]:
    """Verdict per claim, researched fresh. SUPPORTED requires sources;
    a SUPPORTED verdict with no URL is downgraded to UNSUPPORTED in code."""
    if not claims:
        return []
    sem = asyncio.Semaphore(_MAX_PARALLEL)

    async def one(c: str) -> dict[str, Any]:
        async with sem:
            try:
                raw = await _call(
                    prompt=_CLAIM_CHECK_PROMPT.format(
                        claim=c, context=context or "a public post", today=today,
                    ),
                    max_tokens=1200,
                    searches=4,
                )
                v = _json_of(raw) or {}
            except Exception as e:
                logger.warning("[claim-check] errored on %r: %s", c[:60], e)
                v = {}
            verdict = (v.get("verdict") or "UNSUPPORTED").upper()
            srcs = [s for s in (v.get("sources") or [])
                    if isinstance(s, str) and s.startswith("http")]
            if verdict == "SUPPORTED" and not srcs:
                verdict = "UNSUPPORTED"
            return {
                "claim": c,
                "verdict": verdict,
                "sources": srcs,
                "correction": (v.get("correction") or "").strip(),
                "note": (v.get("note") or "").strip(),
            }

    return list(await asyncio.gather(*(one(c) for c in claims)))
