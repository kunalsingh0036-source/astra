"""
Draft a voiceover script — convert any artifact to read-aloud form.

Two modes:
- From an existing artifact (deck, doc, one-pager, video_brief): convert
  its content to a clean, naturally-spoken script suitable for TTS or
  human voice-over.
- From a topic + duration: standalone script generation.

Output is structured: per-segment text + duration estimate + delivery
notes (pace, tone, emphasis cues). Renders to a plain .txt file for
TTS input or human teleprompter use.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_VOICEOVER_SYSTEM = """You are Astra's creator sub-agent — voiceover script drafter.

You produce voiceover scripts intended for either text-to-speech (ElevenLabs,
PlayHT, OpenAI TTS) OR human read-aloud (founder voice-over).

Voice rules in <voice-rules> are absolute. Forbidden phrases in
<forbidden-phrases> are a hard ban — case-insensitive substring match.
Spoken voice is STRICTER than written: hedge words and corporate-speak
sound worse aloud than they read.

Your output is STRICT JSON matching this schema:

{
  "title": "<script internal title>",
  "duration_seconds": <integer — target runtime>,
  "voice_persona": "<one paragraph — who is speaking? founder voice / institutional narrator / warm SMB-customer voice. The TTS picks a voice from this hint.>",
  "delivery_notes": "<one paragraph — pace, energy, register. e.g. 'Conversational, unhurried; founder-first-person; no salesy lift on the closing line — let it land flat.' >",
  "segments": [
    {
      "position": <integer, 1-N>,
      "spoken_text": "<the exact words to be read. Punctuation matters: commas insert micro-pauses; periods full pauses; em-dashes longer pauses.>",
      "duration_seconds": <integer — estimated by word count / 2.6 wps>,
      "delivery_cue": "<optional — a per-segment cue: 'pause 0.5s after this', 'emphasize on the number', 'rising tone for the question'>",
      "emphasis_words": ["<words within spoken_text to stress>"],
      "pronunciation_notes": [
        {"word": "<unusual word>", "ipa_or_hint": "<phonetic hint, e.g. 'MCP = em-cee-pee', 'GSM = gee-ess-em'>"}
      ]
    }
  ],
  "estimated_total_words": <integer>,
  "estimated_speaking_seconds": <integer — should match duration_seconds within 1-2s>,
  "tts_recommendations": {
    "best_voice_style": "<for TTS — e.g. 'warm-male-baritone', 'crisp-female-mid', 'institutional-male-deep'>",
    "speaking_rate": "slow" | "medium" | "fast",
    "ssml_hints": "<optional SSML markers if you want pauses or emphasis encoded>"
  }
}

Rules:

1. Spoken-text discipline: every word should sound natural read aloud.
   No "synergistic". No "leverage". No "ecosystem" unless the kit
   specifically uses it. Read your draft aloud (mentally) — if any
   word makes you wince, replace it.

2. Punctuation is timing. A comma is ~0.2s pause. A period is ~0.5s.
   Em-dashes are ~0.7s. Use punctuation TO CONTROL TIMING, not just
   for grammar.

3. Sentence length: short sentences sound confident. Long sentences
   sound hedging. Mix: punchy hooks + medium body + landing line.

4. Numbers: spell out small numbers in spoken form. "20 percent" not
   "20%". "Two million" not "2M". "RS fifteen thousand" not "Rs 15K".
   The TTS or reader will mispronounce shorthand.

5. Acronyms in pronunciation_notes: any acronym the listener might
   not pronounce correctly. MCP = "em-cee-pee". GSM = "gee-ess-em".
   API = "ay-pee-ai". TBD = ... etc.

6. Duration math: spoken_text_word_count / 2.6 ≈ seconds (English
   conversational pace). Sum of segment durations should equal
   duration_seconds within 1-2s.

7. Cite proof points ONLY from <proof-points>. Don't invent traction,
   names, or numbers in the spoken script.

8. Forbidden phrases: hard ban. Spoken voice exposes corporate-speak
   like a microscope; even one forbidden phrase breaks the take.

9. delivery_notes is what the human director / TTS configurator reads
   to set the voice. Be specific. "Conversational, unhurried,
   founder-first-person, do not lift the closing line" beats
   "professional".

