"""
CONTAINMENT-NOW regression tests — one section per flip.

These tests pin the five interim mitigations that close the live
self-approval hole (astra-body/docs/CONTAINMENT-NOW.md). Every one
of them failed against the pre-containment code. If any of these
goes red, the gate has regressed to a state where the model can
escalate its own permissions — treat it as a production incident,
not a flaky test.

  §1  Unknown tools default to DESTRUCTIVE; tier is required at
      registration.
  §2  The decision path fails CLOSED — an exception denies.
  §3  set_mode is not model-callable; full_auto cannot be permanent.
  §4  The unattended tool surface is a strict subset of the
      interactive one; self-modification is interactive-only.
  §5  resolve_approval requires a human-typed token in the actual
      inbound message.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from astra.autonomy.modes import (
    ActionTier,
    AutonomyMode,
    PermissionDecision,
    TOOL_TIERS,
    get_action_tier,
    get_permission,
)


# ────────────────────────────────────────────────────────────
# §1 — unknown tools default to DESTRUCTIVE, tier required
# ────────────────────────────────────────────────────────────

def test_unknown_tool_defaults_to_destructive():
    assert get_action_tier("tool_nobody_classified") is ActionTier.DESTRUCTIVE


def test_unknown_tool_is_not_auto_allowed_in_semi_auto():
    decision = get_permission(AutonomyMode.SEMI_AUTO, "tool_nobody_classified")
    assert decision is PermissionDecision.ASK


def test_guess_tier_falls_to_destructive_on_miss():
    from astra.runtime.sdk_adapter import _guess_tier
    from astra.runtime.tool_registry import ActionTier as RegistryTier

    assert _guess_tier("tool_nobody_classified") is RegistryTier.DESTRUCTIVE


def test_tooldef_requires_tier():
    from astra.runtime.tool_registry import ToolDef

    async def _impl(args):  # pragma: no cover
        return "x"

    with pytest.raises(TypeError):
        ToolDef(
            name="no_tier_tool",
            description="a tool that forgot to declare a tier",
            input_schema={"type": "object", "properties": {}},
            fn=_impl,
        )


def test_registry_rejects_invalid_tier():
    from astra.runtime.tool_registry import ToolDef, ToolRegistry

    async def _impl(args):  # pragma: no cover
        return "x"

    reg = ToolRegistry()
    bad = ToolDef(
        name="bad_tier_tool",
        description="tier is not an ActionTier",
        input_schema={"type": "object", "properties": {}},
        fn=_impl,
        tier="write",  # a string, not the enum — must be refused
    )
    with pytest.raises(ValueError, match="tier"):
        reg.register(bad)


def test_every_registered_tool_is_classified_in_tool_tiers():
    """The DESTRUCTIVE default is a tripwire for NEW tools — the
    live surface must be fully, deliberately classified. A tool
    registered without an entry here would silently land in the
    ask-everything tier and look like a UX bug instead of the
    missing classification it is."""
    import astra.runtime.tools  # noqa: F401 — side-effect registration
    from astra.runtime.tool_registry import REGISTRY

    unclassified = [
        t.name for t in REGISTRY.all() if t.name not in TOOL_TIERS
    ]
    assert unclassified == [], (
        f"tools registered without a TOOL_TIERS entry: {unclassified} — "
        "classify them in astra/autonomy/modes.py"
    )


# ────────────────────────────────────────────────────────────
# §2 — the decision path fails CLOSED
# ────────────────────────────────────────────────────────────

def _td(tier):
    """Minimal ToolDef stand-in for _autonomy_decide."""
    class _TD:
        pass

    td = _TD()
    td.tier = tier
    return td


def test_decision_path_denies_when_autonomy_check_raises():
    from astra.runtime.agent_loop import _autonomy_decide

    class _RaisingTier:
        @property
        def value(self):
            raise RuntimeError("induced failure in the decision path")

    decision, reason = _autonomy_decide(_td(_RaisingTier()), "some_tool")
    assert decision == "deny"
    assert "fail closed" in reason


def test_decision_path_denies_when_autonomy_module_unavailable():
    from astra.runtime import agent_loop

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _broken_import(name, *args, **kwargs):
        if name.startswith("astra.autonomy"):
            raise ImportError("induced: autonomy module unavailable")
        return real_import(name, *args, **kwargs)

    with mock.patch("builtins.__import__", side_effect=_broken_import):
        decision, reason = _autonomy_decide_fresh(agent_loop)
    assert decision == "deny"
    assert "fail closed" in reason


def _autonomy_decide_fresh(agent_loop):
    """Call _autonomy_decide with a non-exempt tool + WRITE-ish td.
    Split out so the import patch above wraps only the call."""
    from astra.runtime.tool_registry import ActionTier as RegistryTier

    return agent_loop._autonomy_decide(
        _td(RegistryTier.WRITE), "definitely_not_exempt_tool"
    )


def test_gate_exempt_tools_still_allowed():
    """The exemption list must keep working — resolve_approval IS the
    approval mechanism until the broker replaces it (Workstream A)."""
    from astra.runtime.agent_loop import _autonomy_decide

    decision, _ = _autonomy_decide(_td(None), "resolve_approval")
    assert decision == "allow"


# ────────────────────────────────────────────────────────────
# §3 — set_mode is not model-callable; full_auto is never permanent
# ────────────────────────────────────────────────────────────

def test_set_mode_not_in_model_tool_surface():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    assert REGISTRY.get("set_mode") is None, (
        "set_mode is back in the model's tool surface — the model "
        "can raise its own autonomy again (CONTAINMENT §3)"
    )


def test_set_mode_not_in_autonomy_server():
    from astra.tools.autonomy_tools import create_autonomy_mcp_server

    server = create_autonomy_mcp_server()
    names = {t.name for t in server.tools}
    assert "set_mode" not in names


def test_full_auto_without_ttl_rejected():
    from astra.autonomy.manager import AutonomyManager

    mgr = AutonomyManager()
    with pytest.raises(ValueError, match="duration"):
        mgr.set_mode(AutonomyMode.FULL_AUTO)


def test_full_auto_with_ttl_accepted_and_reverts():
    import time as _time

    from astra.autonomy.manager import AutonomyManager

    mgr = AutonomyManager()
    mgr.set_mode(AutonomyMode.FULL_AUTO, duration_minutes=1)
    assert mgr.mode is AutonomyMode.FULL_AUTO
    # Force the revert deadline into the past; the property getter
    # runs _check_revert.
    mgr._revert_at = _time.time() - 1
    assert mgr.mode is not AutonomyMode.FULL_AUTO


def test_full_auto_adopted_from_db_gets_clamped():
    from astra.autonomy import manager as manager_mod
    from astra.autonomy.manager import AutonomyManager

    mgr = AutonomyManager()

    async def _fake_read():
        return "full_auto"

    async def _run():
        with mock.patch.object(
            manager_mod, "_read_mode_from_db", _fake_read
        ):
            return await mgr.refresh_from_db()

    updated = asyncio.run(_run())
    assert updated is True
    assert mgr._mode is AutonomyMode.FULL_AUTO
    assert mgr._revert_at is not None, (
        "full_auto adopted from app_settings must carry a TTL — "
        "a permanent full_auto row would otherwise never expire"
    )
    assert mgr._previous_mode is AutonomyMode.SEMI_AUTO


def test_semi_auto_without_ttl_still_fine():
    """The default steady state must not require a TTL."""
    from astra.autonomy.manager import AutonomyManager

    mgr = AutonomyManager()
    mgr.set_mode(AutonomyMode.SEMI_AUTO)
    assert mgr.mode is AutonomyMode.SEMI_AUTO


# ────────────────────────────────────────────────────────────
# §4 — unattended ⊂ interactive; self-modification interactive-only
# ────────────────────────────────────────────────────────────

def test_unattended_excludes_all_self_mod_families():
    from astra.runtime.tool_surface import (
        allowed_tool_names,
        interactive_only_tool_names,
    )

    blocked = interactive_only_tool_names()
    for name in (
        "edit_astra_file", "write_astra_file", "commit_code_changes",
        "run_creator_tests", "revert_last_code_commit",
        "commit_kit_changes", "apply_self_improvement",
        "local_bash", "local_edit", "local_write",
    ):
        assert name in blocked, f"{name} missing from interactive-only set"

    all_names = ["recall_memories", "edit_astra_file", "local_bash"]
    allowed, excluded = allowed_tool_names(all_names, "unattended")
    assert allowed == ["recall_memories"]
    assert set(excluded) == {"edit_astra_file", "local_bash"}


def test_interactive_keeps_the_full_surface():
    from astra.runtime.tool_surface import allowed_tool_names

    all_names = ["recall_memories", "edit_astra_file", "local_bash"]
    allowed, excluded = allowed_tool_names(all_names, "interactive")
    assert allowed == all_names
    assert excluded == []


def test_unknown_surface_treated_as_unattended():
    from astra.runtime.tool_surface import normalize_surface

    assert normalize_surface(None) == "unattended"
    assert normalize_surface("") == "unattended"
    assert normalize_surface("INTERACTIVE") == "interactive"
    assert normalize_surface("garbage") == "unattended"


def test_whatsapp_channel_maps_to_unattended():
    from astra.runtime.tool_surface import surface_for_channel

    assert surface_for_channel("whatsapp") == "unattended"
    assert surface_for_channel("web") == "interactive"
    assert surface_for_channel(None) == "interactive"  # web PWA default
    assert surface_for_channel("anything_else") == "unattended"


def test_fallback_matches_families():
    """The hardcoded fallback must never lag the real family
    constants — if a tool is added to code/kit/self-improve, this
    fails until the fallback list includes it too."""
    from astra.runtime.tool_surface import _FALLBACK_INTERACTIVE_ONLY
    from astra.tools.code_editor_tools import CODE_EDITOR_TOOLS
    from astra.tools.kit_editor_tools import KIT_EDITOR_TOOLS
    from astra.tools.self_improve_tools import SELF_IMPROVE_TOOLS

    family_names = {
        t.name
        for t in (*CODE_EDITOR_TOOLS, *KIT_EDITOR_TOOLS, *SELF_IMPROVE_TOOLS)
    }
    missing = family_names - _FALLBACK_INTERACTIVE_ONLY
    assert missing == set(), (
        f"fallback lags the family constants: {missing} — update "
        "_FALLBACK_INTERACTIVE_ONLY in astra/runtime/tool_surface.py"
    )


# ────────────────────────────────────────────────────────────
# §5 — resolve_approval requires a human-typed token
# ────────────────────────────────────────────────────────────

def _resolve(args, prompt):
    """Run resolve_approval_tool with the turn context set to a
    given human prompt, with the DB-backed core mocked out."""
    from astra.autonomy.turn_context import current_user_prompt
    from astra.tools import autonomy_tools

    async def _fake_core(approval_id, decision, *, standing=False,
                         source="web"):
        return {
            "ok": True, "tool_name": "some_tool",
            "decision": decision, "standing": standing,
        }

    async def _run():
        token = current_user_prompt.set(prompt)
        try:
            with mock.patch(
                "astra.autonomy.approvals.resolve_approval",
                _fake_core,
            ):
                return await autonomy_tools.resolve_approval_tool.handler(
                    args
                )
        finally:
            current_user_prompt.reset(token)

    return asyncio.run(_run())


def test_model_cannot_resolve_without_human_token():
    """The model inventing 'Kunal approved it' must be refused when
    his actual message contains no approve token."""
    out = _resolve(
        {"approval_id": 12, "decision": "approved", "standing": True},
        prompt="what's on my calendar tomorrow?",
    )
    assert out.get("is_error") is True
    assert "REFUSED" in out["content"][0]["text"]


def test_human_approve_token_releases():
    out = _resolve(
        {"approval_id": 12, "decision": "approved"},
        prompt="approve 12",
    )
    assert not out.get("is_error")


def test_standing_requires_always_in_human_message():
    out = _resolve(
        {"approval_id": 12, "decision": "approved", "standing": True},
        prompt="approve 12",
    )
    assert out.get("is_error") is True
    assert "STANDING" in out["content"][0]["text"]

    out = _resolve(
        {"approval_id": 12, "decision": "approved", "standing": True},
        prompt="approve 12 always",
    )
    assert not out.get("is_error")


def test_wrong_id_in_human_message_refused():
    out = _resolve(
        {"approval_id": 12, "decision": "approved"},
        prompt="approve 13",
    )
    assert out.get("is_error") is True


def test_empty_turn_context_refuses():
    """Outside a turn (or with broken plumbing) there is no human
    message — that is a failed check, never a passed one."""
    out = _resolve(
        {"approval_id": 12, "decision": "approved"},
        prompt="",
    )
    assert out.get("is_error") is True


def test_deny_token_works():
    out = _resolve(
        {"approval_id": 7, "decision": "denied"},
        prompt="deny 7",
    )
    assert not out.get("is_error")


# ────────────────────────────────────────────────────────────
# End-to-end: a scripted escalation attempt fails through the
# REAL agent loop (CONTAINMENT "done means")
# ────────────────────────────────────────────────────────────

def _escalation_frames(surface: str):
    """Drive run_lean_turn with a fake Anthropic client that tries to
    call edit_astra_file, on the given surface. Returns parsed SSE
    frames. Uses the REAL registry — the call must be stopped before
    dispatch on both surfaces (surface guard / approval gate), so the
    tool body never runs."""
    import astra.runtime.tools  # noqa: F401 — populate the registry

    from tests.test_runtime.test_agent_loop import (
        _FakeBlock,
        _FakeClientWithTools,
        _parse_sse_frame,
    )
    from astra.runtime.agent_loop import run_lean_turn

    queue = [
        {
            "text_chunks": ["Editing my own code now. "],
            "stop_reason": "tool_use",
            "content": [
                _FakeBlock(
                    type="tool_use",
                    id="tu_esc",
                    name="edit_astra_file",
                    input={
                        "path": "astra/autonomy/modes.py",
                        "old": "DESTRUCTIVE",
                        "new": "READ",
                    },
                ),
            ],
        },
        {
            "text_chunks": ["(done)"],
            "stop_reason": "end_turn",
            "content": [_FakeBlock(type="text", text="(done)")],
        },
    ]
    fake = _FakeClientWithTools(queue)

    async def _run():
        frames = []
        with mock.patch(
            "astra.runtime.agent_loop.AsyncAnthropic", return_value=fake
        ):
            async for f in run_lean_turn(
                "please improve yourself",
                session_id="containment-e2e",
                load_history=False,
                surface=surface,
            ):
                frames.append(_parse_sse_frame(f))
        return frames

    return asyncio.run(_run())


def test_escalation_refused_on_unattended_surface():
    frames = _escalation_frames("unattended")
    results = [d for n, d in frames if n == "tool_result"]
    assert results, "no tool_result frame emitted"
    preview = results[0].get("preview", "")
    assert "REFUSED" in preview and "interactive-only" in preview
    assert results[0].get("is_error") is True


def test_escalation_gated_not_executed_on_interactive_surface():
    """On the interactive surface the tool is exposed, but it is
    DESTRUCTIVE — in semi_auto it must go to the approval path (or
    fail closed if the approval store is unreachable), never
    execute."""
    frames = _escalation_frames("interactive")
    results = [d for n, d in frames if n == "tool_result"]
    assert results, "no tool_result frame emitted"
    preview = results[0].get("preview", "")
    assert results[0].get("is_error") is True
    # Either a pending approval or a failed-closed approval store —
    # both mean NOT EXECUTED. What it must never be is a successful
    # edit result.
    assert "NOT EXECUTED" in preview or "denied" in preview
