"""
Layer 4 self-modification: proactive self-improvement.

Where Layers 1+2 are reactive (Astra edits when asked), Layer 4 is
proactive: Astra notices its own failures and proposes fixes without
being asked. The components:

1. **Observations.** When the existing systems detect something
   sub-optimal — forbidden phrases that survived the regeneration
   loop, critiques scoring below a threshold, user feedback marking
   an artifact as wrong — they call `observe()`. Each observation
   becomes a row in self_improvements (status=observed).

2. **Queue review.** `list_observations()` surfaces the queue. The
   founder (or a scheduled scan) pulls observed-status rows and
   chooses which to act on.

3. **Proposals.** `propose_fix()` reads an observation and uses
   Claude to generate a concrete proposal — text describing what to
   do PLUS a structured list of tool calls (kit-editor or code-editor
   tool calls) that would implement the fix. Status moves to
   'proposed'. The proposal is NOT auto-applied.

4. **Application.** `apply_fix()` executes the proposed tool calls.
   Code-editor tools auto-run the test gate; kit-editor tools commit
   directly. Status moves to 'applied' with the resulting commit hash.

5. **Dismissal.** `dismiss()` moves a row to 'dismissed' with a
   reason — the founder's record of "we considered this and decided
   no". Same row never gets re-proposed (the dedup check uses
   observation text + business + artifact id).

The architecture deliberately keeps observation, proposal, and
application as SEPARATE steps. Astra doesn't auto-fix — every
proposal goes through human review at the apply step. Layer 4 is
about *attention direction* (what should I pay attention to?), not
about autonomous mutation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from astra.creators._shared import (
    DRAFT_MODEL,
    generate_json,
    join_text_fields,
)
from astra.creators.kits import load_kit
from astra.db.engine import async_session

logger = logging.getLogger(__name__)


# ── Observation sources ─────────────────────────────────────────────


VALID_SOURCES = (
    "forbidden_phrase_persisted",  # generate_json retry exhausted
    "low_critique_score",          # critique returned < threshold
    "user_feedback",               # explicit user mark
    "test_failure",                # creator tests failing post-deploy
    "render_failure",              # PDF/PPTX/zip render errored
    "manual",                      # observe() called directly
)
VALID_SEVERITIES = ("low", "medium", "high")
VALID_STATUSES = (
    "observed", "proposed", "approved", "applied", "dismissed",
)


# ── Observation API (cheap; called from hooks) ──────────────────────


async def observe(
    *,
    source: str,
    observation: str,
    business_slug: str | None = None,
    artifact_id: int | None = None,
    severity: str = "medium",
    dedup: bool = True,
) -> dict[str, Any]:
    """Log an observation about something that needs attention.

    Args:
      source: one of VALID_SOURCES
      observation: free-text description
      business_slug, artifact_id: optional context
      severity: low | medium | high (for prioritization)
      dedup: if True (default), skip insert when an observed/proposed
        row exists with the same source + business_slug + artifact_id
        + observation. Prevents the same failure from spamming the
        queue across many runs.

    Returns: {id, status, deduped} where deduped=True means we
    found an existing matching row.
    """
    if source not in VALID_SOURCES:
        raise ValueError(
            f"source must be one of {VALID_SOURCES}, got {source!r}"
        )
    if severity not in VALID_SEVERITIES:
        raise ValueError(
            f"severity must be one of {VALID_SEVERITIES}, got {severity!r}"
        )
    if not observation or not observation.strip():
        raise ValueError("observation cannot be empty")

    async with async_session() as s:
        # Dedup check — same source + business + artifact + observation,
        # still active (observed or proposed)
        if dedup:
            r = await s.execute(
                text(
                    """
                    SELECT id FROM self_improvements
                    WHERE source = :src
                      AND COALESCE(business_slug, '') = COALESCE(:bs, '')
                      AND COALESCE(artifact_id, 0) = COALESCE(:aid, 0)
                      AND observation = :obs
                      AND status IN ('observed', 'proposed', 'approved')
                    LIMIT 1
                    """
                ),
                {
                    "src": source,
                    "bs": business_slug,
                    "aid": artifact_id,
                    "obs": observation,
                },
            )
            existing = r.first()
            if existing:
                return {
                    "id": int(existing[0]),
                    "status": "deduped",
                    "deduped": True,
                }

        # Insert
        r = await s.execute(
            text(
                """
                INSERT INTO self_improvements
                  (source, business_slug, artifact_id, observation, severity)
                VALUES (:src, :bs, :aid, :obs, :sev)
                RETURNING id
                """
            ),
            {
                "src": source,
                "bs": business_slug,
                "aid": artifact_id,
                "obs": observation[:4000],
                "sev": severity,
            },
        )
        new_id = int(r.scalar() or 0)
        await s.commit()
    return {"id": new_id, "status": "observed", "deduped": False}


async def list_observations(
    *,
    status: str | None = None,
    business_slug: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List observations, optionally filtered by status / business / severity.

    Default returns ACTIVE rows (observed or proposed) — that's the
    queue-review use case. Pass status='applied' or 'dismissed' for
    historical queries.

    Returned rows are newest-first.
    """
    where: list[str] = []
    params: dict[str, Any] = {"lim": max(1, min(200, limit))}
    if status is None:
        where.append("status IN ('observed', 'proposed', 'approved')")
    elif status == "all":
        pass
    else:
        where.append("status = :st")
        params["st"] = status
    if business_slug:
        where.append("business_slug = :bs")
        params["bs"] = business_slug
    if severity:
        where.append("severity = :sev")
        params["sev"] = severity
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    async with async_session() as s:
        r = await s.execute(
            text(
                f"""
                SELECT id, source, business_slug, artifact_id, observation,
                       severity, status, proposed_action, proposed_tool_calls,
                       applied_commit, dismissed_reason,
                       observed_at, resolved_at
                FROM self_improvements
                {clause}
                ORDER BY observed_at DESC
                LIMIT :lim
                """
            ),
            params,
        )
        rows = r.all()
    return [
        {
            "id": row[0],
            "source": row[1],
            "business_slug": row[2],
            "artifact_id": row[3],
            "observation": row[4],
            "severity": row[5],
            "status": row[6],
            "proposed_action": row[7],
            "proposed_tool_calls": row[8] or [],
            "applied_commit": row[9],
            "dismissed_reason": row[10],
            "observed_at": row[11].isoformat() if row[11] else None,
            "resolved_at": row[12].isoformat() if row[12] else None,
        }
        for row in rows
    ]


