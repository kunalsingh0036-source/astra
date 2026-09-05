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
  §5  There is no approval tool. The chat-side token check that used
      to live here went with the tool it guarded (Phase A5,
      SECURITY-MODEL §1); the registry refuses the name at boot and
      tests/test_broker/test_no_approval_tool.py is the acceptance
      suite for that deletion.
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


def test_resolve_approval_is_absent_and_not_exempt():
    """Phase A5 inversion. Until A5 this asserted that the exemption
    list kept resolve_approval un-gated because it WAS the approval
    mechanism; its own docstring named the broker as its expiry. The
    broker's Touch ID lane is live, the tool is gone, and with it the
    exemption: the name is absent from the registry, a DESTRUCTIVE
    stand-in by that name asks like any other DESTRUCTIVE tool, and a
    stand-in with no tier fails closed. Nothing named
    resolve_approval is ever 'allow'."""
    import astra.runtime.tools  # noqa: F401
    from astra.autonomy.manager import autonomy_manager
    from astra.runtime.agent_loop import _autonomy_decide
    from astra.runtime.tool_registry import (
        REGISTRY,
        ActionTier as RegistryTier,
    )

    assert REGISTRY.get("resolve_approval") is None

    previous = autonomy_manager.mode
    try:
        for mode in (AutonomyMode.SEMI_AUTO, AutonomyMode.ALWAYS_ASK):
            autonomy_manager.set_mode(mode, reason="test")
            decision, reason = _autonomy_decide(
                _td(RegistryTier.DESTRUCTIVE), "resolve_approval"
            )
            assert decision == "ask", (mode, reason)
            decision, reason = _autonomy_decide(_td(None), "resolve_approval")
            assert decision == "deny", (mode, reason)
    finally:
        autonomy_manager.set_mode(previous, reason="test cleanup")


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
    # CHANGED 2026-09-04, deliberately tightened. This used to assert
    # None == "interactive" because the PWA relied on the default. That
    # meant ANY caller which forgot the field silently received the full
    # Mac-writing surface — least privilege backwards, and silent,
    # because omitting a field looks like nothing at all. astra-web now
    # sends channel:"web" explicitly and a test pins that.
    assert surface_for_channel(None) == "unattended"
    assert surface_for_channel("") == "unattended"
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
# §5: there is no approval tool (Phase A5, SECURITY-MODEL §1)
# ────────────────────────────────────────────────────────────
#
# The six tests that lived here exercised the chat-side token check
# inside the deleted approval tool, so the check has
# nothing to guard; what replaces it is structural. The registry
# refuses the name (tests/test_broker/test_no_approval_tool.py holds
# the boot-assertion, grep and red-team cases); this file keeps the
# same-shaped canary as §3's set_mode: the autonomy server itself
# must not offer the tool.

def test_approval_tools_not_in_autonomy_server():
    from astra.tools.autonomy_tools import create_autonomy_mcp_server

    server = create_autonomy_mcp_server()
    names = {t.name for t in server.tools}
    assert "resolve_approval" not in names, (
        "resolve_approval is back in the autonomy server: the model can "
        "approve its own actions again (SECURITY-MODEL §1)"
    )
    assert "revoke_tool_grant" not in names
    # The survivors must still be here, or the namespace silently lost
    # them (the CONTAINMENT §9 dead-capability class).
    assert {
        "get_mode", "get_audit_log", "audit_stats", "list_pending_approvals",
    } <= names, names


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


# ────────────────────────────────────────────────────────────
# §6 — the no-standing list (CHARTER: some things are approved
# per call or not at all)
# ────────────────────────────────────────────────────────────

