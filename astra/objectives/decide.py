"""The objective decision function — PURE, no I/O, no LLM.

Every tick's behaviour is decided here from (objective, now, evidence).
Same inputs, same decision. This is deliberately separated from the
effects in engine.py so the escalation ladder — the part that decides
whether Kunal gets pinged — is directly unit-testable, the same way
obligation_engine.materialise() is.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


class Action(str, enum.Enum):
    WAIT = "wait"                 # not yet time
    CLOSE_DONE = "close_done"     # the done condition fired
    DRAFT = "draft"               # produce the next nudge
    DRAFT_FIRM = "draft_firm"     # nudge + tell Kunal it is dragging
    ESCALATE = "escalate"         # out of attempts / past deadline — human call
    ABANDON = "abandon"           # give up cleanly, say so once


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def decide(
    objective: dict,
    *,
    now: datetime,
    satisfied_at: datetime | None = None,
    satisfied_detail: str = "",
) -> Decision:
    """What should this objective do right now?

    `satisfied_at` is the outcome evidence gathered by the caller (a
    reply landed, an obligation was filed). It is passed IN rather than
    fetched here so this stays pure — and so a missing check can never
    be mistaken for a negative result.
    """
    state = objective.get("state", "active")
    if state != "active":
        return Decision(Action.WAIT, f"state={state}")

    # 1. Success beats everything, including a passed deadline.
    if satisfied_at is not None:
        last = objective.get("last_action_at")
        if last is None or satisfied_at >= last:
            return Decision(Action.CLOSE_DONE, satisfied_detail or "done condition satisfied")

    attempts = int(objective.get("attempts") or 0)
    max_attempts = int(objective.get("max_attempts") or 4)
    escalate_after = int(objective.get("escalate_after") or 2)
    deadline = objective.get("deadline_at")

    # 2. A blown deadline is a human decision, not another polite nudge.
    if deadline is not None and now >= deadline:
        return Decision(Action.ESCALATE, "deadline passed without the outcome")

    # 3. Out of attempts: stop. A loop that never stops is a nag, and a
    #    nag gets muted — at which point it protects nothing.
    if attempts >= max_attempts:
        return Decision(Action.ABANDON, f"{attempts} attempts made, no outcome")

    # 4. Not yet time.
    nxt = objective.get("next_check_at")
    if nxt is not None and now < nxt:
        return Decision(Action.WAIT, f"next check {nxt.isoformat()}")

    # 5. Chase — firmly once it has been dragging.
    if attempts >= escalate_after:
        return Decision(Action.DRAFT_FIRM, f"attempt {attempts + 1}, dragging")
    return Decision(Action.DRAFT, f"attempt {attempts + 1}")


def next_check(objective: dict, *, now: datetime) -> datetime:
    """When to look again. Backs off as attempts accumulate so a slow
    counterparty is not chased at the same tempo forever."""
    cadence = max(1, int(objective.get("cadence_days") or 3))
    attempts = int(objective.get("attempts") or 0)
    factor = 1 + min(attempts, 3) * 0.5      # 1x, 1.5x, 2x, 2.5x
    return now + timedelta(days=cadence * factor)


def summarise(decisions: list[tuple[dict, Decision]]) -> str:
    """ONE batched message for the whole tick. Never one ping per
    objective — the notification budget is what keeps alerting alive."""
    acted = [(o, d) for o, d in decisions
             if d.action in (Action.DRAFT, Action.DRAFT_FIRM, Action.ESCALATE,
                             Action.ABANDON, Action.CLOSE_DONE)]
    if not acted:
        return ""
    lines: list[str] = []
    done = [o for o, d in acted if d.action == Action.CLOSE_DONE]
    esc = [(o, d) for o, d in acted if d.action == Action.ESCALATE]
    aband = [o for o, d in acted if d.action == Action.ABANDON]
    drafts = [(o, d) for o, d in acted
              if d.action in (Action.DRAFT, Action.DRAFT_FIRM)]

    if esc:
        lines.append("Needs you:")
        for o, d in esc:
            lines.append(f"  #{o['id']} {o['title']} — {d.reason}")
    if drafts:
        lines.append("Drafted a nudge:" if not esc else "\nDrafted a nudge:")
        for o, d in drafts:
            firm = " (firm)" if d.action == Action.DRAFT_FIRM else ""
            lines.append(f"  #{o['id']} {o['title']}{firm}")
    if done:
        lines.append("\nClosed (they responded):")
        for o in done:
            lines.append(f"  #{o['id']} {o['title']}")
    if aband:
        lines.append("\nGiving up (out of attempts):")
        for o in aband:
            lines.append(f"  #{o['id']} {o['title']}")
    return "\n".join(lines)
