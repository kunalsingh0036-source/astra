"""
Draft an AI-video brief — shot list + script + b-roll + music + per-shot prompts.

The brief is the prompt-first artifact for AI video generation. It:
- breaks the video into shots
- writes the voiceover line-by-line, timed to shots
- specifies on-screen text per shot (lower-thirds, captions, titles)
- generates per-shot image-gen prompts (brand-aware)
- specifies music vibe + b-roll list

Use case: founder generates this brief, then either pastes the per-shot
prompts into Sora / Runway / Veo (the flagship UI gets the latest
models first), OR — when MCPs mature — Astra orchestrates the rendering
itself. The brief is the canonical artifact either way.
"""

from __future__ import annotations

import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact

logger = logging.getLogger(__name__)


_VIDEO_BRIEF_SYSTEM = """You are Astra's creator sub-agent — AI-video brief drafter.

You produce structured briefs for short-form AI-generated videos.
Output is shot-by-shot with timing, voiceover, on-screen text,
image-gen prompt, and b-roll notes — the prompt-first artifact a human
(or future MCP-mediated agent) takes into Sora / Runway / Veo to
render the actual frames.

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.

Your output is STRICT JSON matching this schema:

{
  "title": "<video internal title — for the founder's records>",
  "format": "vertical_short" | "horizontal_short" | "square",
  "platform": "instagram_reels" | "youtube_shorts" | "linkedin" | "twitter" | "tiktok" | "internal_brief",
  "runtime_seconds": <integer, 15-90 for shorts; up to 180 for longer>,
  "logline": "<one-sentence — what the video is about, in the voice the script will use>",
  "narrative_arc": "<one paragraph — how the video unfolds from hook to payoff to CTA>",
  "music_vibe": "<concrete description of music: tempo, instrumentation, mood, brand-fit. e.g. 'Sparse piano + sub-bass, 70 BPM, instrumental, no drops; institutional but not somber.' NOT 'upbeat'.>",
  "music_reference_artists_or_genres": ["<2-4 references — 'Hans Zimmer film-score', 'Bonobo electronic minimalism'>"],

  "shots": [
    {
      "position": <integer, 1-N>,
      "duration_seconds": <integer or float — typical: 2-6s>,
      "shot_type": "talking_head" | "establishing" | "close_up" | "product_shot" | "data_overlay" | "b_roll" | "title_card" | "transition",
      "voiceover_text": "<the EXACT words spoken during this shot. Empty string if shot is silent.>",
      "on_screen_text": "<text that appears on screen during the shot — usually short, big-font. Empty string if no on-screen text.>",
      "visual_description": "<the shot — concrete, specific. 'Close-up of the GSM tag on a folded polo, gold-thread embroidery in shallow focus, soft side-light.' NOT 'a polo'.>",
      "image_prompt": "<a complete image-gen prompt for THIS shot's hero frame. Brand colors anchored. Aspect-ratio-aware. Negative-prompt cues from kit.imagery anti-patterns.>",
      "negative_prompt": "<comma-separated cues to avoid for this shot, drawn from the kit's imagery anti-patterns + general AI-video pitfalls>",
      "transition_in": "cut" | "fade" | "match_cut" | "slide" | "zoom",
      "transition_out": "cut" | "fade" | "match_cut" | "slide" | "zoom"
    }
  ],

  "b_roll_list": [
    {
      "name": "<short label>",
      "description": "<concrete — what the b-roll shows>",
      "duration_seconds": <integer>,
      "where_used": "<which shots this b-roll plays under, by position>"
    }
  ],

  "captions_burnt_in": <true/false — true if the video has hard-coded captions; false if the platform overlays them>,

  "thumbnail_prompt": "<a complete image-gen prompt for the video's thumbnail / cover frame. Different framing from any shot — usually a striking single image with maximum stopping power.>",

  "platform_specific_notes": {
    "primary": "<one sentence — how this video lands on the primary platform>",
    "repurpose": "<one paragraph — how to cut this for the OTHER platforms (cropping, length trim, caption changes)>"
  },

  "post_production_notes": [
    "<short note — 'Add brand emerald lower-third on shots 3 and 7 with tagline'.>"
  ]
}

Rules:

1. Total duration check: sum of shot durations should equal runtime_seconds (within 1 second). If you're off, the model is hallucinating; recompute.

2. Voiceover totaling: spoken words / 2.6 ≈ seconds (English at conversational pace). If voiceover_text totals 60 words, that's ~23 seconds of speech. Don't over-cram — leave room for visual breathing. Empty voiceover is FINE for visual-led shots.

3. Image prompts per shot: each one must be brand-aware. Pull brand
   colors from the kit (anchor to hex codes in the prompt). Pull
   imagery anti-patterns from kit.imagery for the negative prompt.
   Don't include text inside the image prompt — generated text from
   image models is unreliable. The on_screen_text is overlaid in post.

4. Cite proof points ONLY from <proof-points>. Don't invent traction,
   customer names, or numbers in the voiceover or on-screen text.

5. Shot count: 5-12 shots for a 15-60s short; 12-25 for longer. Resist
   over-cutting — modern AI-video models handle 4-8 second sustained
   shots well, and rapid cutting often hides poor frame quality rather
   than serving the story.

6. Music vibe: CONCRETE. Tempo, instrumentation, mood, what NOT to use.
   "Cinematic" alone tells the editor nothing. "Sparse piano + sub-bass,
   70 BPM, instrumental" tells them everything.

7. Voice discipline: voiceover_text and on_screen_text obey
   <voice-rules>. Forbidden phrases are banned including in voiceover
   (which is just spoken text — same rules).

8. Format / aspect ratios:
   - vertical_short: 9:16 (Reels, Shorts, TikTok)
   - horizontal_short: 16:9 (LinkedIn, Twitter, YouTube)
   - square: 1:1 (some Instagram feed, X)
   image_prompt aspect should match.

Return ONLY the JSON. No prose preamble."""


