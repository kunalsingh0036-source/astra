"""
Whisper.cpp wrapper.

Uses pywhispercpp (which ships whisper.cpp's Metal backend on macOS
— we saw 'Apple M4 Pro' detected in the smoke test). Models are
downloaded to ~/.cache/whisper on first use.

Why `base.en` as default:
  - ~140MB model, ~5x realtime on M4 Pro
  - good enough for 1-on-1 business meetings
  - `tiny.en` drops too many words on Indian accents
  - `small.en` is higher quality (~500MB) but 2x slower — opt-in via env

Non-audio files are rejected cheaply (extension check only; we don't
probe with ffprobe to keep this lightweight).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".mp4", ".mov", ".flac", ".ogg", ".webm", ".aiff", ".aif"}

# Model is cacheable at module level — first call pays the load cost.
_MODEL: Any | None = None


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return _MODEL
    from pywhispercpp.model import Model

    name = os.environ.get("ASTRA_WHISPER_MODEL", "base.en")
    logger.info("[meetings] loading whisper model: %s", name)
    _MODEL = Model(
        name,
        n_threads=max(4, (os.cpu_count() or 8) // 2),
        print_progress=False,
        print_realtime=False,
        # Better defaults for business-meeting content
        language="en",
    )
    return _MODEL


@dataclass
class Transcript:
    text: str
    segments: list[dict]   # {t0, t1, text} — seconds, text
    duration_s: float
    model_used: str


def _to_wav_16khz_mono(src: Path) -> Path:
    """Convert any supported audio/video file to 16 kHz mono 16-bit WAV
    using macOS `afconvert` (built-in, no brew/ffmpeg needed).

    Returns the original path if it's already .wav (assumed whisper-ready;
    if its sample rate is wrong whisper.cpp will resample internally).
    """
    if src.suffix.lower() == ".wav":
        return src

    tmp_dir = Path(tempfile.gettempdir())
    out = tmp_dir / f"astra-whisper-{os.getpid()}-{src.stem}.wav"

    cmd = [
        "/usr/bin/afconvert",
        "-f", "WAVE",
        "-d", "LEI16@16000",  # 16-bit little-endian, 16 kHz
        "-c", "1",            # mono
        str(src),
        str(out),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            "afconvert not found (macOS built-in). Is this a Mac?"
        ) from e
    if result.returncode != 0:
        raise RuntimeError(
            f"afconvert failed rc={result.returncode}: "
            f"{result.stderr.strip()[:400]}"
        )
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("afconvert produced no output")
    return out


def transcribe_file(path: str) -> Transcript:
    """Run whisper.cpp over the file and return the full transcript.

    If the file isn't already a WAV, we transcode to 16 kHz mono WAV
    via macOS afconvert first. Temp WAVs are cleaned up after use.

    Raises on codec / whisper errors so the caller can mark the row
    'error' with a real reason.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    if p.suffix.lower() not in AUDIO_EXTS:
        raise ValueError(f"unsupported extension: {p.suffix}")

    wav_path = _to_wav_16khz_mono(p)
    cleanup = wav_path != p

    try:
        model = _load_model()
        segs = model.transcribe(str(wav_path))
    finally:
        if cleanup:
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass

    parts: list[dict] = []
    for s in segs:
        # pywhispercpp times are centiseconds.
        parts.append(
            {
                "t0": float(s.t0) / 100.0,
                "t1": float(s.t1) / 100.0,
                "text": s.text.strip(),
            }
        )

    full_text = "\n".join(s["text"] for s in parts if s["text"])
    duration = parts[-1]["t1"] if parts else 0.0
    model_name = os.environ.get("ASTRA_WHISPER_MODEL", "base.en")

    return Transcript(
        text=full_text,
        segments=parts,
        duration_s=duration,
        model_used=f"whisper.cpp:{model_name}",
    )


def _reset_meeting_for_retry(source_path: str) -> None:
    """Utility used by pipeline retries — drops the row so the watcher
    can re-pick it up. Not wired by default."""
    # Intentionally no-op here; the pipeline handles state reset.
    pass
