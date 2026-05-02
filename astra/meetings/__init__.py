"""Meetings — audio → transcript → summary → action items pipeline.

Phase 1: file-drop. Kunal records via macOS cmd+shift+5 (or any tool)
and drops the file into ~/Astra/recordings/. The scheduler picks it
up, transcribes with whisper.cpp, summarizes with Claude, and stages
action items as tasks — all with approval gates where appropriate.
"""

from astra.meetings.pipeline import scan_and_process
from astra.meetings.transcriber import transcribe_file
from astra.meetings.summarizer import summarize_transcript

__all__ = [
    "scan_and_process",
    "transcribe_file",
    "summarize_transcript",
]
