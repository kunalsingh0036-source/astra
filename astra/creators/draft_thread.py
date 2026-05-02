"""
Draft a long-form thread for X (Twitter) or LinkedIn.

A thread is a sequence of short posts that build a narrative or
argument. Different shape from a carousel: text-only (mostly),
serially-readable, requires each post to land on its own.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_THREAD_SYSTEM = """You are Astra's creator sub-agent — long-form thread drafter.

You produce threads (a sequence of connected posts) for X (Twitter)
or LinkedIn. The first post hooks; each subsequent post earns the
swipe; the last delivers the payoff and the CTA.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "title": "<thread internal title — for the founder's records>",
  "platform": "twitter" | "linkedin",
  "thread_kind": "narrative" | "argument" | "framework" | "case_study" | "lessons" | "thread_essay",
  "hook_post": "<the FIRST post — must work as a standalone tweet/post. The promise of the thread. CRITICAL.>",
  "posts": [
    {
      "position": <integer, starting at 2 — hook_post is position 1 by definition>,
      "body": "<the post body. Stay within platform char limits. May reference the previous post implicitly.>",
      "purpose": "<what this post does for the narrative — 'set the scene', 'pose the contradiction', 'deliver the data', 'name the pattern', 'land the lesson'>",
      "image_hint": "<optional — concrete image direction if a visual would land. Most posts are text-only.>"
    }
  ],
  "closing_post": "<the LAST post — recap + soft CTA. Should make sense even if read alone>",
  "engagement_prompt": "<optional — a question to drive replies>",
  "best_post_time_hint": "<one short sentence — when this would land best>",
  "estimated_read_time_seconds": <integer>
}

Platform char limits (apply automatically):

X (Twitter):
- Each post: 280 char hard cap (the closing post can use 280 too;
  no buffer for the chain). Use TweetDeck-style writing — every char
  earns its place.
- Thread length: 5-12 posts ideal; 15+ loses retention badly.
- Hook post: 240 chars max — leaves room for screenshot quotability.

LinkedIn:
- Each post: ~3000 chars allowed but 800-1500 reads better.
- Thread length: 3-7 posts; LinkedIn isn't optimized for long threads —
  more value goes into longer-form-single-post-with-line-breaks.
- Hook post: works best with a strong visual line break and a clear
  promise of what's coming.

Rules:

1. Hook post is the entire game. If hook_post doesn't work as a
   standalone, the thread fails. Test: would you tap "show this
   thread" if you only saw hook_post in your feed?

2. Each subsequent post must EARN the next swipe. The reader is
   thinking "do I keep going?" after every post. Show, don't summarize.

3. No filler. If a post could be deleted without losing the
   argument, delete it.

4. Cite proof points ONLY from <proof-points>. No invented traction,
   customer names, or numbers.

5. Voice discipline: every word obeys <voice-rules>. Threads sound
   like the founder writing them, not like marketing copy. Match
   the kit's voice register (SMB-customer vs investor) to the audience.

6. Forbidden phrases: hard ban including in hook and closing posts.

7. closing_post landing: pay off the hook's promise. If hook said
   "here's what I learned raising $2M", closing must contain the
   actual lesson, not "DM me to learn more".

8. estimated_read_time_seconds: rough math — a post takes ~5-10 seconds
   to read on Twitter, ~15-25 on LinkedIn. Use this for the
   founder to decide if the thread is too long.

Return ONLY the JSON. No prose preamble."""


def _thread_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    parts.append(d.get("hook_post", "") or "")
    for p in (d.get("posts", []) or []):
        if isinstance(p, dict):
            parts.append(join_text_fields(p, ("body", "purpose", "image_hint")))
    parts.append(d.get("closing_post", "") or "")
    parts.append(d.get("engagement_prompt", "") or "")
    return "\n".join(parts)


async def draft_thread(
    *,
    business_slug: str,
    audience_slug: str,
    topic: str,
    platform: str = "twitter",
    thread_kind: str = "narrative",
    context: str = "",
) -> dict[str, Any]:
    """Draft a long-form thread.

    Args:
      business_slug: kit slug
      audience_slug: persona slug
      topic: what the thread is about
      platform: twitter (default) | linkedin
      thread_kind: hint to the model — narrative | argument | framework
        | case_study | lessons | thread_essay
      context: optional additional framing
    """
    kit = load_kit(business_slug)
    audience_md = kit.audience(audience_slug)
    if not audience_md:
        avail = sorted(kit.audiences.keys())
        raise FileNotFoundError(
            f"audience '{audience_slug}' not found in {business_slug} kit. "
            f"Available: {avail}"
        )

    platform = (platform or "twitter").lower()
    if platform not in ("twitter", "linkedin"):
        raise ValueError(f"unsupported platform: {platform}")

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug}\">\n{audience_md}\n</audience>\n\n"
        f"<platform>{platform}</platform>\n"
        f"<thread-kind>{thread_kind}</thread-kind>\n"
        f"<topic>{topic}</topic>\n"
    )
    if context:
        user_prompt += f"\n<additional-context>\n{context[:3000]}\n</additional-context>\n"
    user_prompt += "\nDraft the thread now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    th_json = await generate_json(
        system=_THREAD_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_thread_text_blob,
        max_tokens=5000,
    )

    title = th_json.get("title") or f"{kit.name} thread — {topic[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="thread",
        audience_slug=audience_slug,
        title=title,
        ask=f"{platform} thread: {topic}",
        content=th_json,
    )
    return artifact
