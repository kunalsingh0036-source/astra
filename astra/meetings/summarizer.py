"""
Claude-powered meeting summarization + action-item extraction.

Prompt is tuned for Kunal's meeting mix: investor calls (HelmTech
fundraise), BAY partnership discussions, Apex buyer calls, Top
Studios briefs, internal agent syncs. Pulls out:

  * 3-sentence gist ("what this meeting was about")
  * Decisions (things we settled)
  * Action items (things to do, with owner + rough timing if stated)
  * Open questions (things we couldn't answer)
  * Follow-up draft (email Kunal should send post-meeting)

Output is structured JSON so the caller can split into DB columns
and stage tasks cleanly.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MeetingSummary:
    gist: str
    decisions: list[str]
    action_items: list[dict]     # {title, owner?, due?, priority?}
    open_questions: list[str]
    followup_draft: str
    raw_json: str


def _get_api_key() -> str:
    """Resolve the anthropic key with the same fallback chain as the
    scheduler — env first, .env second. pydantic-settings sometimes
    gets an empty env var from the parent shell."""
    from astra.config import settings

    key = settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if key:
        return key
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


async def summarize_transcript(
    transcript: str,
    *,
    title: str = "",
    today_ist: str | None = None,
) -> MeetingSummary:
    """Send the transcript to Claude and parse structured output.

    `today_ist` — ISO date string for "today" in IST; used so Claude
    can resolve relative dates ("Friday", "next Tuesday") into real
    YYYY-MM-DD. Defaults to the actual current IST date.

    Falls back to an empty MeetingSummary on API failure so the caller
    can still persist the raw transcript + flag the row as 'error'.
    """
    import anthropic
    from datetime import datetime, timedelta, timezone

    from astra.config import settings

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("anthropic API key not configured")

    client = anthropic.AsyncAnthropic(api_key=api_key)

    if today_ist is None:
        ist = timezone(timedelta(hours=5, minutes=30))
        today_ist = datetime.now(ist).strftime("%Y-%m-%d (%A)")

    prompt = f"""You are Astra, Kunal's personal AI agent. A meeting just happened and was transcribed by whisper.cpp — expect occasional word errors on Indian names and technical terms. Read the transcript below and produce a structured summary.

TODAY IS: {today_ist}  — use this to resolve any relative dates mentioned in the meeting ("Friday", "tomorrow", "next Tuesday", "end of month") into actual YYYY-MM-DD. The action_items `due` field MUST be either a real YYYY-MM-DD relative to today, or null if unclear.

Return STRICT JSON only — no prose, no code fences, no preamble. Shape:

{{
  "gist": "<2-3 sentence summary of what the meeting was about and the outcome>",
  "decisions": ["<decision 1>", "<decision 2>"],
  "action_items": [
    {{
      "title": "<imperative — e.g. 'Send pitch deck to Ankur by Friday'>",
      "owner": "<kunal|other-party-name|unknown>",
      "due": "<YYYY-MM-DD relative to today, or null if unstated>",
      "priority": <1|2|3>   // 1=low, 2=normal, 3=high
    }}
  ],
  "open_questions": ["<question 1>", "<question 2>"],
  "followup_draft": "<1-paragraph email Kunal can send as post-meeting follow-up. Start with 'Thanks for the call' or similar. Reference decisions + action items. Professional but personal tone.>"
}}

Rules:
- Only list action items that were explicitly discussed as to-dos. Do not invent or pad.
- If an item's owner is unclear, use "unknown".
- Relative dates ("Friday", "next Tuesday", "EOD") MUST be converted to a real YYYY-MM-DD using TODAY's date above. If no date was discussed at all, use null.
- If the transcript is empty or all [BLANK_AUDIO], return `gist` = "Empty or silent recording — no content to summarize." with empty arrays.
- Priority heuristic: fundraise/client/revenue-blocking = 3; team / internal = 2; admin = 1.
- Kunal's businesses: HelmTech (Shotgun AI platform + Buckshot AI film), Apex Human (corporate merchandise), BAY (squash institution), Top Studios. When a business context is clear, weave it into `gist`.

Meeting title (from filename, may be unhelpful): {title!r}

Transcript:
---
{transcript[:60000]}
---

Return JSON now.
"""

    response = await client.messages.create(
        model=settings.model_sonnet,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "\n".join(
        block.text for block in response.content if hasattr(block, "text")
    ).strip()

    # Strip code fences defensively.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        data = json.loads(text)
    except Exception as e:
        logger.warning("[meetings] summary JSON parse failed: %s", e)
        return MeetingSummary(
            gist="Summary parse failed — see raw_json for Claude's output.",
            decisions=[],
            action_items=[],
            open_questions=[],
            followup_draft="",
            raw_json=text,
        )

    return MeetingSummary(
        gist=str(data.get("gist", "") or ""),
        decisions=list(data.get("decisions", []) or []),
        action_items=list(data.get("action_items", []) or []),
        open_questions=list(data.get("open_questions", []) or []),
        followup_draft=str(data.get("followup_draft", "") or ""),
        raw_json=text,
    )