def _video_brief_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    parts.append(d.get("logline", "") or "")
    parts.append(d.get("narrative_arc", "") or "")
    for s in (d.get("shots", []) or []):
        if isinstance(s, dict):
            parts.append(join_text_fields(s, (
                "voiceover_text", "on_screen_text", "visual_description",
                "image_prompt",
            )))
    for br in (d.get("b_roll_list", []) or []):
        if isinstance(br, dict):
            parts.append(join_text_fields(br, ("name", "description")))
    parts.append(d.get("thumbnail_prompt", "") or "")
    psn = d.get("platform_specific_notes") or {}
    if isinstance(psn, dict):
        parts.append(psn.get("primary", "") or "")
        parts.append(psn.get("repurpose", "") or "")
    parts.extend(d.get("post_production_notes", []) or [])
    return "\n".join(parts)


async def draft_video_brief(
    *,
    business_slug: str,
    audience_slug: str,
    topic: str,
    runtime_seconds: int = 30,
    format: str = "vertical_short",
    platform: str = "instagram_reels",
    context: str = "",
) -> dict[str, Any]:
    """Draft an AI-video brief.

    Args:
      business_slug: kit slug
      audience_slug: persona slug
      topic: what the video is about
      runtime_seconds: target runtime — 15-90 for shorts, up to 180
      format: vertical_short | horizontal_short | square
      platform: instagram_reels | youtube_shorts | linkedin |
        twitter | tiktok | internal_brief
      context: free-text additional framing
    """
    kit = load_kit(business_slug)
    audience_md = kit.audience(audience_slug)
    if not audience_md:
        avail = sorted(kit.audiences.keys())
        raise FileNotFoundError(
            f"audience '{audience_slug}' not found in {business_slug} kit. "
            f"Available: {avail}"
        )
    runtime_seconds = max(8, min(180, int(runtime_seconds)))

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug}\">\n{audience_md}\n</audience>\n\n"
        f"<format>{format}</format>\n"
        f"<platform>{platform}</platform>\n"
        f"<runtime-seconds>{runtime_seconds}</runtime-seconds>\n"
        f"<topic>{topic}</topic>\n"
    )
    if context:
        user_prompt += f"\n<additional-context>\n{context[:3000]}\n</additional-context>\n"
    user_prompt += "\nDraft the video brief now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    vb_json = await generate_json(
        system=_VIDEO_BRIEF_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_video_brief_text_blob,
        max_tokens=8000,
    )

    title = vb_json.get("title") or f"{kit.name} video — {topic[:60]}"
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="video_brief",
        audience_slug=audience_slug,
        title=title,
        ask=f"{format} {runtime_seconds}s for {platform}: {topic}",
        content=vb_json,
    )
    return artifact