def test_no_standing_list_covers_the_charter_categories():
    from astra.autonomy.approvals import NO_STANDING_TOOLS

    for name in (
        # shell / execution
        "local_bash", "Bash", "run_creator_tests",
        # sends + publishes
        "send_reply_draft", "approve_content_draft", "send_a2a_task",
        # self-modification + deploy
        "edit_astra_file", "write_astra_file", "local_edit",
        "local_write", "commit_code_changes", "commit_kit_changes",
        "apply_self_improvement",
        # deletes
        "forget_memory", "restart_agent",
        # permission surface
        "set_mode", "resolve_approval", "revoke_tool_grant",
        # exposes the machine
        "start_tunnel", "stop_tunnel",
    ):
        assert name in NO_STANDING_TOOLS, f"{name} must be no-standing"


def test_every_no_standing_tool_is_destructive():
    """The two lists must agree: anything too dangerous for a
    standing grant is too dangerous to auto-allow in semi_auto."""
    from astra.autonomy.approvals import NO_STANDING_TOOLS

    wrong = [
        n for n in NO_STANDING_TOOLS
        if n in TOOL_TIERS and TOOL_TIERS[n] is not ActionTier.DESTRUCTIVE
    ]
    assert wrong == [], f"no-standing but not DESTRUCTIVE: {wrong}"


def test_standing_grant_ignored_for_no_standing_tool():
    """A tool_grants row for local_bash (one exists in production,
    granted 2026-06-12 via chat) must not authorise anything."""
    from astra.autonomy import approvals

    class _FakeResult:
        def __init__(self, has_row):
            self._has_row = has_row

        def first(self):
            return object() if self._has_row else None

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, stmt, params=None):
            # First query = the tool_grants lookup → row EXISTS.
            # Second = the one-shot UPDATE → no unconsumed approval.
            sql = str(stmt)
            return _FakeResult("tool_grants" in sql)

        async def commit(self):
            return None

    async def _run():
        with mock.patch.object(
            approvals, "async_session", lambda: _FakeSession()
        ):
            return await approvals.check_grant("local_bash")

    granted, reason = asyncio.run(_run())
    assert granted is False, (
        "a standing grant on local_bash still authorised a call — "
        "the no-standing list is not being enforced on read"
    )
    assert reason == "no grant"


def test_standing_grant_still_works_for_ordinary_tools():
    from astra.autonomy import approvals

    class _FakeResult:
        def first(self):
            return object()

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def execute(self, stmt, params=None):
            return _FakeResult()

        async def commit(self):
            return None

    async def _run():
        with mock.patch.object(
            approvals, "async_session", lambda: _FakeSession()
        ):
            return await approvals.check_grant("notes_search")

    granted, reason = asyncio.run(_run())
    assert granted is True
    assert reason == "standing grant"


def test_approval_prompt_offers_no_chat_token_and_no_always_hint():
    """Phase A5 inversion. This test used to require the per-tool
    'add always' hint in the ASK result. With the approval tool gone,
    a chat token resolves nothing and 'always' is a choice made on
    the /approvals page, so any instruction to type either is the
    docstring-lies class in the other direction: telling Kunal to do
    something that no longer does anything. The ASK result must
    point at the human surface and nowhere else."""
    import inspect

    from astra.runtime import agent_loop

    src = inspect.getsource(agent_loop)
    assert "NO_STANDING_TOOLS" not in src, (
        "the ASK text consults the no-standing list again, which only "
        "made sense when it offered an 'always' hint"
    )
    assert "ONE CALL AT A TIME" not in src
    assert "add 'always'" not in src
    assert "approve {approval_id}" not in src, (
        "the ASK text tells the model a chat token can approve"
    )
    assert "_approvals_url()" in src
    assert agent_loop._approvals_url().endswith("/approvals")


# ────────────────────────────────────────────────────────────
# §8 — the bridge chokepoint (found 2026-08-30: the surface
# split was enforced in the agent loop only, and two production
# paths reached the Mac bridge without crossing it)
# ────────────────────────────────────────────────────────────

