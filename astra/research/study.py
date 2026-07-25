"""
The STUDY function — "if there is something to be studied, it tells me
what is to be studied."

Budget (Kunal, 2026-07-25): 5 hours/week, free sources only. Split:
one paper/essay (~1h) + one build block (~3h) routed toward what he is
actually shipping + ~10 min/day spaced recall. At this budget the canon
below reaches its target depths in roughly two quarters.

Surfaces: ONE WhatsApp screen on Sunday (the assignment) + ONE short
recall message daily. Everything long-form lives in the app's Research
room (research_briefings kind='study_assignment'). WhatsApp is for
communication, not studying.

Knowledge model: knowledge_state tracks per-domain level L0-L4 with
evidence. Initial levels are PROVISIONAL estimates (marked as such);
they harden as assignments complete and teach-backs land. The largest
gap-to-target domain gets priority, but assignments prefer domains
touching his current shipping work when the gap difference is small.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from astra.db.engine import async_session

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# ── The canon: domains, target depths, free resources ──────────────
# target levels: L0 unaware · L1 aware · L2 conversant · L3 can build /
# defend in a hard meeting · L4 can teach + extend.
CURRICULUM: list[dict[str, Any]] = [
    {
        "domain": "transformer-internals",
        "target": 3,
        "provisional_level": 1,
        "why": "The substrate of everything he runs. L3 = can whiteboard attention, KV cache, and training loop for any investor or researcher.",
        "resources": [
            "Karpathy: Let's build GPT from scratch (video + repo nanoGPT)",
            "Attention Is All You Need (1706.03762) + GPT-2 paper read-through",
            "3-block build: tokenizer → attention → full training run on tiny corpus",
        ],
    },
    {
        "domain": "post-training",
        "target": 3,
        "provisional_level": 1,
        "why": "SFT/DPO/GRPO is where models become products; Sargo's future own-models thesis depends on it.",
        "resources": [
            "Raschka: Build an LLM from Scratch — finetuning chapters",
            "DPO paper (2305.18290); GRPO via TRL quickstart — run ONE real GRPO job on a small model",
            "Interconnects post-training posts (already in spine)",
        ],
    },
    {
        "domain": "agents-and-evals",
        "target": 4,
        "provisional_level": 3,
        "why": "His daily craft (Astra, Shotgun). The gap is EVALS: systematic harnesses, not vibes. L4 = teaches it publicly.",
        "resources": [
            "Anthropic: Building Effective Agents + evals docs",
            "Build: a real eval harness for Astra's drafter (voice-match + claim-gate regression suite)",
            "MCP spec releases (already in spine)",
        ],
    },
    {
        "domain": "video-diffusion",
        "target": 3,
        "provisional_level": 1,
        "why": "Sargo IS this domain. L3 = can explain DiT/flow-matching tradeoffs to any lab researcher and predict where video models go next.",
        "resources": [
            "Flow matching intro (Lipman 2210.02747, guided read)",
            "DiT paper (2212.09748); Wan/HunyuanVideo tech reports (open)",
            "Build: generate with an open video model via fal, inspect the sampling/conditioning knobs Sargo exposes",
        ],
    },
    {
        "domain": "inference-economics",
        "target": 3,
        "provisional_level": 2,
        "why": "Sargo's margins and the raise story both live here. L3 = can defend $/credit against any technical investor cold.",
        "resources": [
            "vLLM PagedAttention paper (2309.06180)",
            "Exercise: rebuild Sargo's blended cost/credit from first principles (tokens, GPU-seconds, batch effects)",
            "SemiAnalysis free posts (already in spine)",
        ],
    },
]

_ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_state (
    domain      TEXT PRIMARY KEY,
    level       INTEGER NOT NULL DEFAULT 0,
    target      INTEGER NOT NULL DEFAULT 3,
    provisional BOOLEAN NOT NULL DEFAULT TRUE,
    evidence    TEXT NOT NULL DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS study_assignments (
    id          BIGSERIAL PRIMARY KEY,
    briefing_id INTEGER,
    domain      TEXT NOT NULL,
    paper       TEXT NOT NULL DEFAULT '',
    build_task  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'assigned',
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS srs_cards (
    id          BIGSERIAL PRIMARY KEY,
    domain      TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    answer      TEXT NOT NULL DEFAULT '',
    due_on      DATE NOT NULL DEFAULT CURRENT_DATE,
    interval_d  INTEGER NOT NULL DEFAULT 1,
    reps        INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def ensure_tables() -> None:
    async with async_session() as s:
        for stmt in _ENSURE_SQL.split(";"):
            if stmt.strip():
                await s.execute(text(stmt))
        # seed knowledge_state from the canon (idempotent)
        for c in CURRICULUM:
            await s.execute(text("""
                INSERT INTO knowledge_state (domain, level, target, provisional, evidence)
                VALUES (:d, :l, :t, TRUE, 'provisional estimate, unassessed')
                ON CONFLICT (domain) DO NOTHING
            """), {"d": c["domain"], "l": c["provisional_level"], "t": c["target"]})
        await s.commit()


_ASSIGN_PROMPT = """You are Astra's study director for Kunal Singh (AI founder: Sargo creative studio, Astra agent OS; ambition: top AI mind; budget: 5 hours this week — one ~1h paper/essay + one ~3h build block).

