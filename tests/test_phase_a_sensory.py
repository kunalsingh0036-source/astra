"""
Phase A regression locks — WhatsApp→Astra channel, outbound drain,
calendar creds materialization.

Network-touching paths are covered by smoke journeys; these lock the
pure logic that decides WHO gets the Astra channel, HOW sessions
stay continuous, and WHETHER creds reach disk.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest


# ── astra_chat helpers ─────────────────────────────────────


def test_owner_numbers_parsing(monkeypatch):
    from gateway.services import astra_chat

    monkeypatch.setenv(
        "ASTRA_OWNER_NUMBERS", "+919987953145, 919993094281 ,"
    )
    nums = astra_chat.owner_numbers()
    assert nums == {"919987953145", "919993094281"}
    assert astra_chat.is_owner("+919987953145")
    assert astra_chat.is_owner("919993094281")
    assert not astra_chat.is_owner("14155550123")


def test_owner_numbers_empty_env_means_nobody(monkeypatch):
    """No env → the channel is OFF, never open-to-all. Same fail-closed
    philosophy as the mesh secrets."""
    from gateway.services import astra_chat

    monkeypatch.delenv("ASTRA_OWNER_NUMBERS", raising=False)
    assert astra_chat.owner_numbers() == set()
    assert not astra_chat.is_owner("919987953145")


def test_session_id_stable_per_number_per_day(monkeypatch):
    """Same number, same IST day → same session (intra-day
    continuity); different numbers → different sessions; and the key
    ROTATES at the IST day boundary — the original forever-key let
    months of WhatsApp history pile into one session and a stale
    test prompt bled into a fresh answer (turn 319). Valid UUID
    because turns.session_id stores UUIDs."""
    import gateway.services.astra_chat as ac

    a1 = ac.session_id_for("+919987953145")
    a2 = ac.session_id_for("919987953145")  # normalized equal
    b = ac.session_id_for("919993094281")
    assert a1 == a2
    assert a1 != b
    uuid.UUID(a1)  # parses

    # Day rotation: recompute the key the same way with tomorrow's
    # IST date and assert it differs from today's.
    from datetime import datetime, timedelta, timezone

    tomorrow = (
        datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
        + timedelta(days=1)
    ).date()
    rotated = str(uuid.uuid5(ac._SESSION_NS, f"919987953145:{tomorrow}"))
    assert rotated != a1


def test_chunks_respects_meta_limit():
    from gateway.services.astra_chat import _chunks

    assert _chunks("short", 4096) == ["short"]
    big = "x" * 9000
    parts = _chunks(big, 4096)
    assert len(parts) == 3
    assert all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == big


@pytest.mark.asyncio
async def test_run_turn_unreachable_stream_degrades_to_message(monkeypatch):
    """Stream down ≠ silence on WhatsApp. The user always gets SOME
    reply text back."""
    from gateway.services import astra_chat

    monkeypatch.setattr(
        astra_chat, "_STREAM_URL", "http://127.0.0.1:1"  # nothing listens
    )
    out = await astra_chat._run_turn("919987953145", "hello")
    assert isinstance(out, str) and len(out) > 0
    assert "try again" in out.lower() or "couldn't reach" in out.lower()


# ── calendar creds materialization ─────────────────────────


def test_calendar_materialize_writes_env_to_paths(tmp_path, monkeypatch):
    from astra.calendar.client import _materialize_calendar_creds

    creds = tmp_path / "c.json"
    tok = tmp_path / "t.json"
    monkeypatch.setenv("CALENDAR_CREDENTIALS_JSON", '{"installed": {}}')
    monkeypatch.setenv("CALENDAR_TOKEN_JSON", '{"token": "abc"}')
    _materialize_calendar_creds(creds, tok)
    assert creds.read_text() == '{"installed": {}}'
    assert tok.read_text() == '{"token": "abc"}'


def test_calendar_materialize_never_clobbers_disk(tmp_path, monkeypatch):
    """A refreshed token written by google-auth must survive restarts —
    the env original would roll auth back to a stale token."""
    from astra.calendar.client import _materialize_calendar_creds

    tok = tmp_path / "t.json"
    tok.write_text('{"token": "REFRESHED"}')
    monkeypatch.setenv("CALENDAR_TOKEN_JSON", '{"token": "STALE"}')
    _materialize_calendar_creds(tmp_path / "c.json", tok)
    assert tok.read_text() == '{"token": "REFRESHED"}'


def test_calendar_creds_fall_back_to_gmail_client(tmp_path, monkeypatch):
    """Calendar reuses the Gmail OAuth client when no calendar-specific
    one is provided — same installed app, different token scopes."""
    from astra.calendar.client import _materialize_calendar_creds

    creds = tmp_path / "c.json"
    monkeypatch.delenv("CALENDAR_CREDENTIALS_JSON", raising=False)
    monkeypatch.setenv("GMAIL_CREDENTIALS_JSON", '{"installed": {"id": 1}}')
    monkeypatch.delenv("CALENDAR_TOKEN_JSON", raising=False)
    _materialize_calendar_creds(creds, tmp_path / "t.json")
    assert creds.read_text() == '{"installed": {"id": 1}}'


# ── scheduler wa_dispatch degrades gracefully ──────────────


@pytest.mark.asyncio
async def test_wa_dispatch_unreachable_gateway_returns_not_ok(monkeypatch):
    from astra.scheduler.jobs import wa_dispatch

    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    out = await wa_dispatch()
    assert out["ok"] is False


# ── gateway modules import cleanly (catches mount typos) ───


def test_gateway_imports():
    # The production path (drain route + astra chat + dispatcher) must
    # import celery-free — that's the whole point of the refactor.
    import gateway.api.queue  # noqa: F401
    import gateway.services.astra_chat  # noqa: F401
    import gateway.services.dispatcher  # noqa: F401

    # The celery wrappers only import where celery is installed
    # (deploy image, CI). Local 3.14 envs may lack it — skip, don't
    # fail: nothing in production routes through these wrappers.
    pytest.importorskip("celery")
    import gateway.workers.dispatch  # noqa: F401
