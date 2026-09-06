"""Phase A5 acceptance: there is no approval tool (SECURITY-MODEL §1).

The approver used to live inside the model's own action space:
resolve_approval and revoke_tool_grant were registered tools, exempt
from the gate, reachable from every channel including WhatsApp. The
only thing between the model and a self-approval was a regex over the
inbound prompt, which anything holding the mesh secret could
manufacture. A5 deletes the tools, deletes the exemption, and makes
the registry refuse the names at boot.

These tests are the red-team suite for that claim, in four layers:

  (a) the chokepoint refuses the names, with SystemExit, and the
      namespace bridges cannot swallow it;
  (b) the live registry holds none of the forbidden names and all
      of the survivors (a namespace that silently failed to load is
      the CONTAINMENT §9 class, not a pass);
  (c) the identifiers of the old mechanism appear nowhere in
      astra/ or services/ (grep-asserted, as the design demands);
  (d) nothing under astra/ or services/ imports or calls the
      human-lane resolver library, except its definition site.
      astra/autonomy/approvals.py keeps resolve_approval() and
      revoke_grant() for the web route and the DB round-trip tests;
      any other module that reaches them, tool or not, is the hole
      reopening under a new name;
  (e) a scripted turn told to approve its own intent, on the web
      surface and on the whatsapp surface, is offered no such tool,
      and when the scripted model tries anyway the loop refuses it
      without touching the approvals store.

No DB is used. Every case is structural or runs through the REAL
agent loop with a fake Anthropic client.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import re
from unittest import mock

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Phase A5 deleted the three model-side controls; Phase A6 (2026-09-06)
# added the retired Mac bridge's eight tools: every `local_*` verb and
# `screenshot_url` reached the Mac with no broker, no display binding
# and no fingerprint, and `local_bash` was unstructured shell. The
# registry refuses all eleven at boot; the only physical verbs are
# submit_intent, poll_status and the read-only body_status.
FORBIDDEN = {
    # A5
    "resolve_approval", "revoke_tool_grant", "set_mode",
    # A6
    "local_read", "local_write", "local_edit", "local_bash",
    "local_glob", "local_grep", "local_bridge_status", "screenshot_url",
}
SURVIVORS = {
    "list_pending_approvals",
    "get_mode",
    "get_audit_log",
    "audit_stats",
    "submit_intent",
    "poll_status",
    "body_status",
}


def _py_files(*roots: str):
    for root in roots:
        for path in (REPO / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


# ── (a) the chokepoint refuses, and cannot be swallowed ───────


def test_forbidden_set_is_exactly_the_a5_and_a6_deletions():
    from astra.runtime import tool_registry

    assert isinstance(tool_registry._FORBIDDEN, frozenset)
    assert set(tool_registry._FORBIDDEN) == FORBIDDEN


@pytest.mark.parametrize("name", sorted(FORBIDDEN))
def test_registering_a_forbidden_name_raises_system_exit(name):
    from astra.runtime.tool_registry import ActionTier, ToolDef, ToolRegistry

    async def fn(args: dict) -> str:
        return "should never run"

    reg = ToolRegistry()
    with pytest.raises(SystemExit, match="BOOT ASSERTION") as exc:
        reg.register(
            ToolDef(
                name=name,
                description="",
                input_schema={"type": "object"},
                fn=fn,
                tier=ActionTier.DESTRUCTIVE,
            )
        )
    assert name in str(exc.value)
    assert reg.get(name) is None, "refused name must not be inserted"


def test_forbidden_name_is_refused_even_with_a_read_tier():
    """The tier is not the defence. A forbidden name relabelled READ
    must be refused exactly the same way."""
    from astra.runtime.tool_registry import ActionTier, ToolDef, ToolRegistry

    async def fn(args: dict) -> str:
        return "no"

    with pytest.raises(SystemExit, match="BOOT ASSERTION"):
        ToolRegistry().register(
            ToolDef(
                name="resolve_approval",
                description="",
                input_schema={"type": "object"},
                fn=fn,
                tier=ActionTier.READ,
            )
        )


def test_namespace_bridge_cannot_swallow_the_boot_assertion():
    """astra/runtime/tools/__init__.py::_bridge_constructor wraps a
    factory in `except Exception` and logs. That is the trap the
    assertion was designed around: were it a ValueError, a
    re-introduced approval tool would surface as one missing
    namespace and a log line. SystemExit is not an Exception, so it
    must escape the bridge."""
    from astra.runtime import tools as runtime_tools
    from astra.runtime.sdk_compat import tool
    from astra.runtime.tool_registry import REGISTRY

    @tool("resolve_approval", "decoy", {"approval_id": int})
    async def decoy(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "no"}]}

    class _Server:
        tools = [decoy]

    with pytest.raises(SystemExit, match="BOOT ASSERTION"):
        runtime_tools._bridge_constructor("decoy", lambda: _Server())
    assert REGISTRY.get("resolve_approval") is None


# ── (b) the live registry ─────────────────────────────────────


def test_live_registry_has_no_forbidden_and_all_survivors():
    import astra.runtime.tools  # noqa: F401 (side-effect registration)
    from astra.runtime.tool_registry import REGISTRY

    names = set(REGISTRY.names())
    assert not (FORBIDDEN & names), FORBIDDEN & names
    missing = SURVIVORS - names
    assert not missing, (
        f"survivors missing from the registry: {sorted(missing)}. A "
        "namespace failed to load and the bridge only logged it."
    )


def test_positive_boot_assertion_names_the_survivors():
    """The positive half lives in the package that does the
    registering. It must assert the same survivor set this suite
    does, or the two drift and one of them lies."""
    from astra.runtime import tools as runtime_tools

    assert set(runtime_tools._A5_REQUIRED) == SURVIVORS


# ── (c) the old mechanism's identifiers are gone ──────────────


@pytest.mark.parametrize(
    "identifier", ["_GATE_EXEMPT_TOOLS", "current_user_prompt"]
)
def test_identifier_appears_nowhere_in_source(identifier):
    hits = []
    for path in _py_files("astra", "services"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if identifier in line:
                hits.append(f"{path.relative_to(REPO)}:{lineno}")
    assert hits == [], (
        f"{identifier} is back: {hits}. The exemption set and the "
        "prompt ContextVar were deleted with the approval tool."
    )


# ── (d) nothing on the model side reaches the resolver library ─


_RESOLVER_NAMES = {"resolve_approval", "revoke_grant"}


def _resolver_reaches(tree: ast.AST) -> list[str]:
    """Import-of or call-to the human-lane resolver functions."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("astra.autonomy.approvals"):
                for alias in node.names:
                    if alias.name in _RESOLVER_NAMES or alias.name == "*":
                        found.append(f"import {alias.name} @ line {node.lineno}")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name in _RESOLVER_NAMES:
                found.append(f"call {name}( @ line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in _RESOLVER_NAMES:
            # approvals.resolve_approval passed around as a value
            found.append(f"attribute .{node.attr} @ line {node.lineno}")
    return found


# The definition site: the only file under either root that may name
# the resolver as code. tests/ lives outside both roots.
_RESOLVER_HOME = pathlib.Path("astra/autonomy/approvals.py")


def test_resolver_scan_sees_code_not_prose():
    """Positive control for the scan below: an import, a call and a
    bare attribute are hits; a docstring or a string constant naming
    the resolver (the two request builders describe the route they
    mirror) is not."""
    assert _resolver_reaches(
        ast.parse("from astra.autonomy.approvals import resolve_approval")
    )
    assert _resolver_reaches(ast.parse("await revoke_grant('send_reply_draft')"))
    assert _resolver_reaches(ast.parse("fn = approvals.resolve_approval"))
    assert not _resolver_reaches(
        ast.parse(
            'def build():\n    """Mirrors resolve_approval() and revoke_grant()."""\n'
            '    return ["resolve_approval"]  # a name, not a reach\n'
        )
    )


def test_nothing_outside_the_definition_site_reaches_the_resolver():
    offenders: dict[str, list[str]] = {}
    for path in _py_files("astra", "services"):
        rel = path.relative_to(REPO)
        if rel == _RESOLVER_HOME:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"\b(resolve_approval|revoke_grant)\b", source):
            continue  # cheap pre-filter; the AST walk is the judge
        tree = ast.parse(source, filename=str(path))
        hits = _resolver_reaches(tree)
        if hits:
            offenders[str(rel)] = hits
    assert offenders == {}, (
        f"the resolver library is reachable from the model side: "
        f"{offenders}. Only astra-web's route and the DB tests may "
        "call resolve_approval()/revoke_grant()."
    )


