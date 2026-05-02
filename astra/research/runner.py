"""
Research Intel runner.

Pipeline per research briefing:

  1. Stage a `research_briefings` row with status='pending'.
  2. Gather compass + Astra self-state (`context.gather_*`).
  3. Build the Claude prompt — compass + state + topic focus + the
     "what to build / what to subtract" output schema.
  4. Invoke Claude with WebSearch/WebFetch tool access via Agent SDK
     or direct Messages API (depending on which is cleaner for this
     path — we use Messages with web_search tool since the sub-agent
     lives inside the briefing process, not a separate conversation).
  5. Parse the structured JSON payload, persist body/findings/
     signals/actions/sources onto the row.
  6. Also write a summary memory + stage high-priority action items
     as tasks.

Output schema Claude must return (enforced by prompt + JSON parse):

  {
    "gist": "string",
    "findings": [{"title", "detail", "confidence", "sources":[url,...]}],
    "signals": [{"pattern", "significance", "timeframe"}],
    "build_recommendations": [{
        "what", "why", "priority", "estimated_lift", "blocks":[...]
    }],
    "subtract_recommendations": [{
        "what", "why", "risk", "confidence"
    }],
    "urgencies": [{"what", "by_when":"YYYY-MM-DD", "rationale"}],
    "action_items": [{"title", "owner", "priority", "due":"YYYY-MM-DD|null"}],
    "sources": [{"url", "description"}]
  }

Nothing in this module touches the Apple Note / calendar / email —
it only writes into Astra's own stores. Briefings surface via the
`/research` UI and via the morning briefing's top-line fold-in.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────


async def run_scheduled_daily() -> dict[str, Any]:
    """07:00 IST scheduler hook — runs today's rotating topic."""
    from astra.research.topics import daily_topic

    topic = daily_topic()
    return await run_research(
        topic=topic.title,
        topic_slug=topic.slug,
        prompt_focus=topic.prompt_focus,
        business_tags=topic.business_tags,
        kind="scheduled",
        depth=topic.depth,
    )


async def run_topic_on_demand(
    topic: str,
    *,
    prompt_focus: str | None = None,
    business_tags: str = "",
    depth: str = "standard",
) -> dict[str, Any]:
    """MCP tool entry — Kunal asked Astra to research X."""
    return await run_research(
        topic=topic,
        topic_slug="on_demand",
        prompt_focus=prompt_focus or (
            f"Research the topic: {topic}. Prioritize what Kunal can act "
            "on this week given the compass. Distinguish fact from signal. "
            "Call out things worth adding to Astra's roadmap and things "
            "worth subtracting."
        ),
        business_tags=business_tags,
        kind="on_demand",
        depth=depth,
    )


# ──────────────────────────────────────────────────────────────────
# Core
# ──────────────────────────────────────────────────────────────────


async def run_research(
    *,
    topic: str,
    topic_slug: str,
    prompt_focus: str,
    business_tags: str,
    kind: str,
    depth: str = "standard",
) -> dict[str, Any]:
    """Full pipeline. Returns a dict with id + summary fields."""
    from astra.research.context import gather_astra_state, gather_compass

    briefing_id = await _insert_pending(
        topic=topic,
        kind=kind,
        business_tags=business_tags,
    )
    t0 = datetime.now(timezone.utc)

    try:
        compass = gather_compass()
        state = await gather_astra_state()

        prompt = _build_prompt(
            topic=topic,
            prompt_focus=prompt_focus,
            depth=depth,
            compass_block=compass.render_for_prompt(),
            state=state,
        )

        model_used, raw_json_text = await _invoke_claude(
            prompt=prompt, depth=depth,
        )

        parsed = _safe_json(raw_json_text)
        if parsed is None:
            await _mark_error(
                briefing_id,
                f"JSON parse failed; raw head: {raw_json_text[:400]!r}",
                model_used,
                t0,
            )
            return {"status": "error", "id": briefing_id,
                    "error": "parse_failed"}

        body_md = _compose_markdown(topic=topic, parsed=parsed)

        # Persist everything
        await _mark_ready(
            briefing_id=briefing_id,
            body_md=body_md,
            parsed=parsed,
            model_used=model_used,
            started_at=t0,
        )

        # Memory + tasks
        memory_id = await _file_memory(
            topic=topic, body_md=body_md,
            tags_csv=f"research,{business_tags},{topic_slug}".strip(","),
        )
        task_ids = await _stage_tasks(
            briefing_id=briefing_id,
            topic=topic,
            action_items=parsed.get("action_items") or [],
        )
        await _link_side_records(
            briefing_id=briefing_id,
            memory_id=memory_id,
            task_ids=task_ids,
        )

        return {
            "status": "ready",
            "id": briefing_id,
            "topic": topic,
            "gist": parsed.get("gist", ""),
            "build_recs": len(parsed.get("build_recommendations") or []),
            "subtract_recs": len(parsed.get("subtract_recommendations") or []),
            "action_items": len(parsed.get("action_items") or []),
            "task_ids": task_ids,
            "duration_ms": int(
                (datetime.now(timezone.utc) - t0).total_seconds() * 1000
            ),
        }
    except Exception as e:
        logger.exception("[research] runner crashed on briefing %s", briefing_id)
        await _mark_error(briefing_id, str(e)[:900], "", t0)
        return {"status": "error", "id": briefing_id, "error": str(e)[:300]}


