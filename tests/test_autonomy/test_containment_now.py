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
  §8  There is no bridge. The chokepoint tests that lived here
      (surface guard on local._dispatch, on_behalf_of, the
      token-scoped finalize) guarded code that Phase A6 deleted; what
      replaces them are absence assertions: the modules are gone,
      nothing imports them, the routes are gone, the names are
      absent from the registry and every policy map, and the broker's
      status route is scoped to the presenting body.
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
    ):
        assert name in blocked, f"{name} missing from interactive-only set"

    all_names = ["recall_memories", "edit_astra_file", "submit_intent"]
    allowed, excluded = allowed_tool_names(all_names, "unattended")
    assert allowed == ["recall_memories", "submit_intent"]
    assert set(excluded) == {"edit_astra_file"}


def test_interactive_only_set_names_only_registered_tools():
    """Phase A6. The set used to carry the three Mac-bridge writing
    verbs; they are gone from the registry, and a surface rule about a
    tool that cannot register is a rule about nothing, which the next
    reader takes as evidence the tool exists."""
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY, _FORBIDDEN
    from astra.runtime.tool_surface import _EXTRA_INTERACTIVE_ONLY

    names = set(REGISTRY.names())
    assert not (set(_EXTRA_INTERACTIVE_ONLY) & _FORBIDDEN)
    unregistered = sorted(set(_EXTRA_INTERACTIVE_ONLY) - names)
    assert unregistered == [], (
        f"_EXTRA_INTERACTIVE_ONLY names tools that do not exist: "
        f"{unregistered}"
    )


def test_interactive_keeps_the_full_surface():
    from astra.runtime.tool_surface import allowed_tool_names

    all_names = ["recall_memories", "edit_astra_file", "submit_intent"]
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
        "Bash", "run_creator_tests",
        # sends + publishes
        "send_reply_draft", "approve_content_draft", "send_a2a_task",
        # self-modification + deploy
        "edit_astra_file", "write_astra_file",
        "commit_code_changes", "commit_kit_changes",
        "apply_self_improvement",
        # deletes
        "forget_memory", "restart_agent",
        # permission surface
        "set_mode", "resolve_approval", "revoke_tool_grant",
        # exposes the machine
        "start_tunnel", "stop_tunnel",
    ):
        assert name in NO_STANDING_TOOLS, f"{name} must be no-standing"


