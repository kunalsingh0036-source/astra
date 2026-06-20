"""
Content tools — the conversational surface for the LinkedIn beachhead.

The daily content_draft job stages LinkedIn posts (drafted from the
research briefing's outward insight, status='pending_review'). These
tools let Astra DRIVE that staged work from chat — WhatsApp or web —
so Kunal can ship posts by talking:

  "show my post"            → list_content_drafts
  "approve it / I'll post"  → approve_content_draft(id)
  "make it punchier"        → refine_content_draft(id, "punchier")
  "drop it"                 → discard_content_draft(id)
  "draft a post now"        → draft_linkedin_now()
  "how's my posting"        → content_metrics

Astra never posts to LinkedIn. Approve = "I'm shipping this" (the
metric); Kunal pastes it into LinkedIn himself. Optionally he passes
the posted URL back to record a true 'posted'.
"""

from __future__ import annotations

from astra.runtime.sdk_compat import create_sdk_mcp_server, tool


def _post_display(content: dict) -> str:
    body = (content.get("body") or "").strip()
    tags = content.get("hashtags") or []
    tail = ("\n\n" + " ".join(tags)) if tags else ""
    return body + tail


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _to_int(v) -> int | None:
    try:
        return int(str(v).strip())
    except Exception:
        return None


@tool(
    "list_content_drafts",
    "List the LinkedIn post DRAFTS waiting for Kunal — posts Astra drafted "
    "from the daily research and staged for review. Each has an id (use it "
    "with approve/refine/discard), a title, and the post text. Use when "
    "Kunal says 'show my post', 'what's drafted', or after a content nudge.",
    {"limit": int},
)
async def list_content_drafts_tool(args: dict) -> dict:
    from astra.creators.store import list_content_artifacts

    limit = max(1, min(20, int(args.get("limit") or 10)))
    try:
        rows = await list_content_artifacts(
            kind="linkedin_post", status="pending_review", limit=limit
        )
    except Exception as e:
        return _err(f"couldn't load content drafts: {e}")
    if not rows:
        return _ok("No LinkedIn drafts waiting. The daily 08:00 job stages one "
                   "from the morning research when there's a postable angle.")
    lines = [f"{len(rows)} LinkedIn draft(s) waiting:"]
    for d in rows:
        c = d.get("content") or {}
        lines.append(
            f"\n[id {d['id']}] {d.get('title', '')[:70]}\n"
            f"{_post_display(c)[:600]}"
        )
    lines.append(
        "\nTo act: approve_content_draft(id), refine_content_draft(id, instruction), "
        "or discard_content_draft(id)."
    )
    return _ok("\n".join(lines))


@tool(
    "get_content_draft",
    "Show the full text of one staged LinkedIn post by its id (from "
    "list_content_drafts). Use when Kunal wants to read the whole post "
    "before deciding.",
    {"artifact_id": int},
)
async def get_content_draft_tool(args: dict) -> dict:
    from astra.creators.store import get_artifact

    aid = _to_int(args.get("artifact_id"))
    if aid is None:
        return _err("get_content_draft: artifact_id required")
    art = await get_artifact(aid)
    if not art or art.get("kind") != "linkedin_post":
        return _err(f"no LinkedIn draft with id {aid}")
    c = art.get("content") or {}
    angle = c.get("reason") or ""
    body = _post_display(c)
    src = c.get("briefing_topic") or ""
    head = f"LinkedIn draft [id {aid}] · status {art.get('status')}"
    extra = f"\n(angle: {angle})" if angle else ""
    extra += f"\n(from research: {src})" if src else ""
    return _ok(f"{head}{extra}\n\n{body}")


@tool(
    "approve_content_draft",
    "Approve a staged LinkedIn post — Kunal's signal that he's shipping it "
    "(this is the posts-shipped metric). Pass the draft id. If he gives the "
    "LinkedIn URL after posting, pass posted_url to record a confirmed post. "
    "Astra does NOT post to LinkedIn; Kunal pastes it himself.",
    {"artifact_id": int, "posted_url": str},
)
async def approve_content_draft_tool(args: dict) -> dict:
    from astra.creators.store import set_artifact_status

    aid = _to_int(args.get("artifact_id"))
    if aid is None:
        return _err("approve_content_draft: artifact_id required")
    url = (args.get("posted_url") or "").strip()
    status = "posted" if url else "approved"
    merge = {"posted_url": url} if url else None
    try:
        ok = await set_artifact_status(aid, status=status, merge_content=merge)
    except Exception as e:
        return _err(f"approve failed: {e}")
    if not ok:
        return _err(f"no LinkedIn draft with id {aid}")
    if url:
        return _ok(f"Marked posted ✓ (recorded {url}).")
    return _ok("Approved — counts as shipped. Paste it into LinkedIn when ready; "
               "send me the URL after and I'll mark it posted.")