# ──────────────────────────────────────────────────────────────────
# Prompt construction
# ──────────────────────────────────────────────────────────────────


def _build_prompt(
    *,
    topic: str,
    prompt_focus: str,
    depth: str,
    compass_block: str,
    state: dict[str, Any],
) -> str:
    """Build the Claude prompt.

    The prompt is deliberately long and specific. Research Intel is
    the most important agent in the fleet — the one that tells us
    what to build and subtract — so we give it the best possible
    framing and the strictest output contract.
    """
    depth_note = {
        "standard": "Aim for 6-10 findings, 3-6 build recs, 2-5 subtract recs.",
        "deep": "This is a DEEP review. Aim for 10-18 findings, 5-10 build "
                "recs, 4-8 subtract recs. Be ruthless about subtractions.",
    }.get(depth, "Aim for 6-10 findings.")

    now_ist = state.get("now_ist", "")

    return f"""You are Research Intel — the single most important agent in Astra's fleet. Kunal relies on you to tell him (a) what the world around his four businesses looks like right now, and (b) what Astra itself should build or subtract next to keep advancing his compass.

You are compass-aware: the <compass> block below is Kunal's north star. Every finding you surface, every recommendation you make, must measure against it.

You are self-aware: the <astra_state> block is Astra's current internals — what services run, what's pending, what got built recently, what's stalled. Use it to spot dead weight to subtract and gaps to fill.

TODAY (IST): {now_ist}
TOPIC: {topic}
FOCUS: {prompt_focus}

{depth_note}

────────────────────────────────────────────────────────────────────
Output — STRICT JSON only. No prose, no code fences, no preamble.
────────────────────────────────────────────────────────────────────

{{
  "gist": "<2-3 sentences — the headline of today's briefing, written as if Kunal only reads one line>",

  "findings": [
    {{
      "title": "<short headline>",
      "detail": "<2-4 sentences explaining the finding and why it matters to Kunal's compass>",
      "confidence": "<high|medium|low>",
      "sources": ["<url1>", "<url2>"]
    }}
  ],

  "signals": [
    {{
      "pattern": "<what pattern is emerging>",
      "significance": "<why Kunal should care>",
      "timeframe": "<how soon this matters — days/weeks/quarter>"
    }}
  ],

  "build_recommendations": [
    {{
      "what": "<concrete thing to build into Astra, or ship in HelmTech / Apex / BAY / Top Studios>",
      "why": "<which compass vector it advances, with specific metric (e.g. 'unblocks $2M raise', 'accelerates Nov 2026 Nationals prep')>",
      "priority": <1|2|3>,   // 1=low, 2=normal, 3=high
      "estimated_lift": "<hours / days / weeks>",
      "blocks": ["<specific future work that becomes possible once this is built>"]
    }}
  ],

  "subtract_recommendations": [
    {{
      "what": "<specific feature / code path / habit / commitment to cut>",
      "why": "<why it's dead weight — unused, misaligned with compass, eats attention>",
      "risk": "<what we lose if we cut it — be honest>",
      "confidence": "<high|medium|low>"
    }}
  ],

  "urgencies": [
    {{
      "what": "<the thing that's time-sensitive>",
      "by_when": "<YYYY-MM-DD>",
      "rationale": "<why this date is real, not arbitrary>"
    }}
  ],

  "action_items": [
    {{
      "title": "<imperative, e.g. 'Send revised pitch deck to Ankur with v2 traction slide'>",
      "owner": "<kunal|astra|chinmay|named-person|unknown>",
      "priority": <1|2|3>,
      "due": "<YYYY-MM-DD or null>"
    }}
  ],

  "sources": [
    {{ "url": "<url>", "description": "<short>" }}
  ]
}}

────────────────────────────────────────────────────────────────────
Rules
────────────────────────────────────────────────────────────────────

- Budget your length. Keep the whole JSON under ~12000 characters so you don't get truncated mid-array. Prioritize quality over quantity in each section.
- JSON strings must escape internal double quotes with \\" — write e.g. "the \\"Kunal\\" note" not "the "Kunal" note".
- Be concrete. "Improve monitoring" is useless; "Add a 5-min interval job that alerts when a scheduled catchup prompt fails to fire" is useful.
- Distinguish facts (confirmed, dated sources) from signals (patterns without confirmation). Don't mix them.
- Never fabricate URLs. If you don't have a source, say so in `detail` and drop the URL rather than invent one.
- For `subtract_recommendations` — this is the rarest kind of advice and the most valuable. Look for: tables/jobs/UI that haven't been touched in weeks, features with zero usage signal, dormant agent definitions, half-built flows that were superseded, complexity that outlived its purpose.
- For compass-tie: prefer specifics from the compass over generics. E.g., "advances HelmTech $2M pre-seed" beats "advances fundraising".
- `by_when` for urgencies and `due` for action items MUST be real YYYY-MM-DD relative to today's IST date (above).
- Priority 3 = ships-this-week-or-it-matters; 2 = next 2-4 weeks; 1 = whenever.
- If the signal is genuinely thin ("no meaningful intel this day"), say so in `gist` and keep arrays short — don't pad.
- Write `gist` in Kunal's register: short declarative sentences, no filler, no exclamation marks, italic-serif voice.

<compass>
{compass_block}
</compass>

<astra_state>
{json.dumps(state, indent=2, default=str)[:14000]}
</astra_state>

Return the JSON now."""