KNOWLEDGE STATE (level / target per domain):
{state}

CURRICULUM (domains, why, free resources):
{curriculum}

WHAT HE IS SHIPPING RIGHT NOW (route the build task toward this when the gap sizes are close):
{shipping}

LAST 4 ASSIGNMENTS (do not repeat; build progression):
{recent}

THIS WEEK'S NOTABLE INTEL (optional hook — if a spine item makes one domain urgent, say so):
{intel}

Pick ONE domain and compose this week's assignment. STRICT JSON:
{{"domain": "<from curriculum>",
"why_now": "<one line: why this domain this week — tie to gap or shipping>",
"paper": "<the exact paper/essay + what to extract from it, ~1h>",
"build_task": "<the ~3h build block, concrete, tied to his real repos/products where possible>",
"srs_cards": [{{"prompt": "<recall question>", "answer": "<crisp answer>"}}] (exactly 5, testing the WEEK'S material)
}}"""


async def run_weekly_assignment() -> dict[str, Any]:
    """Sunday 18:00 IST — compose + deliver this week's study assignment."""
    from astra.creators._shared import generate_json
    from astra.research.runner import _insert_pending, _mark_ready

    await ensure_tables()
    async with async_session() as s:
        st = (await s.execute(text(
            "SELECT domain, level, target, provisional FROM knowledge_state ORDER BY (target - level) DESC"
        ))).all()
        recent = (await s.execute(text(
            "SELECT domain, paper, status FROM study_assignments ORDER BY id DESC LIMIT 4"
        ))).all()
        intel = (await s.execute(text("""
            SELECT title, url FROM source_items
            WHERE fetched_at > now() - interval '7 days' AND tag IN ('papers','lab','stack')
            ORDER BY tier ASC, fetched_at DESC LIMIT 20
        """))).all()

    shipping = ("Sargo Aug 1 relaunch (credits, marketplace video/image models via fal); "
                "Astra: research spine + intel brief just shipped, engagement E1 in learning window; "
                "voice/content pipeline live with claim gate.")
    parsed = await generate_json(
        system="You are a precise study director. STRICT JSON only.",
        user=_ASSIGN_PROMPT.format(
            state="\n".join(f"- {r[0]}: L{r[1]}/L{r[2]}{' (provisional)' if r[3] else ''}" for r in st),
            curriculum=json.dumps(CURRICULUM, indent=0)[:6000],
            shipping=shipping,
            recent="\n".join(f"- {r[0]}: {r[1][:80]} ({r[2]})" for r in recent) or "(none yet)",
            intel="\n".join(f"- {r[0][:100]} ({r[1]})" for r in intel)[:3000],
        ),
        forbidden=[], text_blob_fn=lambda d: "",
    )
    domain = parsed.get("domain") or CURRICULUM[0]["domain"]
    today = datetime.now(_IST).strftime("%d %b %Y")
    body_md = (
        f"# Study — week of {today}\n\n"
        f"**Domain:** {domain}\n\n**Why now:** {parsed.get('why_now','')}\n\n"
        f"## Paper (~1h)\n{parsed.get('paper','')}\n\n"
        f"## Build (~3h)\n{parsed.get('build_task','')}\n\n"
        f"## Recall cards added\n"
        + "\n".join(f"- {c.get('prompt','')}" for c in (parsed.get('srs_cards') or []))
    )
    briefing_id = await _insert_pending(
        topic=f"Study — {domain} — {today}", kind="study_assignment", business_tags="helmtech",
    )
    await _mark_ready(briefing_id=briefing_id, body_md=body_md,
                      parsed={"gist": parsed.get("why_now", ""), "findings": [],
                              "signals": [], "sources": []},
                      model_used="study_weekly",
                      started_at=datetime.now(timezone.utc))
    async with async_session() as s:
        await s.execute(text("""
            INSERT INTO study_assignments (briefing_id, domain, paper, build_task)
            VALUES (:b, :d, :p, :t)
        """), {"b": briefing_id, "d": domain,
               "p": (parsed.get("paper") or "")[:800],
               "t": (parsed.get("build_task") or "")[:800]})
        for c in (parsed.get("srs_cards") or [])[:5]:
            if isinstance(c, dict) and c.get("prompt"):
                await s.execute(text("""
                    INSERT INTO srs_cards (domain, prompt, answer, due_on)
                    VALUES (:d, :p, :a, CURRENT_DATE + 1)
                """), {"d": domain, "p": c["prompt"][:500], "a": (c.get("answer") or "")[:500]})
        await s.commit()

    app = os.environ.get("ASTRA_WEB_URL", "https://astra.thearrogantclub.com").rstrip("/")
    msg = (f"Study this week — {domain}\n"
           f"{parsed.get('why_now','')}\n"
           f"Paper (~1h) + build (~3h) + daily recall.\n"
           f"Full assignment: {app}/research/{briefing_id}")
    await _notify(msg)
    return {"status": "ready", "id": briefing_id, "domain": domain}


async def run_daily_srs() -> dict[str, Any]:
    """Daily 21:30 IST — up to 4 due recall prompts, ONE message."""
    await ensure_tables()
    async with async_session() as s:
        rows = (await s.execute(text("""
            SELECT id, domain, prompt FROM srs_cards
            WHERE due_on <= CURRENT_DATE ORDER BY due_on ASC LIMIT 4
        """))).all()
        if not rows:
            return {"status": "quiet", "served": 0}
        for r in rows:
            # simple expanding schedule: 1 → 3 → 7 → 16 → 35 days
            await s.execute(text("""
                UPDATE srs_cards
                SET reps = reps + 1,
                    interval_d = LEAST(interval_d * 2 + 1, 35),
                    due_on = CURRENT_DATE + LEAST(interval_d * 2 + 1, 35)
                WHERE id = :i
            """), {"i": r[0]})
        await s.commit()
    msg = "Recall (answer in your head, ~5 min):\n" + "\n".join(
        f"{i}. [{r[1]}] {r[2]}" for i, r in enumerate(rows, 1))
    await _notify(msg[:1500])
    return {"status": "sent", "served": len(rows)}


async def _notify(text_msg: str) -> None:
    import httpx

    base = os.environ.get("GATEWAY_URL", "http://whatsapp.railway.internal:8080").rstrip("/")
    secret = os.environ.get("AGENT_SHARED_SECRET", "").strip()
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{base}/api/v1/notify/owner",
                             json={"text": text_msg},
                             headers={"x-astra-secret": secret})
        if r.status_code != 200:
            logger.error("[study] notify failed: %s", r.status_code)
    except Exception:
        logger.exception("[study] notify errored")
