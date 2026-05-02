"""
Draft a subtitle set — multilingual SRT-format captions for a video.

Source can be a voiceover_script (preferred) or a video_brief or
direct text. Output includes:
- Per-language SRT-format strings (English mandatory, additional
  languages optional)
- Per-line timing
- Reading-rate validation (15-20 chars per second is the max comfortable
  reading speed for subtitles)

Defaults to English + Hindi for India-targeted content.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from astra.creators._shared import generate_json, join_text_fields
from astra.creators.kits import load_kit
from astra.creators.store import create_artifact, get_artifact

logger = logging.getLogger(__name__)


_SUBTITLE_SYSTEM = """You are Astra's creator sub-agent — subtitle drafter.

You produce subtitle sets in SRT-compatible format for AI-generated
or human-spoken videos. The subtitles must be timing-accurate AND
reading-rate-valid (max 17 characters per second on screen for comfortable
reading; below 12 cps for accessibility).

Voice rules in <voice-rules> apply to the English source. Translations
preserve meaning + register; they are NOT direct word-for-word translations.

Your output is STRICT JSON matching this schema:

{
  "title": "<subtitle set title>",
  "source_kind": "voiceover_script" | "video_brief" | "raw_text",
  "languages": [
    {
      "code": "en" | "hi" | "ta" | "te" | "mr" | "bn" | "kn" | "gu" | "<other ISO-639-1>",
      "label": "English" | "हिन्दी" | "தமிழ்" | "...",
      "is_translation": <true/false — true for everything except 'en'>,
      "lines": [
        {
          "index": <integer, 1-N>,
          "start_seconds": <float — seconds from video start>,
          "end_seconds": <float>,
          "text": "<subtitle text — 1-2 lines max, ≤42 chars per line as SRT convention>",
          "characters_per_second": <float — text length / (end-start). Must be ≤17 for comfortable reading.>"
        }
      ],
      "srt_string": "<the full SRT-format string for this language. Standard format: index newline timestamp newline text newline blank-line. Timestamps in HH:MM:SS,mmm format.>"
    }
  ],
  "validation": {
    "total_duration_seconds": <float>,
    "lines_per_language": <integer>,
    "max_cps_seen": <float — across all lines>,
    "warnings": [
      "<any subtitle that exceeds 17 cps OR overlaps OR has gaps >2s>"
    ]
  }
}

Rules:

1. SRT format MUST be exact (otherwise YouTube/Premiere/CapCut won't parse):
   - Index lines are 1-based integers
   - Timestamp format: HH:MM:SS,mmm --> HH:MM:SS,mmm (note the COMMA not period before ms)
   - Each subtitle is followed by a blank line

2. Line length: 42 characters per line max; 2 lines max per subtitle.
   Long sentences split across multiple subtitles, not multiple lines.

3. Reading rate: characters_per_second ≤17 ideal, ≤20 ceiling for fast
   talking-head. If a line exceeds 17 cps, EITHER extend its end_seconds
   (push later subtitles forward — but keep alignment with audio) OR
   split into two subtitles. Don't ship lines >20 cps.

4. Translation discipline: when translating to Hindi (hi) or other
   Indian languages, preserve the kit's voice register and forbidden-phrase
   discipline IN THE TARGET LANGUAGE. Hindi business voice is its own
   register; don't transliterate, translate.

5. Time gaps: avoid gaps >2 seconds between subtitles. If the source
   has silence, EITHER omit subtitles in that range OR extend the
   previous subtitle's end_seconds slightly to bridge.

6. Forbidden phrases: hard ban on the English source. For translations,
   the model must independently choose phrasing that respects the same
   intent (e.g. avoid hype-equivalents in Hindi).

7. If language has multiple line-breaks within a subtitle, use \\n in
   the JSON and \\n in the SRT string (which is real newlines in the
   actual file).

8. Defaults: English mandatory. Hindi recommended for India audience.
   Other languages only if explicitly requested.

Return ONLY the JSON. No prose preamble."""


def _subtitle_text_blob(d: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(d.get("title", "") or "")
    for lang in (d.get("languages", []) or []):
        if isinstance(lang, dict):
            for line in (lang.get("lines") or []):
                if isinstance(line, dict):
                    parts.append(line.get("text", "") or "")
    return "\n".join(parts)


def _summarize_source_for_subtitles(artifact: dict[str, Any]) -> str:
    """Pull timed text content from a source artifact."""
    kind = artifact.get("kind", "")
    content = artifact.get("content") or {}
    if kind == "voiceover_script":
        # voiceover already has timed segments
        parts = [f"<voiceover-script duration={content.get('duration_seconds','?')}s>"]
        cur_t = 0.0
        for seg in (content.get("segments", []) or []):
            dur = float(seg.get("duration_seconds") or 0)
            parts.append(
                f"[{cur_t:.2f}s - {cur_t + dur:.2f}s] {seg.get('spoken_text','')}"
            )
            cur_t += dur
        parts.append("</voiceover-script>")
        return "\n".join(parts)
    elif kind == "video_brief":
        parts = [f"<video-brief runtime={content.get('runtime_seconds','?')}s>"]
        cur_t = 0.0
        for sh in (content.get("shots", []) or []):
            dur = float(sh.get("duration_seconds") or 0)
            vo = sh.get("voiceover_text", "") or ""
            on = sh.get("on_screen_text", "") or ""
            parts.append(
                f"[{cur_t:.2f}s - {cur_t + dur:.2f}s] VO: {vo!r}  OSD: {on!r}"
            )
            cur_t += dur
        parts.append("</video-brief>")
        return "\n".join(parts)
    return f"<source>\n{json.dumps(content, indent=2)[:6000]}\n</source>"


async def draft_subtitle_set(
    *,
    source_artifact_id: int | None = None,
    raw_text: str | None = None,
    raw_duration_seconds: int | None = None,
    languages: list[str] | None = None,
    business_slug: str | None = None,
) -> dict[str, Any]:
    """Draft a subtitle set in one or more languages.

    Args:
      source_artifact_id: voiceover_script or video_brief artifact id
      raw_text: alternative — raw spoken text (requires raw_duration_seconds)
      raw_duration_seconds: total video runtime, required with raw_text
      languages: list of ISO-639-1 codes. Defaults to ['en', 'hi'].
      business_slug: optional override for kit context (otherwise inferred
        from the source artifact)
    """
    languages = languages or ["en", "hi"]
    if "en" not in languages:
        # English is mandatory as the source language
        languages = ["en"] + list(languages)

    source_blob = ""
    duration = raw_duration_seconds
    if source_artifact_id:
        src = await get_artifact(int(source_artifact_id))
        if not src:
            raise FileNotFoundError(f"source artifact #{source_artifact_id} not found")
        if src.get("kind") not in ("voiceover_script", "video_brief"):
            raise ValueError(
                f"source #{source_artifact_id} kind={src.get('kind')!r} — "
                "expected voiceover_script or video_brief"
            )
        source_blob = _summarize_source_for_subtitles(src)
        content = src.get("content") or {}
        duration = (
            content.get("duration_seconds")
            or content.get("runtime_seconds")
            or duration
        )
        if not business_slug:
            business_slug = src.get("business_slug")
    elif raw_text:
        if not raw_duration_seconds:
            raise ValueError(
                "raw_duration_seconds required when raw_text is given"
            )
        source_blob = (
            f"<raw-text duration={raw_duration_seconds}s>\n"
            f"{raw_text[:6000]}\n</raw-text>"
        )
    else:
        raise ValueError("Either source_artifact_id or raw_text required")

    if not business_slug:
        raise ValueError("business_slug required (could not infer from source)")

    kit = load_kit(business_slug)

    user_prompt = (
        f"{kit.render_for_prompt()}\n\n"
        f"<target-languages>{','.join(languages)}</target-languages>\n"
        f"<duration-seconds>{duration or 'unknown'}</duration-seconds>\n\n"
        f"<source>\n{source_blob[:10000]}\n</source>\n\n"
        "Produce the subtitle set in SRT-compatible JSON now. "
        "Return JSON only."
    )

    forbidden = kit.brand.get("forbidden_phrases", []) or []
    sub_json = await generate_json(
        system=_SUBTITLE_SYSTEM,
        user=user_prompt,
        forbidden=forbidden,
        text_blob_fn=_subtitle_text_blob,
        max_tokens=6000,
    )

    title = (
        f"Subtitles ({','.join(languages)}) — "
        + (f"from #{source_artifact_id}" if source_artifact_id else "raw")
    )
    artifact = await create_artifact(
        business_slug=business_slug,
        kind="subtitle_set",
        title=title,
        ask=f"subtitles in {languages}",
        content=sub_json,
        parent_id=source_artifact_id,
    )
    return artifact