@tool(
    "refine_content_draft",
    "Revise a staged LinkedIn post per Kunal's instruction (e.g. 'punchier "
    "hook', 'shorter', 'lead with the contrarian point', 'drop the last "
    "line'). Keeps his voice + the rules. Does NOT post — the revised draft "
    "stays pending review.",
    {"artifact_id": int, "instruction": str},
)
async def refine_content_draft_tool(args: dict) -> dict:
    from astra.creators.draft_linkedin_post import refine_linkedin_post

    aid = _to_int(args.get("artifact_id"))
    instruction = (args.get("instruction") or "").strip()
    if aid is None or not instruction:
        return _err("refine_content_draft: artifact_id and instruction required")
    try:
        art = await refine_linkedin_post(aid, instruction)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:
        return _err(f"refine failed: {e}")
    return _ok("Revised:\n\n" + _post_display(art.get("content") or {}))


@tool(
    "discard_content_draft",
    "Discard a staged LinkedIn post Kunal doesn't want. Pass the id. Marked "
    "rejected (counts against approval rate). Use when he says 'drop it' / "
    "'not this one'.",
    {"artifact_id": int},
)
async def discard_content_draft_tool(args: dict) -> dict:
    from astra.creators.store import set_artifact_status

    aid = _to_int(args.get("artifact_id"))
    if aid is None:
        return _err("discard_content_draft: artifact_id required")
    try:
        ok = await set_artifact_status(aid, status="rejected")
    except Exception as e:
        return _err(f"discard failed: {e}")
    return _ok("Discarded.") if ok else _err(f"no LinkedIn draft with id {aid}")


@tool(
    "draft_linkedin_now",
    "Draft a LinkedIn post on demand from a research briefing (defaults to "
    "the latest). Use when Kunal says 'draft a post now' or 'write a post "
    "from today's research'. Optionally pass a briefing_id. Stages it for "
    "review like the daily job; returns the new draft.",
    {"briefing_id": int},
)
async def draft_linkedin_now_tool(args: dict) -> dict:
    from astra.creators.draft_linkedin_post import draft_linkedin_post

    bid = _to_int(args.get("briefing_id")) if args.get("briefing_id") else None
    try:
        res = await draft_linkedin_post(briefing_id=bid, force=True)
    except Exception as e:
        return _err(f"draft failed: {e}")
    st = res.get("status")
    if st == "staged":
        from astra.creators.store import get_artifact

        art = await get_artifact(res["artifact_id"])
        body = _post_display((art or {}).get("content") or {})
        return _ok(f"Drafted [id {res['artifact_id']}]:\n\n{body}\n\n"
                   "Say approve / refine / discard.")
    if st == "not_postable":
        return _ok(f"No post drafted — {res.get('reason', 'no postable angle in the briefing')}.")
    if st == "no_briefing":
        return _ok("No research briefing is ready to draft from yet.")
    return _err(f"draft did not stage: {res.get('reason', st)}")


@tool(
    "content_metrics",
    "The content beachhead's value number: over the last N days, how many "
    "LinkedIn posts were drafted, approved (=shipped), posted (with URL), "
    "rejected, still pending — plus approval rate + posts/week. Use when "
    "Kunal asks how his posting is going, or for the Friday review.",
    {"days": int},
)
async def content_metrics_tool(args: dict) -> dict:
    from astra.creators.draft_linkedin_post import content_metrics

    days = max(1, min(90, int(args.get("days") or 7)))
    try:
        m = await content_metrics(days)
    except Exception as e:
        return _err(f"metrics failed: {e}")
    rate = m.get("approval_rate")
    rate_txt = f"{rate:.0%}" if isinstance(rate, (int, float)) else "n/a"
    text = (
        f"LinkedIn content · last {m.get('window_days', days)}d\n"
        f"  drafted:   {m.get('drafted', 0)}\n"
        f"  approved:  {m.get('approved', 0)} (shipped)\n"
        f"  posted:    {m.get('posted', 0)} (URL confirmed)\n"
        f"  rejected:  {m.get('rejected', 0)}\n"
        f"  pending:   {m.get('pending', 0)}\n"
        f"  approval rate: {rate_txt}\n"
        f"  pace: ~{m.get('posts_per_week', 0)} posts/week"
    )
    return _ok(text)


def create_content_mcp_server():
    return create_sdk_mcp_server(
        name="astra-content",
        version="0.1.0",
        tools=[
            list_content_drafts_tool,
            get_content_draft_tool,
            approve_content_draft_tool,
            refine_content_draft_tool,
            discard_content_draft_tool,
            draft_linkedin_now_tool,
            content_metrics_tool,
        ],
    )