Return ONLY the JSON. No prose preamble."""


def _voiceover_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    parts.append(d.get("voice_persona", "") or "")
    parts.append(d.get("delivery_notes", "") or "")
    for s in (d.get("segments", []) or []):
        if isinstance(s, dict):
            parts.append(join_text_fields(s, (
                "spoken_text", "delivery_cue",
            )))
    return "\n".join(parts)


def _summarize_source_artifact(artifact: dict[str, Any]) -> str:
    """Summarize a source artifact's text content for the voiceover prompt."""
    kind = artifact.get("kind", "")
    content = artifact.get("content") or {}
    parts: list[str] = [
        f"<source-kind>{kind}</source-kind>",
        f"<source-title>{content.get('title') or artifact.get('title','')}</source-title>",
    ]
    if kind == "deck":
        for i, s in enumerate((content.get("slides", []) or []), 1):
            parts.append(f"\nSlide {i} ({s.get('type','content')}):")
            for k in ("title", "subtitle", "heading", "body_md"):
                v = s.get(k)
                if v:
                    parts.append(f"  {k}: {v}")
    elif kind == "one_pager":
        parts.append(f"\nLead: {content.get('lead','')}")
        for s in (content.get("sections", []) or []):
            parts.append(f"\n## {s.get('heading','')}\n{s.get('body_md','')}")
    elif kind == "doc":
        parts.append(f"\nExecutive summary: {content.get('executive_summary','')}")
        for s in (content.get("sections", []) or []):
            parts.append(f"\n## {s.get('heading','')}\n{s.get('body_md','')}")
    elif kind == "video_brief":
        parts.append(f"\nLogline: {content.get('logline','')}")
        for sh in (content.get("shots", []) or []):
            parts.append(
                f"\nShot {sh.get('position','?')} "
                f"({sh.get('duration_seconds','?')}s): "
                f"{sh.get('voiceover_text','')}"
            )
    elif kind == "carousel":
        for s in (content.get("slides", []) or []):
            parts.append(f"\nSlide {s.get('position','?')}: "
                         f"{s.get('headline','')} — {s.get('body','')}")
    elif kind == "thread":
        parts.append(f"\nHook: {content.get('hook_post','')}")
        for p in (content.get("posts", []) or []):
            parts.append(f"\nPost {p.get('position','?')}: {p.get('body','')}")
        parts.append(f"\nClosing: {content.get('closing_post','')}")
    else:
        parts.append(f"\n{json.dumps(content, indent=2)[:4000]}")
    return "\n".join(parts)


async def draft_voiceover_script(
    *,
    business_slug: str | None = None,
    audience_slug: str | None = None,
    duration_seconds: int = 60,
    source_artifact_id: int | None = None,
    topic: str | None = None,
    voice_persona_hint: str = "",
    context: str = "",
) -> dict[str, Any]:
    """Draft a voiceover script.

    Two modes:
      - source_artifact_id: convert an existing artifact to spoken form.
        business_slug is inferred from the source if not given.
      - topic + business_slug + audience_slug: standalone script.

    Args:
      business_slug: kit slug (required if source_artifact_id not given)
      audience_slug: persona slug (required if source_artifact_id not given)
      duration_seconds: target runtime
      source_artifact_id: optional source artifact to convert
      topic: required if no source_artifact_id
      voice_persona_hint: optional hint about who's speaking
      context: optional additional framing
    """
    duration_seconds = max(5, min(600, int(duration_seconds)))

    source_blob = ""
    if source_artifact_id:
        src = await get_artifact(int(source_artifact_id))
        if not src:
            raise FileNotFoundError(f"source artifact #{source_artifact_id} not found")
        source_blob = _summarize_source_artifact(src)
        # Inherit business + audience from source if not overridden
        if not business_slug:
            business_slug = src.get("business_slug")
        if not audience_slug:
            audience_slug = src.get("audience_slug") or audience_slug

    if not business_slug:
        raise ValueError("business_slug or source_artifact_id required")

    kit = load_kit(business_slug)
    audience_md = (kit.audience(audience_slug) or "") if audience_slug else ""

    if not source_artifact_id and not topic:
        raise ValueError("topic required when source_artifact_id is not given")

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<audience slug=\"{audience_slug or 'unspecified'}\">\n"
        f"{audience_md or '(no specific audience — judge from kit)'}\n"
        f"</audience>\n\n"
        f"<duration-seconds>{duration_seconds}</duration-seconds>\n"
    )
    if voice_persona_hint:
        user_prompt += f"<voice-persona-hint>{voice_persona_hint}</voice-persona-hint>\n"
    if topic:
        user_prompt += f"<topic>{topic}</topic>\n"
    if source_blob:
        user_prompt += (
            f"\n<source-artifact id=\"{source_artifact_id}\">\n"
            f"{source_blob[:8000]}\n</source-artifact>\n"
        )
    if context:
        user_prompt += f"\n<additional-context>\n{context[:2000]}\n</additional-context>\n"
    user_prompt += "\nDraft the voiceover script now. Return JSON only."

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    vo_json = await generate_json(
        system=_VOICEOVER_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_voiceover_text_blob,
        max_tokens=4500,
    )

    title = (
        vo_json.get("title")
        or (f"VO from #{source_artifact_id}" if source_artifact_id else f"VO — {topic[:50]}")
    )
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="voiceover_script",
        audience_slug=audience_slug,
        title=title,
        ask=topic or f"voiceover from artifact #{source_artifact_id}",
        content=vo_json,
        parent_id=source_artifact_id,
    )
    return artifact
