"""The macOS platform gate must not strand cloud-capable jobs.

notes_sync sat inside `if _IS_MACOS:` in astra/scheduler/app.py while
its body (jobs.py::notes_sync) had been rewritten to detect the cloud
and route through the Mac bridge (since retired in Phase A6 for a
`notes.sync` intent through the capability broker; the gate lesson is
the same). The scheduler runs on Railway
(Linux), so the job was never registered and the rewritten body never
executed. Verified 2026-08-30: apple_notes.last_synced_at newest was
2026-07-25 — 36 days stale, frozen at 54 rows, exactly as the same
mirror froze at 50 once before.

The class: a platform gate is a claim about what a job NEEDS. When a
job's body gains a cloud path, the gate becomes a lie that fails
silently — the job simply is not there, and nothing reports a job that
was never scheduled.
"""

from __future__ import annotations

import sys
from unittest import mock

import pytest


def _job_ids(platform: str) -> list[str]:
    # Import first so every dependency resolves under the real
    # platform; patching sys.platform before import breaks sysconfig.
    from astra.scheduler.app import _build_scheduler

    with mock.patch.object(sys, "platform", platform):
        return sorted(j.id for j in _build_scheduler().get_jobs())


def test_notes_sync_registered_on_linux():
    """The cloud scheduler is Linux. If this fails, the Apple Notes
    mirror silently stops updating and nothing alerts."""
    assert "notes_sync" in _job_ids("linux")


def test_notes_sync_registered_on_macos_too():
    assert "notes_sync" in _job_ids("darwin")


def test_genuinely_macos_only_jobs_stay_gated():
    """These shell out to osascript or watch laptop filesystem paths
    with no cloud path. They must NOT leak onto the Linux scheduler —
    that is the fire-forever-fail-forever bug the gate exists for."""
    linux = set(_job_ids("linux"))
    mac_only = {
        "missed_session_snapshot",
        "apply_approved_catchups",
        "meetings_pipeline",
        "meeting_capture_trigger",
    }
    leaked = sorted(mac_only & linux)
    assert leaked == [], f"macOS-only jobs registered on Linux: {leaked}"


def test_macos_registers_a_superset():
    """Whatever Linux gets, macOS must also get — a job available only
    in the cloud would be an unnoticed asymmetry."""
    linux, mac = set(_job_ids("linux")), set(_job_ids("darwin"))
    missing = sorted(linux - mac)
    assert missing == [], f"registered on Linux but not macOS: {missing}"


def test_skip_log_does_not_still_claim_notes_sync():
    """The skip message listed notes_sync among the jobs it drops.
    A stale log line is how this stayed invisible."""
    import inspect

    from astra.scheduler import app

    src = inspect.getsource(app)
    skip_msg_start = src.find("skipping")
    assert skip_msg_start != -1
    window = src[skip_msg_start:skip_msg_start + 600]
    assert "NO LONGER" in window, (
        "the skip log must state that notes_sync is not among the "
        "skipped jobs — otherwise the next reader re-learns the bug"
    )


def test_broker_reaper_is_registered_on_every_platform():
    """The reaper runs in the CLOUD precisely because the body may be
    the thing that is missing. Without it an intent sits in 'claimed'
    forever, and that state is indistinguishable from "the broker died
    mid-work" — CHARTER §8's silent no-op, enforced by the component
    that is absent in exactly this failure."""
    for platform in ("linux", "darwin"):
        assert "broker_reap" in _job_ids(platform), (
            f"broker_reap is not registered on {platform}"
        )