def _dispatch_on_surface(tool_name, surface, on_behalf_of=None):
    """Call local._dispatch with a given surface, with the bridge
    resolution mocked out so we only exercise the guard."""
    from astra.autonomy.turn_context import current_surface
    from astra.runtime.tools import local

    async def _no_bridge():
        return None, []          # bridge offline — never reached if refused

    async def _run():
        token = current_surface.set(surface)
        try:
            with mock.patch.object(
                local, "_active_bridge_token_id", _no_bridge
            ):
                return await local._dispatch(
                    tool_name, {"command": "echo hi"},
                    timeout_sec=5.0, on_behalf_of=on_behalf_of,
                )
        finally:
            current_surface.reset(token)

    return asyncio.run(_run())


def test_bridge_refuses_shell_on_unattended_surface():
    out = _dispatch_on_surface("local_bash", "unattended")
    assert out.get("is_error") is True
    assert "REFUSED" in out["content"][0]["text"]


def test_bridge_allows_shell_on_interactive_surface():
    """Interactive passes the guard and falls through to the bridge
    (which is mocked offline here) — the point is it is NOT refused
    by the surface check."""
    out = _dispatch_on_surface("local_bash", "interactive")
    assert "REFUSED" not in out["content"][0]["text"]


def test_bridge_default_surface_is_unattended():
    """Code paths outside a turn — schedulers, jobs — must be treated
    as unattended. The ContextVar default carries this."""
    from astra.autonomy.turn_context import current_surface

    assert current_surface.get() == "unattended"


def test_preauthorised_internal_caller_allowed_but_named():
    """notes_sync is a reviewed, fixed-argument job. It passes; an
    anonymous caller doing the same thing does not."""
    ok = _dispatch_on_surface("local_bash", "unattended",
                              on_behalf_of="notes_sync")
    assert "REFUSED" not in ok["content"][0]["text"]

    anon = _dispatch_on_surface("local_bash", "unattended",
                                on_behalf_of="something_new")
    assert anon.get("is_error") is True
    assert "REFUSED" in anon["content"][0]["text"]


def test_notes_sync_declares_itself_to_the_chokepoint():
    """The scheduler's 30-minute job must pass on_behalf_of — without
    it the call is anonymous and gets refused, silently freezing the
    Notes mirror (it froze at 50 once already)."""
    import inspect

    from astra.tools import notes_tools

    src = inspect.getsource(notes_tools)
    assert 'on_behalf_of="notes_sync"' in src


def test_reply_tools_declares_itself_to_the_chokepoint():
    import inspect

    from astra.tools import reply_tools

    src = inspect.getsource(reply_tools)
    assert 'on_behalf_of="ingest_voice_export"' in src