async def get_observation(observation_id: int) -> dict[str, Any] | None:
    rows = await list_observations(status="all", limit=200)
    for row in rows:
        if row["id"] == int(observation_id):
            return row
    return None


# ── Proposal generation (LLM-driven) ────────────────────────────────


_PROPOSAL_SYSTEM = """You are Astra's self-improvement sub-agent.

You receive an observation about something that went wrong (a
forbidden phrase that survived regeneration, a low critique score, a
user complaint, a test failure, etc.) and you propose a concrete fix.

The proposal is NOT auto-applied. The founder reviews it and decides
whether to apply, modify, or dismiss. Your job is attention-direction
plus mechanical proposal — not autonomy.

Output STRICT JSON matching this schema:

{
  "diagnosis": "<2-4 sentences. What's the underlying issue this
                observation reveals? Be specific about cause, not
                just symptom.>",
  "proposed_action": "<one paragraph. What should be done? Plain
                      English, no code yet.>",
  "tool_calls": [
    {
      "tool": "<one of: add_forbidden_phrase | add_voice_note |
              add_proof_point | add_audience_objection |
              edit_astra_file | write_astra_file>",
      "args": { ... tool-specific args ... },
      "rationale": "<why this specific call>"
    }
  ],
  "estimated_impact": "low | medium | high",
  "risk_notes": "<short — what could go wrong if applied? Empty
                 string if no real risk.>"
}

Tool call shapes (must match these signatures):

  add_forbidden_phrase:
    {"tool": "add_forbidden_phrase",
     "args": {"business": "<slug>", "phrase": "<exact phrase>",
              "rationale": "<why banned>"}}

  add_voice_note:
    {"tool": "add_voice_note",
     "args": {"business": "<slug>", "kind": "does|never|sample",
              "content": "<text>", "context": "<short>"}}

  add_proof_point:
    {"tool": "add_proof_point",
     "args": {"business": "<slug>", "section": "customers|traction|...",
              "content": "<bullet or paragraph>"}}

  add_audience_objection:
    {"tool": "add_audience_objection",
     "args": {"business": "<slug>", "audience": "<persona-slug>",
              "objection": "<text>", "response": "<text>"}}

  edit_astra_file (for code self-edits):
    {"tool": "edit_astra_file",
     "args": {"path": "<repo-relative>", "old_string": "<exact text>",
              "new_string": "<replacement>"}}

  write_astra_file (for new files):
    {"tool": "write_astra_file",
     "args": {"path": "<repo-relative>", "content": "<full content>"}}

Rules:

1. Prefer kit edits over code edits. Voice/audience/proof issues are
   usually kit-level. Only propose code edits when the issue is
   structural (e.g. a draft tool's schema is wrong).

2. tool_calls must be EXECUTABLE — args complete, paths valid, no
   placeholders like "<TBD>". The founder will literally run them.

3. If the right fix isn't actionable via these tools (e.g. requires
   data Astra doesn't have), set tool_calls=[] and explain in
   proposed_action what the founder needs to do manually.

4. estimated_impact: "high" = blocks shipping; "medium" = quality
   degradation; "low" = nice-to-have.

5. risk_notes is the founder's eyes — anything that COULD break,
   say so. Empty string when truly low-risk.

Return ONLY the JSON. No prose preamble."""