# ──────────────────────────────────────────────────────────────────
# Claude invocation
# ──────────────────────────────────────────────────────────────────


async def _invoke_claude(
    *, prompt: str, depth: str,
) -> tuple[str, str]:
    """Call Claude Messages with the web_search tool enabled.

    Returns (model_name, raw_text). We use Sonnet by default; Opus is
    a future path for the Saturday meta-review. Web search is Claude's
    own server-side tool — no MCP plumbing needed here.
    """
    import anthropic

    from astra.config import settings

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not available")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    # Choose model by depth. Sonnet is usually right; Opus for
    # ruthless Saturday audits once we need the extra muscle.
    model = settings.model_sonnet if depth != "deep" else settings.model_sonnet
    # 8000 tokens is ~32K chars of output — enough headroom for the
    # deep Saturday audit. Standard keeps tighter so daily briefings
    # stay short.
    max_tokens = 8000 if depth == "deep" else 4000

    # Anthropic's server-side web_search tool lets Claude fetch pages
    # without us plumbing MCP. We only invoke it when we actually want
    # Claude to look things up. Available since 2025-05.
    tools: list[dict] = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 10 if depth == "deep" else 6},
    ]

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        tools=tools,
        messages=[{"role": "user", "content": prompt}],
    )

    # Walk content blocks — tool_use and server_tool_use are already
    # consumed by Anthropic; we just need the final text.
    text_parts: list[str] = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
        elif hasattr(block, "text"):
            text_parts.append(block.text)
    return model, "\n".join(text_parts).strip()


def _get_api_key() -> str:
    from astra.config import settings

    key = settings.anthropic_api_key or os.environ.get(
        "ANTHROPIC_API_KEY", ""
    )
    if key:
        return key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ──────────────────────────────────────────────────────────────────
# JSON parse + markdown render
# ──────────────────────────────────────────────────────────────────


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL)


