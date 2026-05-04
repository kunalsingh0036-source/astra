"""
MCP tools for Astra's Layer 4 — proactive self-improvement.

The queue lives in the self_improvements DB table. Auto-detection
hooks in _shared.generate_json + critique populate the queue
automatically; these tools surface and act on it.

Tool flow:
  list_self_improvements  → see what's pending
  observe_issue           → manually log an observation (from user feedback)
  propose_self_improvement → ask Astra to propose a fix for one row
  review_proposal         → see the proposed tool calls before applying
  apply_self_improvement  → execute the proposal (gated by autonomy mode)
  dismiss_self_improvement → mark as not actionable, with reason
"""

from __future__ import annotations

import json

from astra.runtime.sdk_compat import tool

from astra.creators.self_improve import (
    apply_fix,
    dismiss,
    get_observation,
    list_observations,
    observe,
    propose_fix,
)


@tool(
    "observe_issue",
    "Log an observation about something that needs attention. Most "
    "observations are logged automatically by the auto-detection hooks "
    "(forbidden phrases that survive regeneration, low critique scores). "
    "Use this tool to log MANUAL observations — user feedback ('this "
    "deck didn't land'), pattern recognition ('we keep getting questioned "
    "about X'), or anything else worth queuing for review.",
    {
        "source": str,           # forbidden_phrase_persisted | low_critique_score | user_feedback | test_failure | render_failure | manual
        "observation": str,
        "business": str,         # optional kit slug
        "artifact_id": int,      # optional related artifact
        "severity": str,         # low | medium | high
    },
)
async def observe_issue_tool(args: dict) -> dict:
    source = (args.get("source") or "manual").strip()
    observation = (args.get("observation") or "").strip()
    business = (args.get("business") or "").strip() or None
    aid = int(args.get("artifact_id") or 0) or None
    severity = (args.get("severity") or "medium").strip()

    if not observation:
        return {"content": [{"type": "text", "text": (
            "observe_issue requires: observation"
        )}]}
    try:
        result = await observe(
            source=source, observation=observation,
            business_slug=business, artifact_id=aid,
            severity=severity,
        )
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"observe failed: {type(e).__name__}: {e}"
        )}]}

    if result.get("deduped"):
        return {"content": [{"type": "text", "text": (
            f"Observation already in queue as #{result['id']} "
            f"(deduped). No new row created."
        )}]}
    return {"content": [{"type": "text", "text": (
        f"Observation #{result['id']} logged ({source}, {severity}).\n"
        f"Run propose_self_improvement(observation_id={result['id']}) "
        f"when ready to generate a fix proposal."
    )}]}


@tool(
    "list_self_improvements",
    "List the self-improvement queue. By default returns ACTIVE rows "
    "(observed or proposed). Pass status='applied' or 'dismissed' for "
    "history; status='all' for everything. Optionally filter by business "
    "or severity. Newest first.",
    {
        "status": str,           # observed | proposed | applied | dismissed | all (default: active)
        "business": str,
        "severity": str,
        "limit": int,
    },
)
async def list_self_improvements_tool(args: dict) -> dict:
    status = (args.get("status") or "").strip() or None
    business = (args.get("business") or "").strip() or None
    severity = (args.get("severity") or "").strip() or None
    limit = int(args.get("limit") or 50)

    try:
        rows = await list_observations(
            status=status, business_slug=business,
            severity=severity, limit=limit,
        )
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"list failed: {type(e).__name__}: {e}"
        )}]}

    if not rows:
        return {"content": [{"type": "text", "text": (
            "Queue is empty (no rows match the filter)."
        )}]}

    lines = [f"{len(rows)} observation{'s' if len(rows) != 1 else ''}:"]
    for r in rows:
        biz = r.get("business_slug") or "—"
        aid = r.get("artifact_id")
        aid_s = f"#{aid}" if aid else "—"
        proposed = "✓" if r.get("proposed_action") else " "
        applied = "✓" if r.get("applied_commit") else " "
        lines.append(
            f"  #{r['id']:<4} [{r['status']:9}] [{r['severity']:6}] "
            f"{r['source']:28} biz={biz:14} art={aid_s:6} "
            f"prop={proposed} appl={applied}"
        )
        # First 80 chars of observation
        obs = (r.get("observation") or "")[:140]
        lines.append(f"        {obs}")
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


@tool(
    "propose_self_improvement",
    "Generate a fix proposal for one observation. Reads the observation "
    "+ relevant kit context, asks Claude to diagnose the underlying "
    "issue and propose concrete tool calls (kit-editor or code-editor) "
    "that would fix it. Saves the proposal on the observation row "
    "(status moves to 'proposed'). Does NOT apply — review with "
    "review_proposal, then apply_self_improvement.",
    {"observation_id": int},
)
async def propose_self_improvement_tool(args: dict) -> dict:
    oid = int(args.get("observation_id") or 0)
    if not oid:
        return {"content": [{"type": "text", "text": (
            "propose_self_improvement requires: observation_id"
        )}]}
    try:
        result = await propose_fix(oid)
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"propose failed: {type(e).__name__}: {e}"
        )}]}

    full = result.get("full_proposal") or {}
    tool_calls = full.get("tool_calls") or []
    text = [
        f"Proposal for observation #{oid}:",
        f"  Diagnosis: {(full.get('diagnosis') or '')[:300]}",
        f"  Action: {(full.get('proposed_action') or '')[:300]}",
        f"  Estimated impact: {full.get('estimated_impact', '?')}",
        f"  Risk notes: {full.get('risk_notes', '') or 'none'}",
        f"  Tool calls ({len(tool_calls)}):",
    ]
    for i, tc in enumerate(tool_calls):
        if isinstance(tc, dict):
            text.append(f"    {i+1}. {tc.get('tool', '?')}({tc.get('rationale', '')[:60]})")
    text.append("")
    text.append(
        f"Apply with: apply_self_improvement(observation_id={oid})"
    )
    text.append(
        f"Or dismiss: dismiss_self_improvement(observation_id={oid}, reason='...')"
    )
    return {"content": [{"type": "text", "text": "\n".join(text)}]}