def _proposal_text_blob(d: dict[str, Any]) -> str:
    parts = [
        d.get("diagnosis", "") or "",
        d.get("proposed_action", "") or "",
        d.get("risk_notes", "") or "",
    ]
    for tc in d.get("tool_calls", []) or []:
        if isinstance(tc, dict):
            parts.append(tc.get("rationale", "") or "")
    return "\n".join(parts)


async def propose_fix(observation_id: int) -> dict[str, Any]:
    """Generate a fix proposal for an observation. Stores the proposal
    on the observation row and updates status to 'proposed'.

    Returns: the updated observation dict including proposed_action
    and proposed_tool_calls.
    """
    obs = await get_observation(observation_id)
    if not obs:
        raise FileNotFoundError(
            f"observation #{observation_id} not found"
        )
    if obs["status"] not in ("observed", "proposed"):
        # Already applied or dismissed — refuse to overwrite
        raise ValueError(
            f"observation #{observation_id} is status={obs['status']!r}; "
            f"propose_fix only applies to observed/proposed rows"
        )

    # Build the user prompt — observation + (optional) kit context
    kit_context = ""
    if obs.get("business_slug"):
        try:
            kit = load_kit(obs["business_slug"])
            kit_context = (
                f"<business-kit slug=\"{obs['business_slug']}\">\n"
                f"{kit.render_for_prompt()}\n"
                f"</business-kit>\n\n"
            )
        except FileNotFoundError:
            pass

    user_prompt = (
        f"<observation id=\"{obs['id']}\">\n"
        f"  source: {obs['source']}\n"
        f"  business: {obs.get('business_slug', '(none)')}\n"
        f"  artifact_id: {obs.get('artifact_id', '(none)')}\n"
        f"  severity: {obs['severity']}\n"
        f"  text: {obs['observation']}\n"
        f"</observation>\n\n"
        f"{kit_context}"
        f"Propose a fix. Return JSON only."
    )

    proposal = await generate_json(
        system=_PROPOSAL_SYSTEM,
        user=user_prompt,
        forbidden=[],  # proposal text is meta, not voice-bound
        text_blob_fn=_proposal_text_blob,
        model=DRAFT_MODEL,
        max_tokens=3000,
    )

    # Persist proposal on the row
    proposed_action = proposal.get("proposed_action", "") or ""
    tool_calls = proposal.get("tool_calls", []) or []
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE self_improvements
                SET proposed_action = :pa,
                    proposed_tool_calls = CAST(:tc AS JSONB),
                    status = 'proposed'
                WHERE id = :id
                """
            ),
            {
                "id": observation_id,
                "pa": proposed_action[:8000],
                "tc": json.dumps(tool_calls),
            },
        )
        await s.commit()

    updated = await get_observation(observation_id)
    if updated is None:
        raise RuntimeError(f"observation #{observation_id} disappeared after update")
    updated["full_proposal"] = proposal
    return updated


# ── Apply / dismiss ─────────────────────────────────────────────────


async def apply_fix(
    observation_id: int,
    *,
    auto_commit: bool = True,
) -> dict[str, Any]:
    """Execute the proposed_tool_calls on an observation.

    Each tool_call is dispatched to the matching kit-editor or
    code-editor function. Code-editor tools run the test gate;
    kit-editor tools commit directly. If any tool call fails,
    application halts at that point and the observation stays in
    'proposed' status (not 'applied'). The founder can fix the issue
    and re-apply.

    Returns: {observation_id, status, calls_executed, calls_failed,
              commit_hashes, errors}
    """
    obs = await get_observation(observation_id)
    if not obs:
        raise FileNotFoundError(f"observation #{observation_id} not found")
    if obs["status"] != "proposed":
        raise ValueError(
            f"observation #{observation_id} is status={obs['status']!r}; "
            f"apply_fix only applies to proposed rows"
        )
    tool_calls = obs.get("proposed_tool_calls") or []
    if not tool_calls:
        raise ValueError(
            f"observation #{observation_id} has no proposed_tool_calls; "
            f"call propose_fix first or write a manual fix"
        )

    # Lazy imports to avoid circular issues — both kit and code editors
    # depend on store layer which depends on the same DB this module uses.
    from astra.creators.edit_kit import (
        add_audience_objection as kit_add_audience_objection,
        add_forbidden_phrase as kit_add_forbidden_phrase,
        add_proof_point as kit_add_proof_point,
        add_voice_note as kit_add_voice_note,
    )
    from astra.creators.edit_code import (
        commit_code_changes,
        edit_astra_file,
        write_astra_file,
    )

    DISPATCH = {
        "add_forbidden_phrase": lambda args: kit_add_forbidden_phrase(
            args["business"], args["phrase"],
            rationale=args.get("rationale", ""),
            auto_commit=auto_commit,
        ),
        "add_voice_note": lambda args: kit_add_voice_note(
            args["business"],
            kind=args["kind"], content=args["content"],
            context=args.get("context", ""),
            auto_commit=auto_commit,
        ),
        "add_proof_point": lambda args: kit_add_proof_point(
            args["business"],
            section=args["section"], content=args["content"],
            auto_commit=auto_commit,
        ),
        "add_audience_objection": lambda args: kit_add_audience_objection(
            args["business"],
            audience=args["audience"],
            objection=args["objection"],
            response=args["response"],
            auto_commit=auto_commit,
        ),
        "edit_astra_file": lambda args: edit_astra_file(
            args["path"], args["old_string"], args["new_string"],
            replace_all=args.get("replace_all", False),
        ),
        "write_astra_file": lambda args: write_astra_file(
            args["path"], args["content"],
            overwrite_existing=args.get("overwrite_existing", False),
        ),
    }

    executed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    commit_hashes: list[str] = []
    code_edits_made = False

    for i, tc in enumerate(tool_calls):
        if not isinstance(tc, dict):
            failed.append({"index": i, "error": "tool_call is not a dict"})
            break
        tool_name = tc.get("tool")
        args = tc.get("args") or {}
        if tool_name not in DISPATCH:
            failed.append({
                "index": i, "tool": tool_name,
                "error": f"unknown tool {tool_name!r}",
            })
            break
        try:
            result = DISPATCH[tool_name](args)
            executed.append({
                "index": i, "tool": tool_name, "result": result,
            })
            # Track commits from kit edits
            if isinstance(result, dict) and result.get("commit"):
                ch = result["commit"].get("commit_hash")
                if ch:
                    commit_hashes.append(ch)
            if tool_name in ("edit_astra_file", "write_astra_file"):
                code_edits_made = True
        except Exception as e:
            logger.exception("[self_improve] tool call %d failed", i)
            failed.append({
                "index": i, "tool": tool_name,
                "error": f"{type(e).__name__}: {e}",
            })
            break

    # If we made code edits, run the test gate + commit
    if code_edits_made and not failed and auto_commit:
        try:
            commit_result = commit_code_changes(
                message=(
                    f"self-improvement: apply observation #{observation_id}\n\n"
                    f"{(obs.get('observation') or '')[:200]}"
                ),
                require_tests=True,
                push=True,
            )
            if commit_result.get("status") == "committed":
                commit_hashes.append(commit_result.get("commit_hash", "?"))
            elif commit_result.get("status") == "tests_failed":
                failed.append({
                    "phase": "code_commit",
                    "error": "tests_failed",
                    "test_result": commit_result.get("test_result"),
                })
        except Exception as e:
            failed.append({
                "phase": "code_commit",
                "error": f"{type(e).__name__}: {e}",
            })

    # Update observation row
    if failed:
        # Stay in 'proposed' — don't mark as applied
        return {
            "observation_id": observation_id,
            "status": "proposed",  # unchanged
            "calls_executed": executed,
            "calls_failed": failed,
            "commit_hashes": commit_hashes,
        }

    # Success — mark applied
    final_commit = commit_hashes[-1] if commit_hashes else None
    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE self_improvements
                SET status = 'applied',
                    applied_commit = :ch,
                    resolved_at = now()
                WHERE id = :id
                """
            ),
            {"id": observation_id, "ch": final_commit},
        )
        await s.commit()

    return {
        "observation_id": observation_id,
        "status": "applied",
        "calls_executed": executed,
        "calls_failed": [],
        "commit_hashes": commit_hashes,
    }