def _safe_json(text: str) -> dict[str, Any] | None:
    """Tolerant JSON parse.

    1. Strip ```json fences.
    2. Clip to outermost { … } block.
    3. If that fails (Claude hit max_tokens mid-array), progressively
       back off from the last `}` looking for a position where the
       parse succeeds.
    """
    s = text.strip()
    if s.startswith("```"):
        s = _FENCE_RE.sub("", s).strip()
    first = s.find("{")
    last = s.rfind("}")
    if first < 0 or last <= first:
        return None

    # Happy path — complete, valid JSON
    try:
        return json.loads(s[first : last + 1])
    except Exception as e:
        logger.warning("[research] primary JSON parse failed: %s", e)

    # Recovery — Claude may have been truncated mid-array. Walk backwards
    # from the end looking for a `]` followed by `,` or `}` that could be
    # a clean close. We try closing each open array with `]` and the top
    # object with `}` up to 50 times before giving up.
    body = s[first:]
    for _trim in range(1, 60):
        # Try progressively shorter bodies and close them.
        head = body[: len(body) - _trim]
        # Count un-matched brackets so we know what to append.
        opens = head.count("{") - head.count("}")
        opens_sq = head.count("[") - head.count("]")
        if opens <= 0 and opens_sq <= 0:
            continue
        candidate = head.rstrip().rstrip(",")
        candidate += "]" * max(0, opens_sq) + "}" * max(0, opens)
        try:
            parsed = json.loads(candidate)
            logger.info(
                "[research] recovered JSON by trimming %d chars + appending closers",
                _trim,
            )
            return parsed
        except Exception:
            continue

    return None


def _compose_markdown(*, topic: str, parsed: dict[str, Any]) -> str:
    """Render the structured briefing as a single markdown blob.

    Stable shape so the morning-briefing integration can cherry-pick
    the gist + top urgencies + top build/subtract recs programmatically.
    """
    lines: list[str] = [f"# {topic}", ""]
    gist = parsed.get("gist", "").strip()
    if gist:
        lines.append(f"**Gist.** {gist}")
        lines.append("")

    findings = parsed.get("findings") or []
    if findings:
        lines.append("## Findings")
        for f in findings:
            lines.append(f"- **{f.get('title','')}** _({f.get('confidence','?')})_ — {f.get('detail','')}")
            srcs = f.get("sources") or []
            if srcs:
                lines.append(f"  _sources: {', '.join(srcs)}_")
        lines.append("")

    signals = parsed.get("signals") or []
    if signals:
        lines.append("## Signals")
        for s in signals:
            lines.append(
                f"- _{s.get('timeframe','?')}_ — {s.get('pattern','')} → {s.get('significance','')}"
            )
        lines.append("")

    builds = parsed.get("build_recommendations") or []
    if builds:
        lines.append("## Build")
        for b in builds:
            lines.append(
                f"- **[p{b.get('priority','?')}] {b.get('what','')}** "
                f"({b.get('estimated_lift','?')}) — {b.get('why','')}"
            )
            blocks = b.get("blocks") or []
            if blocks:
                lines.append(f"  _unblocks: {'; '.join(blocks)}_")
        lines.append("")

    subs = parsed.get("subtract_recommendations") or []
    if subs:
        lines.append("## Subtract")
        for s in subs:
            lines.append(
                f"- **{s.get('what','')}** _({s.get('confidence','?')})_ — {s.get('why','')}"
            )
            risk = s.get("risk", "")
            if risk:
                lines.append(f"  _risk of cutting: {risk}_")
        lines.append("")

    urg = parsed.get("urgencies") or []
    if urg:
        lines.append("## Urgent")
        for u in urg:
            lines.append(
                f"- **by {u.get('by_when','?')}** — {u.get('what','')} ({u.get('rationale','')})"
            )
        lines.append("")

    actions = parsed.get("action_items") or []
    if actions:
        lines.append("## Action items")
        for a in actions:
            bits = [f"[p{a.get('priority','?')}]", a.get("title", "")]
            owner = a.get("owner")
            if owner and owner not in ("unknown", ""):
                bits.append(f"_({owner})_")
            if a.get("due"):
                bits.append(f"· due {a['due']}")
            lines.append("- " + " ".join(bits))
        lines.append("")

    sources = parsed.get("sources") or []
    if sources:
        lines.append("## Sources")
        for src in sources:
            lines.append(f"- [{src.get('description','')}]({src.get('url','')})")

    return "\n".join(lines).strip()


# ──────────────────────────────────────────────────────────────────
# DB writes
# ──────────────────────────────────────────────────────────────────