def test_no_anonymous_bridge_callers_remain():
    """Class check: nothing outside astra/runtime/tools/local.py may
    reach the bridge helpers without naming itself. A new anonymous
    caller re-opens the exact hole found on 2026-08-30."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for path in root.rglob("astra/**/*.py"):
        if ".venv" in str(path) or "__pycache__" in str(path):
            continue
        if path.name == "local.py" and "runtime/tools" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Negative lookbehind so `wa_dispatch(` and friends do not
        # masquerade as the bridge's `_dispatch(`.
        for m in re.finditer(
            r"(?<![A-Za-z0-9_])(?:local\.)?(?:_dispatch|local_bash_impl|"
            r"local_write_impl|local_edit_impl)\s*\(", text
        ):
            window = text[m.start():m.start() + 400]
            if "on_behalf_of" not in window:
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(root)}:{line}")
    assert offenders == [], (
        "anonymous bridge callers (must pass on_behalf_of=): "
        f"{offenders}"
    )


def test_screenshot_url_is_not_read_tier():
    """It spawns headless Chrome on the Mac. READ meant auto-allowed
    in semi_auto for a tool that starts a local process."""
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY

    td = REGISTRY.get("screenshot_url")
    assert td is not None
    assert td.tier is not td.tier.READ
    assert TOOL_TIERS["screenshot_url"] is not ActionTier.READ


def test_web_resolver_enforces_no_standing():
    """astra-web has its OWN resolver. It drifted from the Python one
    and wrote standing grants for local_bash. Both must agree."""
    import pathlib

    from astra.autonomy.approvals import NO_STANDING_TOOLS

    route = (
        pathlib.Path(__file__).resolve().parents[3]
        / "astra-web/app/api/approvals/[id]/resolve/route.ts"
    )
    if not route.exists():          # astra-web not checked out beside astra
        pytest.skip("astra-web not present")
    src = route.read_text()
    assert "NO_STANDING_TOOLS" in src, (
        "the web resolver does not know about the no-standing list — "
        "clicking 'approve N always' there grants what the Python "
        "resolver refuses"
    )
    missing = [t for t in NO_STANDING_TOOLS if f'"{t}"' not in src]
    assert missing == [], (
        f"web resolver's no-standing list is missing: {missing}"
    )


# ────────────────────────────────────────────────────────────
# §10 — two live bugs the Workstream A red team found in
# existing code (not in the design)
# ────────────────────────────────────────────────────────────

def test_approval_record_is_always_valid_json():
    """create_approval used `json.dumps(tool_input)[:20_000]` cast to
    JSONB. Slicing a serialised string mid-token yields invalid JSON,
    so Postgres rejected the INSERT and any tool call with arguments
    over ~20k chars could never be approved — it failed closed, but
    with a message blaming the approval store. A long shell command is
    exactly the kind of call most worth gating."""
    import json

    from astra.autonomy.approvals import _MAX_INPUT_JSON, _clamp_tool_input

    for case in (
        {},
        {"small": "ok"},
        {"command": "x" * 25_000},
        {"a": "y" * 30_000, "b": "z" * 30_000},
        {"nested": {"deep": ["v" * 40_000]}},
        {"n": 1, "flag": True, "none": None},
    ):
        out = _clamp_tool_input(case)
        json.loads(out)                      # must parse
        assert len(out) <= _MAX_INPUT_JSON   # must fit


def test_truncation_is_disclosed_not_silent():
    """An approval UI must never show less than what will run without
    saying so — otherwise Kunal approves a command he only half saw."""
    import json

    from astra.autonomy.approvals import _clamp_tool_input

    out = json.loads(_clamp_tool_input({"command": "x" * 25_000}))
    assert "__truncated__" in out
    assert "TRUNCATED" in out["command"]


def test_finalize_call_is_scoped_to_the_presenting_token():
    """/bridge/result took call_id straight from the request body with
    no ownership predicate, so any holder of any valid bridge token
    could write a result for any call — forging the outcome of work it
    never performed. Theoretical with one body; a live forgery path the
    moment a second body registers (WORKSTREAMS §G)."""
    import inspect

    from astra.runtime.bridge import store

    import re

    src = inspect.getsource(store.finalize_call)
    assert "bridge_token_id" in src, (
        "finalize_call no longer scopes to the presenting token"
    )
    # Assert the PROPERTY, not the spelling. The first version pinned
    # the literal "bridge_token_id = :tok" and went red when the cast
    # changed to CAST(:tok AS INTEGER) — a guard that fails on a
    # rewording trains people to edit the guard. What must hold is that
    # the UPDATE's WHERE clause constrains bridge_token_id at all.
    where = src[src.find("WHERE"):] if "WHERE" in src else ""
    assert re.search(r"bridge_token_id\s*=", where), (
        "the UPDATE lost its ownership predicate — any bridge token "
        "could finalize any call"
    )
    # And the parameter must actually be bound. An unbound :tok is a
    # 500 on every request, which is exactly what shipped once.
    assert '"tok"' in src, (
        "the :tok parameter is referenced but never bound — this is a "
        "guaranteed 500 on every call, not a subtle bug"
    )


def test_bridge_result_route_passes_the_token_and_fails_loudly():
    import pathlib

    main = (
        pathlib.Path(__file__).resolve().parents[2]
        / "services/stream/main.py"
    ).read_text()
    idx = main.find("async def bridge_result")
    assert idx != -1
    body = main[idx:idx + 2000]
    assert "bridge_token_id=bt.id" in body, (
        "the route does not scope the finalize to the caller's token"
    )
    assert "404" in body, (
        "a rejected finalize must fail loudly, not return ok:true"
    )