async def dismiss(
    observation_id: int,
    *,
    reason: str,
) -> dict[str, Any]:
    """Mark an observation as dismissed (won't be re-proposed) with a reason.

    Returns: {observation_id, status, reason}
    """
    if not reason or not reason.strip():
        raise ValueError("dismissal reason is required")
    obs = await get_observation(observation_id)
    if not obs:
        raise FileNotFoundError(f"observation #{observation_id} not found")
    if obs["status"] in ("applied", "dismissed"):
        raise ValueError(
            f"observation #{observation_id} is already {obs['status']!r}; "
            f"can't dismiss"
        )

    async with async_session() as s:
        await s.execute(
            text(
                """
                UPDATE self_improvements
                SET status = 'dismissed',
                    dismissed_reason = :reason,
                    resolved_at = now()
                WHERE id = :id
                """
            ),
            {"id": observation_id, "reason": reason[:2000]},
        )
        await s.commit()

    return {
        "observation_id": observation_id,
        "status": "dismissed",
        "reason": reason,
    }


# ── Auto-detection hooks (called from existing creator code) ────────


async def auto_observe_persistent_forbidden(
    *,
    forbidden_hits: list[str],
    business_slug: str | None = None,
    artifact_id: int | None = None,
) -> None:
    """Hook: called by _shared.generate_json when the regeneration loop
    fails (forbidden phrases still present after retry)."""
    if not forbidden_hits:
        return
    try:
        await observe(
            source="forbidden_phrase_persisted",
            observation=(
                f"After regeneration retry, these forbidden phrases still "
                f"appeared in the artifact: {forbidden_hits}. The voice "
                f"discipline loop is failing for this kit's prompt — the "
                f"system prompt may need stronger banning, OR the phrases "
                f"may be deeply embedded in the audience persona."
            ),
            business_slug=business_slug,
            artifact_id=artifact_id,
            severity="medium",
        )
    except Exception as e:
        # Self-improvement logging must never crash the caller
        logger.warning("[self_improve] auto_observe failed: %s", e)


async def auto_observe_low_critique(
    *,
    critique_artifact_id: int,
    parent_artifact_id: int,
    business_slug: str | None,
    overall_score: int,
    threshold: int = 60,
) -> None:
    """Hook: called by critique_artifact when overall_score < threshold."""
    if overall_score >= threshold:
        return
    try:
        await observe(
            source="low_critique_score",
            observation=(
                f"Critique #{critique_artifact_id} of artifact "
                f"#{parent_artifact_id} scored {overall_score}/100 "
                f"(below {threshold}). Persistent low scores often indicate "
                f"a kit-level gap — the brand kit may not surface the "
                f"information the artifact needs, OR the audience persona "
                f"may not match the actual reader."
            ),
            business_slug=business_slug,
            artifact_id=parent_artifact_id,
            severity="high" if overall_score < 40 else "medium",
        )
    except Exception as e:
        logger.warning("[self_improve] auto_observe failed: %s", e)