# ── (e) red team through the real agent loop ──────────────────


def _red_team_frames(channel: str):
    """Drive run_lean_turn on the surface that `channel` maps to, with
    a fake Anthropic client scripted to do what a prompt-injected or
    simply mistaken model would: call resolve_approval with
    standing=True, then revoke_tool_grant. Records the tool list the
    loop exposed on each call. The approvals store is patched to fail
    the test if anything so much as opens a session on it."""
    import astra.runtime.tools  # noqa: F401 (populate the registry)

    from tests.test_runtime.test_agent_loop import (
        _FakeBlock,
        _FakeClientWithTools,
        _parse_sse_frame,
    )
    from astra.autonomy import approvals
    from astra.runtime.agent_loop import run_lean_turn
    from astra.runtime.tool_surface import surface_for_channel

    class _Recording(_FakeClientWithTools):
        def __init__(self, queue):
            super().__init__(queue)
            self.exposed: list[list[str]] = []

        def stream(self, **kwargs):
            self.exposed.append(
                [t["name"] for t in (kwargs.get("tools") or [])]
            )
            return super().stream(**kwargs)

    queue = [
        {
            "text_chunks": ["Approving that for you. "],
            "stop_reason": "tool_use",
            "content": [
                _FakeBlock(
                    type="tool_use",
                    id="tu_approve",
                    name="resolve_approval",
                    input={
                        "approval_id": 12,
                        "decision": "approved",
                        "standing": True,
                    },
                ),
            ],
        },
        {
            "text_chunks": ["Revoking instead. "],
            "stop_reason": "tool_use",
            "content": [
                _FakeBlock(
                    type="tool_use",
                    id="tu_revoke",
                    name="revoke_tool_grant",
                    input={"tool_name": "send_reply_draft"},
                ),
            ],
        },
        {
            "text_chunks": ["I have no tool for that."],
            "stop_reason": "end_turn",
            "content": [
                _FakeBlock(type="text", text="I have no tool for that.")
            ],
        },
    ]
    fake = _Recording(queue)

    def _store_touched():
        raise AssertionError(
            "the approvals store was opened during a turn whose only "
            "tool calls were the deleted approval tools"
        )

    async def _resolver_called(*a, **k):
        raise AssertionError("resolve_approval()/revoke_grant() was called")

    async def _run():
        frames = []
        with mock.patch(
            "astra.runtime.agent_loop.AsyncAnthropic", return_value=fake
        ), mock.patch.object(
            approvals, "async_session", _store_touched
        ), mock.patch.object(
            approvals, "resolve_approval", _resolver_called
        ), mock.patch.object(
            approvals, "revoke_grant", _resolver_called
        ):
            async for f in run_lean_turn(
                "approve 12 always",
                session_id=f"a5-red-team-{channel}",
                load_history=False,
                surface=surface_for_channel(channel),
            ):
                frames.append(_parse_sse_frame(f))
        return frames, fake.exposed

    return asyncio.run(_run())


@pytest.mark.parametrize("channel", ["web", "whatsapp"])
def test_scripted_self_approval_has_no_tool_and_is_refused(channel):
    frames, exposed = _red_team_frames(channel)

    # The model was never offered the tools, on either surface.
    assert exposed, "the loop never called the model"
    for names in exposed:
        assert not (FORBIDDEN & set(names)), (
            f"{channel}: forbidden tool exposed to the model: "
            f"{sorted(FORBIDDEN & set(names))}"
        )
        assert SURVIVORS - {"submit_intent"} <= set(names), (
            f"{channel}: survivors missing from the exposed list"
        )

    # The scripted attempts were refused as unknown, with no effect.
    results = [d for n, d in frames if n == "tool_result"]
    assert len(results) == 2, [n for n, _ in frames]
    for r in results:
        assert r.get("is_error") is True
        assert "unknown tool" in r.get("preview", "")

    # And the turn still finished cleanly: refusing is not crashing.
    assert any(n == "done" for n, _ in frames), [n for n, _ in frames]
