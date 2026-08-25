"""The objective tick — gathers evidence, applies decide(), acts once.

Split deliberately: decide.py holds the pure ladder, this holds the I/O.
The one invariant that matters here is that a FAILED EVIDENCE LOOKUP is
never treated as a negative result. If the email agent is unreachable we
do not know whether Samarth replied, and nudging him on that basis is
exactly how an assistant loses trust.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from astra.objectives import store
from astra.objectives.decide import Action, decide, next_check, summarise

logger = logging.getLogger(__name__)


async def _evidence(obj: dict) -> tuple[datetime | None, str, bool]:
    """(satisfied_at, detail, checkable).

    `checkable=False` means we could not determine the outcome — the
    caller must WAIT rather than escalate. Unknown is not 'no'.
    """
    dw = obj.get("done_when") or {}
    kind = dw.get("kind", "manual")

    if kind == "manual":
        return None, "", True          # only Kunal closes it; absence is real

    if kind == "email_reply":
        sender = (dw.get("from") or obj.get("target_ref") or "").strip()
        if not sender:
            return None, "", True
        since = obj.get("last_action_at") or obj.get("created_at")
        base = os.environ.get("EMAIL_AGENT_URL", "http://email.railway.internal:8080").rstrip("/")
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{base}/api/v1/messages/reply-check",
                    params={"sender": sender, "since": (since or datetime.now(timezone.utc)).isoformat()},
                    headers={"x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()},
                )
            if r.status_code != 200:
                logger.warning("[objectives] reply-check HTTP %s — treating as UNKNOWN", r.status_code)
                return None, "", False
            data = r.json()
            if data.get("found"):
                at = datetime.fromisoformat(data["at"])
                return at, f"reply received: {data.get('subject','')[:80]}", True
            return None, "", True
        except Exception as e:
            logger.warning("[objectives] reply-check failed (%s) — treating as UNKNOWN", e)
            return None, "", False

    if kind == "obligation_filed":
        return None, "", True          # wired when the finance tools land

    return None, "", True


async def _draft_nudge(obj: dict, *, firm: bool) -> str:
    """The nudge text, in Kunal's voice. DRAFT ONLY — Astra never sends
    from his personal accounts and never claims to have."""
    try:
        from astra.creators._shared import generate_json

        tone = ("This one has been chasing for a while. Be direct and put a "
                "specific ask with a date in it. Still warm, never passive-aggressive."
                if firm else
                "First or second nudge. Short, light, easy to reply to.")
        out = await generate_json(
            system=(
                "You draft short follow-up messages AS Kunal Singh. His register: "
                "opens mid-thought with no greeting, no sign-off, no hedging, no "
                "em-dashes, never starts a sentence with 'And'. Terse and direct. "
                "STRICT JSON only."
            ),
            user=(
                f"CONTEXT: {obj.get('goal') or obj.get('title')}\n"
                f"CHANNEL: {obj.get('channel')}\n"
                f"PERSON: {obj.get('target_ref')}\n"
                f"ATTEMPT: {int(obj.get('attempts') or 0) + 1}\n"
                f"TONE: {tone}\n\n"
                'Return {"message": "<the follow-up, 1-3 sentences>"}'
            ),
            forbidden=[], text_blob_fn=lambda d: str(d.get("message", "")),
        )
        return (out.get("message") or "").strip()
    except Exception as e:
        logger.warning("[objectives] nudge draft failed: %s", e)
        return ""


async def tick() -> dict[str, Any]:
    """One pass over every active objective. Batched notification."""
    await store.ensure_tables()
    now = datetime.now(timezone.utc)
    objs = await store.due_objectives(limit=50)
    if not objs:
        return {"status": "quiet", "checked": 0}

    decisions: list[tuple[dict, Any]] = []
    for obj in objs:
        satisfied_at, detail, checkable = await _evidence(obj)
        if not checkable:
            # Could not determine the outcome. Do NOT chase on a guess.
            await store.record_event(obj["id"], "skipped",
                                     "outcome unknown (evidence source unreachable)")
            continue

        d = decide(obj, now=now, satisfied_at=satisfied_at, satisfied_detail=detail)

        if d.action is Action.CLOSE_DONE:
            await store.close_objective(obj["id"], state="done", reason=d.reason)
            await store.record_event(obj["id"], "closed", d.reason)
        elif d.action is Action.ABANDON:
            await store.close_objective(obj["id"], state="abandoned", reason=d.reason)
            await store.record_event(obj["id"], "abandoned", d.reason)
        elif d.action is Action.ESCALATE:
            await store.close_objective(obj["id"], state="paused", reason=d.reason)
            await store.record_event(obj["id"], "escalated", d.reason)
        elif d.action in (Action.DRAFT, Action.DRAFT_FIRM):
            msg = await _draft_nudge(obj, firm=d.action is Action.DRAFT_FIRM)
            attempts = int(obj.get("attempts") or 0) + 1
            await store.record_event(obj["id"], "drafted", msg or "(draft failed)")
            await store.advance(
                obj["id"], attempts=attempts,
                next_check_at=next_check({**obj, "attempts": attempts}, now=now),
                last_action_at=now,
            )
            obj["_draft"] = msg
        decisions.append((obj, d))

    body = summarise(decisions)
    if body:
        await _notify(body, decisions)
    return {
        "status": "ok", "checked": len(objs),
        "acted": sum(1 for _, d in decisions if d.action is not Action.WAIT),
    }


async def _notify(body: str, decisions: list[tuple[dict, Any]]) -> None:
    """ONE message per tick. Drafts are carried inline — a pointer to a
    queue he never opens is how the last review loop died."""
    import httpx

    lines = ["Objectives", body]
    drafts = [(o, d) for o, d in decisions
              if d.action in (Action.DRAFT, Action.DRAFT_FIRM) and o.get("_draft")]
    if drafts:
        lines.append("\nDrafts (yours to send, Astra will not):")
        for o, _ in drafts:
            lines.append(f'\n#{o["id"]} -> {o.get("target_ref") or "?"}\n"{o["_draft"]}"')
    text_msg = "\n".join(lines)[:3500]

    base = os.environ.get("GATEWAY_URL", "http://whatsapp.railway.internal:8080").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{base}/api/v1/notify/owner", json={"text": text_msg},
                             headers={"x-astra-secret": os.environ.get("AGENT_SHARED_SECRET", "").strip()})
        if r.status_code != 200:
            logger.error("[objectives] notify failed: %s %s", r.status_code, r.text[:200])
    except Exception:
        logger.exception("[objectives] notify errored")
