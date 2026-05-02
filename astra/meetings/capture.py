"""
Audio-capture process management.

Wraps the ScreenCaptureKit-based Swift binary at ~/Astra/tools/astra-capture.
Each capture runs as a subprocess; we track its PID so we can SIGTERM it
when the calendar says the meeting's over.

Outputs land in ~/Astra/recordings/ so Phase 1 pipeline auto-picks them up.

Permission note:
  First run triggers (or denies) macOS Screen Recording permission. If TCC
  denies, the binary exits with a non-zero code and a line on stderr —
  we translate that into a clear error dict the scheduler logs.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


ASTRA_HOME = Path(os.path.expanduser("~/Astra"))
CAPTURE_BINARY = ASTRA_HOME / "tools" / "astra-capture"
RECORDINGS_DIR = ASTRA_HOME / "recordings"


def is_available() -> bool:
    """True if the Swift binary exists + is executable."""
    return CAPTURE_BINARY.exists() and os.access(CAPTURE_BINARY, os.X_OK)


def _safe_title(event_summary: str) -> str:
    """Filesystem-safe version of an event title."""
    safe = "".join(c if c.isalnum() or c in "-_ " else "-" for c in event_summary)
    return safe.strip().replace(" ", "-")[:80] or "meeting"


def output_path_for(event_google_id: str, summary: str, start_utc: datetime) -> Path:
    """Deterministic filename — UTC timestamp + event id (short) + title."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = start_utc.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    gid_short = event_google_id[:12] if event_google_id else "adhoc"
    title = _safe_title(summary)
    return RECORDINGS_DIR / f"{ts}_{gid_short}_{title}.m4a"


def start_capture(
    output: Path,
    max_seconds: int,
) -> dict[str, Any]:
    """Spawn astra-capture as a detached child. Returns PID + path.

    The child survives this Python process exiting (setsid). The caller
    stores the PID and kills it when the meeting ends.
    """
    if not is_available():
        return {
            "status": "error",
            "error": (
                f"astra-capture binary missing at {CAPTURE_BINARY}. "
                "Rebuild: cd ~/Astra/tools && swiftc -O capture.swift -o astra-capture"
            ),
        }

    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(CAPTURE_BINARY),
        "--output", str(output),
        "--max-seconds", str(max_seconds),
    ]
    # Start in its own session so it survives us; redirect stderr to a
    # logfile next to the recording so failures are debuggable.
    log_path = output.with_suffix(".stderr.log")
    try:
        log_fh = open(log_path, "ab")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        return {"status": "error", "error": f"spawn failed: {e}"}

    logger.info(
        "[capture] started pid=%s max=%ss → %s",
        proc.pid, max_seconds, output.name,
    )
    return {
        "status": "started",
        "pid": proc.pid,
        "output_path": str(output),
        "stderr_log": str(log_path),
        "max_seconds": max_seconds,
    }


def stop_capture(pid: int, grace_seconds: float = 4.0) -> dict[str, Any]:
    """SIGTERM the capture process; the Swift binary catches it and
    finalizes the m4a cleanly. Falls back to SIGKILL after `grace_seconds`.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"status": "already_exited"}
    except PermissionError as e:
        return {"status": "error", "error": f"cannot signal pid {pid}: {e}"}

    # Poll briefly — the binary should finalize within a second or two.
    import time as _time
    deadline = _time.monotonic() + grace_seconds
    while _time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return {"status": "terminated"}
        _time.sleep(0.2)

    # Still alive — force kill.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return {"status": "terminated_late"}
    return {"status": "killed"}


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True
