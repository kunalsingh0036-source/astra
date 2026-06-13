"""
WhatsApp voice-note transcription.

The phone-native input: Kunal sends a voice note instead of typing,
especially mid-training or driving. Meta delivers these as
`type: "audio"` messages with a media id (not the bytes). Flow:
  1. Resolve the media id → a short-lived download URL (Graph API).
  2. Download the audio bytes (WhatsApp voice notes are OGG/Opus).
  3. Transcribe via Whisper-compatible STT.

Transcription is provider-pluggable and env-driven, matching the
rest of the mesh: set ONE of OPENAI_API_KEY (api.openai.com) or
GROQ_API_KEY (Groq's whisper-large-v3, faster + cheaper) and voice
notes light up. With neither, transcribe() returns None and the
caller tells Kunal on WhatsApp instead of dropping the note —
honest degradation, never silence.

Claude can't transcribe audio via the Messages API, which is why
this needs a dedicated STT key rather than reusing ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.facebook.com/v18.0"
_MAX_AUDIO_BYTES = 16 * 1024 * 1024  # WhatsApp voice notes are small


async def fetch_media(media_id: str, access_token: str) -> tuple[bytes, str] | None:
    """media_id → (audio_bytes, mime_type). Two-step Graph fetch."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            meta = await c.get(
                f"{_GRAPH}/{media_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if meta.status_code != 200:
                logger.warning("[voice] media meta %s: %s", meta.status_code, meta.text[:160])
                return None
            info = meta.json()
            url = info.get("url")
            mime = info.get("mime_type", "audio/ogg")
            if not url:
                return None
            # The download URL also requires the bearer token.
            blob = await c.get(url, headers={"Authorization": f"Bearer {access_token}"})
            if blob.status_code != 200 or len(blob.content) > _MAX_AUDIO_BYTES:
                logger.warning("[voice] media download %s / %d bytes", blob.status_code, len(blob.content))
                return None
            return blob.content, mime
    except Exception as e:
        logger.warning("[voice] media fetch failed: %s", e)
        return None


async def transcribe(audio: bytes, mime: str) -> str | None:
    """Audio bytes → text via a Whisper-compatible STT provider.

    Returns None when no provider key is configured or the call
    fails — the caller degrades honestly.
    """
    ext = "ogg" if "ogg" in mime else ("mp3" if "mpeg" in mime else "m4a")
    files = {"file": (f"note.{ext}", audio, mime)}

    groq = os.environ.get("GROQ_API_KEY", "").strip()
    if groq:
        return await _whisper_call(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            groq,
            "whisper-large-v3",
            files,
        )
    openai = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai:
        return await _whisper_call(
            "https://api.openai.com/v1/audio/transcriptions",
            openai,
            "whisper-1",
            files,
        )
    logger.info("[voice] no STT key (GROQ_API_KEY / OPENAI_API_KEY) — skipping")
    return None


async def _whisper_call(url: str, key: str, model: str, files: dict) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            r = await c.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                data={"model": model, "language": "en"},
                files=files,
            )
            if r.status_code != 200:
                logger.warning("[voice] STT %s: %s", r.status_code, r.text[:160])
                return None
            text = (r.json() or {}).get("text", "").strip()
            return text or None
    except Exception as e:
        logger.warning("[voice] STT call failed: %s", e)
        return None