async def _insert_pending(
    *, topic: str, kind: str, business_tags: str,
) -> int:
    async with async_session() as s:
        r = await s.execute(
            text(
                """
                INSERT INTO research_briefings (topic, kind, business_tags, status)
                VALUES (:t, :k, :b, 'pending')
                RETURNING id
                """
            ),
            {"t": topic[:511], "k": kind[:15], "b": business_tags[:255]},
        )
        rid = int(r.scalar_one())
        await s.commit()
    return rid


async def _mark_error(
    briefing_id: int, err: str, model_used: str, started_at: datetime,
) -> None:
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE research_briefings
                SET status='error',
                    error=:e,
                    model_used=:m,
                    duration_ms=:d,
                    completed_at=now()
                WHERE id=:id
                """
            ),
            {
                "id": briefing_id,
                "e": err,
                "m": model_used[:63],
                "d": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
            },
        )
        await s.commit()


async def _mark_ready(
    *,
    briefing_id: int,
    body_md: str,
    parsed: dict[str, Any],
    model_used: str,
    started_at: datetime,
) -> None:
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE research_briefings
                SET status='ready',
                    body_md=:body,
                    signals=CAST(:signals AS JSONB),
                    action_items=CAST(:actions AS JSONB),
                    sources=CAST(:sources AS JSONB),
                    model_used=:m,
                    duration_ms=:d,
                    completed_at=now(),
                    error=NULL
                WHERE id=:id
                """
            ),
            {
                "id": briefing_id,
                "body": body_md,
                "signals": json.dumps(parsed.get("signals") or []),
                "actions": json.dumps(parsed.get("action_items") or []),
                "sources": json.dumps(parsed.get("sources") or []),
                "m": model_used[:63],
                "d": int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000),
            },
        )
        await s.commit()


async def _file_memory(*, topic: str, body_md: str, tags_csv: str) -> int | None:
    """Persist the briefing as an episodic memory so it shows up in
    future recalls. Returns memory id or None on failure."""
    try:
        from astra.memory.models import MemoryType
        from astra.memory.store import store_memory

        async with async_session() as s:
            mem = await store_memory(
                session=s,
                content=f"Research briefing — {topic}\n\n{body_md[:4000]}",
                memory_type=MemoryType.EPISODIC,
                source="research",
                tags=tags_csv[:255],
                importance=0.6,
            )
            await s.commit()
            return int(getattr(mem, "id", 0)) or None
    except Exception as e:
        logger.warning("[research] file_memory failed: %s", e)
        return None


async def _stage_tasks(
    *, briefing_id: int, topic: str, action_items: list[dict],
) -> list[int]:
    """Stage high-priority action items as tasks. p1 items are ignored
    — too noisy. p2 and p3 become real tasks."""
    if not action_items:
        return []
    ids: list[int] = []
    async with async_session() as s:
        for a in action_items:
            try:
                prio = int(a.get("priority") or 2)
            except Exception:
                prio = 2
            if prio < 2:
                continue
            title = (a.get("title") or "").strip()
            if not title:
                continue
            owner = a.get("owner") or ""
            note_bits = [f"From research briefing #{briefing_id}: {topic[:80]}"]
            if owner and owner not in ("unknown", ""):
                note_bits.append(f"owner: {owner}")
            due = a.get("due")
            r = await s.execute(
                text(
                    """
                    INSERT INTO tasks
                      (title, note, status, priority, tags, source, due_at)
                    VALUES
                      (:t, :n, 'open', :p, :tg, :src,
                       CASE WHEN :due = '' OR :due IS NULL THEN NULL
                            ELSE CAST(:due AS TIMESTAMPTZ) END)
                    RETURNING id
                    """
                ),
                {
                    "t": title[:511],
                    "n": " · ".join(note_bits),
                    "p": prio,
                    "tg": "research",
                    "src": f"research:{briefing_id}",
                    "due": (due + "T12:00:00+05:30") if due else None,
                },
            )
            ids.append(int(r.scalar_one()))
        await s.commit()
    return ids


async def _link_side_records(
    *, briefing_id: int, memory_id: int | None, task_ids: list[int],
) -> None:
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE research_briefings
                SET memory_id = :m,
                    task_ids = CAST(:t AS JSONB)
                WHERE id = :id
                """
            ),
            {
                "id": briefing_id,
                "m": memory_id,
                "t": json.dumps(task_ids),
            },
        )
        await s.commit()