def test_no_standing_list_names_no_retired_tool():
    """Phase A6. The three Mac-bridge writing verbs were on this list;
    they are refused at registration now, so an entry for them would
    describe a surface that cannot exist (and astra-web's mirror of
    this list, checked below, would have to carry the lie too). The
    DB trigger in w1p47q2n8l0l still names them: history at the
    chokepoint, not a surface."""
    from astra.autonomy.approvals import NO_STANDING_TOOLS
    from astra.runtime.tool_registry import _FORBIDDEN

    # The A5 controls stay (a re-registered set_mode must still be
    # no-standing); only the A6 bridge names must be gone.
    bridge_names = _FORBIDDEN - {"resolve_approval", "revoke_tool_grant", "set_mode"}
    assert bridge_names, "the registry no longer forbids the bridge names"
    leaked = sorted(NO_STANDING_TOOLS & bridge_names)
    assert leaked == [], f"no-standing list still names retired tools: {leaked}"


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
    """A tool_grants row for a no-standing tool must not authorise
    anything. (The production row that motivated this, granted
    2026-06-12 via chat, was for the Mac shell tool retired in A6; the
    property is the same for every name on the list.)"""
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
            return await approvals.check_grant("send_reply_draft")

    granted, reason = asyncio.run(_run())
    assert granted is False, (
        "a standing grant on send_reply_draft still authorised a call — "
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
# §8 — there is no bridge (Phase A6). The chokepoint found on
# 2026-08-30 (two production paths reached the Mac bridge without
# crossing the surface guard) is closed by deletion: the bridge, its
# tools, its routes and its tables' callers are gone, and the only
# way to the Mac is astra/broker/client.py::run_intent. Each test
# here pins an ABSENCE, and the one positive property that replaced
# the finalize-scoping check.
# ────────────────────────────────────────────────────────────

_RETIRED_MODULES = (
    "astra.runtime.tools.local",
    "astra.runtime.bridge",
    "astra.runtime.bridge.store",
    "astra.bridge_daemon",
)

# The registry refuses these at boot; see tool_registry._FORBIDDEN.
_RETIRED_TOOLS = frozenset({
    "local_read", "local_write", "local_edit", "local_bash",
    "local_glob", "local_grep", "local_bridge_status", "screenshot_url",
})

_REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def _py_sources(*roots):
    for root in roots:
        for path in (_REPO / root).rglob("*.py"):
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            yield path


def _importable(mod: str) -> bool:
    """find_spec imports the PARENT package, so a missing parent raises
    rather than returning None; both mean 'not importable'."""
    import importlib.util

    try:
        return importlib.util.find_spec(mod) is not None
    except ModuleNotFoundError:
        return False


def test_bridge_modules_are_gone():
    for mod in _RETIRED_MODULES:
        assert not _importable(mod), f"{mod} is back"
    for rel in (
        "astra/bridge_daemon.py",
        "astra/runtime/bridge",
        "astra/runtime/tools/local.py",
        "scripts/issue_bridge_token.py",
    ):
        assert not (_REPO / rel).exists(), f"{rel} is back"


def test_nothing_imports_the_bridge_or_names_itself_to_it():
    """AST, not grep: a docstring may recount the old chokepoint and
    its `on_behalf_of=` keyword (the intent client's does, to explain
    what `actor` replaced); code may not import the old modules, and
    no function may accept or pass `on_behalf_of`. `actor` is the
    replacement and is keyword-only there."""
    import ast

    offenders: list[str] = []
    for path in _py_sources("astra", "services", "scripts"):
        rel = str(path.relative_to(_REPO))
        text = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "on_behalf_of":
                offenders.append(f"{rel}:{node.lineno}: on_behalf_of= passed")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = node.args.args + node.args.kwonlyargs + node.args.posonlyargs
                if any(a.arg == "on_behalf_of" for a in params):
                    offenders.append(f"{rel}:{node.lineno}: def takes on_behalf_of")
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module] + [
                    f"{node.module}.{a.name}" for a in node.names
                ]
            for m in mods:
                if any(m == r or m.startswith(r + ".") for r in _RETIRED_MODULES):
                    offenders.append(f"{rel}:{node.lineno}: import {m}")
    assert offenders == [], f"the bridge is still reachable: {offenders}"


def test_retired_mac_tools_are_absent_from_the_registry():
    import astra.runtime.tools  # noqa: F401
    from astra.runtime.tool_registry import REGISTRY, _FORBIDDEN

    names = set(REGISTRY.names())
    assert not (_RETIRED_TOOLS & names), sorted(_RETIRED_TOOLS & names)
    assert _RETIRED_TOOLS <= _FORBIDDEN, (
        "a retired Mac tool is not refused at boot: "
        f"{sorted(_RETIRED_TOOLS - _FORBIDDEN)}"
    )
    # The replacement is present, or the Mac is unreachable and nothing
    # says so (the CONTAINMENT §9 dead-capability class).
    for name in ("submit_intent", "poll_status", "body_status"):
        assert name in names, f"{name} is not registered"


def test_retired_names_have_no_tier_and_the_body_tools_do():
    """A tier for a name that cannot register reads as evidence the
    tool exists. The A5 controls keep theirs on purpose (a re-
    registration must still be gated); the A6 names must not."""
    for name in _RETIRED_TOOLS:
        assert name not in TOOL_TIERS, f"{name} still has a tier"
    assert TOOL_TIERS["submit_intent"] is ActionTier.WRITE
    assert TOOL_TIERS["poll_status"] is ActionTier.READ
    assert TOOL_TIERS["body_status"] is ActionTier.READ