@tool(
    "review_proposal",
    "Show the FULL proposed_tool_calls for an observation, ready for "
    "human review before applying. Use AFTER propose_self_improvement "
    "and BEFORE apply_self_improvement to see exactly what will run.",
    {"observation_id": int},
)
async def review_proposal_tool(args: dict) -> dict:
    oid = int(args.get("observation_id") or 0)
    if not oid:
        return {"content": [{"type": "text", "text": (
            "review_proposal requires: observation_id"
        )}]}
    obs = await get_observation(oid)
    if not obs:
        return {"content": [{"type": "text", "text": (
            f"observation #{oid} not found"
        )}]}
    if obs["status"] != "proposed":
        return {"content": [{"type": "text", "text": (
            f"observation #{oid} is status={obs['status']!r} — "
            f"call propose_self_improvement first if you want a proposal"
        )}]}

    tcs = obs.get("proposed_tool_calls") or []
    text = [
        f"Proposal review — observation #{oid}",
        f"  Source: {obs['source']}",
        f"  Severity: {obs['severity']}",
        f"  Business: {obs.get('business_slug') or '(none)'}",
        f"  Artifact: {obs.get('artifact_id') or '(none)'}",
        f"  Observation:",
        f"    {obs.get('observation', '')[:600]}",
        "",
        f"  Proposed action:",
        f"    {(obs.get('proposed_action') or '')[:600]}",
        "",
        f"  Tool calls ({len(tcs)}):",
    ]
    for i, tc in enumerate(tcs):
        if isinstance(tc, dict):
            text.append(f"    {i+1}. {tc.get('tool', '?')}")
            args_pretty = json.dumps(tc.get("args", {}), indent=6)[:1500]
            text.append(f"       args: {args_pretty}")
            if tc.get("rationale"):
                text.append(f"       why:  {tc['rationale'][:200]}")
    return {"content": [{"type": "text", "text": "\n".join(text)}]}


@tool(
    "apply_self_improvement",
    "Execute the proposed tool calls for an observation. Kit edits "
    "auto-commit + push. Code edits run the test gate (must pass) "
    "before committing. If any tool call fails, application halts and "
    "the observation stays in 'proposed' status. CRITICAL: this tool "
    "actually mutates kits / code — the autonomy mode system should "
    "require human approval at the call site.",
    {
        "observation_id": int,
        "auto_commit": bool,    # default True
    },
)
async def apply_self_improvement_tool(args: dict) -> dict:
    oid = int(args.get("observation_id") or 0)
    auto_commit = bool(args.get("auto_commit", True))
    if not oid:
        return {"content": [{"type": "text", "text": (
            "apply_self_improvement requires: observation_id"
        )}]}
    try:
        result = await apply_fix(oid, auto_commit=auto_commit)
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"apply failed: {type(e).__name__}: {e}"
        )}]}

    status = result["status"]
    executed = result.get("calls_executed", []) or []
    failed = result.get("calls_failed", []) or []
    commits = result.get("commit_hashes", []) or []

    text = [
        f"Apply observation #{oid}: status={status}",
        f"  Calls executed: {len(executed)}",
        f"  Calls failed:   {len(failed)}",
        f"  Commits:        {commits}",
    ]
    if failed:
        text.append("\nFailures:")
        for f in failed[:5]:
            text.append(f"  - {f.get('tool', f.get('phase', '?'))}: "
                        f"{f.get('error', '?')[:200]}")
            if f.get("test_result"):
                tr = f["test_result"]
                text.append(
                    f"    tests: {tr.get('passed_count', 0)}/{tr.get('passed_count', 0) + tr.get('failed_count', 0)} passed"
                )
    return {"content": [{"type": "text", "text": "\n".join(text)}]}


@tool(
    "dismiss_self_improvement",
    "Mark an observation as dismissed (won't be re-proposed) with a "
    "reason. Use when the proposed fix is wrong, the issue is "
    "intentional, or the founder decides to handle it manually.",
    {
        "observation_id": int,
        "reason": str,
    },
)
async def dismiss_self_improvement_tool(args: dict) -> dict:
    oid = int(args.get("observation_id") or 0)
    reason = (args.get("reason") or "").strip()
    if not (oid and reason):
        return {"content": [{"type": "text", "text": (
            "dismiss_self_improvement requires: observation_id, reason"
        )}]}
    try:
        result = await dismiss(oid, reason=reason)
    except FileNotFoundError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except ValueError as e:
        return {"content": [{"type": "text", "text": str(e)}]}
    except Exception as e:
        return {"content": [{"type": "text", "text": (
            f"dismiss failed: {type(e).__name__}: {e}"
        )}]}
    return {"content": [{"type": "text", "text": (
        f"Dismissed observation #{oid}: {reason[:200]}"
    )}]}


SELF_IMPROVE_TOOLS = [
    observe_issue_tool,
    list_self_improvements_tool,
    propose_self_improvement_tool,
    review_proposal_tool,
    apply_self_improvement_tool,
    dismiss_self_improvement_tool,
]