def test_bridge_routes_are_gone_and_broker_routes_survived():
    """Acceptance criterion 2 of the A6 brief, at the source: the
    poll/result routes and their body model are gone; the broker's
    routes, which a naive grep-and-delete on 'bridge' would not touch
    but a careless one might, are present."""
    main = (_REPO / "services/stream/main.py").read_text()
    for gone in (
        '"/bridge/poll"', '"/bridge/result"',
        "async def bridge_poll", "async def bridge_result",
        "class BridgeResultBody",
    ):
        assert gone not in main, f"{gone} is back in services/stream/main.py"
    for kept in (
        '"/broker/intents/next"', '"/broker/intents/{intent_id}/status"',
        '"/broker/audit"', '"/broker/catalog"',
    ):
        assert kept in main, f"{kept} missing from services/stream/main.py"


def test_unrelated_bridge_identifiers_survived_the_deletion():
    """The A2A router, the Kimi failover and the SDK namespace bridge
    all carry the word 'bridge' and are not the Mac bridge. A
    grep-and-delete that took them is a regression this pins."""
    assert (_REPO / "astra/agents/external/bridge_server.py").exists()
    assert "def maybe_bridge" in (_REPO / "astra/llm/failover.py").read_text()
    assert "def _bridge_constructor" in (
        _REPO / "astra/runtime/tools/__init__.py"
    ).read_text()


def test_broker_status_route_is_scoped_to_the_presenting_body():
    """The property the deleted finalize_call test pinned, on its
    successor: the body's outcome write carries the presenting body's
    id unconditionally, and a mismatch is a loud 404, never ok:true."""
    main = (_REPO / "services/stream/main.py").read_text()
    idx = main.find("async def broker_status")
    assert idx != -1
    body = main[idx:idx + 2000]
    assert "body_id=b.id" in body, (
        "the status route no longer scopes record_status to the caller"
    )
    assert "404" in body, "a rejected status write must fail loudly"


def test_default_surface_and_turn_outside_a_turn_are_least_privilege():
    """Code paths outside a turn — schedulers, jobs — are unattended
    and not-a-turn. The ContextVar defaults carry this; the intent
    client keys the jobs-may-file-only-auto-verbs rule on the second."""
    from astra.autonomy.turn_context import current_surface, current_turn

    assert current_surface.get() == "unattended"
    assert current_turn.get() == ""


def _web_no_standing_list(src: str) -> set[str]:
    import re

    m = re.search(r"NO_STANDING_TOOLS\s*=\s*new Set\(\[(.*?)\]\)", src, re.S)
    assert m, "NO_STANDING_TOOLS Set not found in the web resolver"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_web_resolver_enforces_no_standing():
    """astra-web has its OWN resolver. It drifted from the Python one
    once and wrote standing grants for the Mac shell tool. The two
    lists must be EQUAL in both directions: a name only the web knows
    is one the Python resolver would grant, and a name only Python
    knows is one the web would grant."""
    import pathlib

    from astra.autonomy.approvals import NO_STANDING_TOOLS

    route = (
        pathlib.Path(__file__).resolve().parents[3]
        / "astra-web/app/api/approvals/[id]/resolve/route.ts"
    )
    if not route.exists():          # astra-web not checked out beside astra
        pytest.skip("astra-web not present")
    src = route.read_text()
    web = _web_no_standing_list(src)
    assert web == set(NO_STANDING_TOOLS), (
        f"web-only: {sorted(web - set(NO_STANDING_TOOLS))}; "
        f"python-only: {sorted(set(NO_STANDING_TOOLS) - web)}"
    )
    assert not (web & _RETIRED_TOOLS), sorted(web & _RETIRED_TOOLS)


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
