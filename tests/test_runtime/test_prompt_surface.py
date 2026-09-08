"""The system prompt names only tools that exist.

The prompt is served on every turn (services/stream/main.py
get_system_prompt). A tool it names that the registry does not hold is
a guaranteed "unknown tool" in chat followed by an improvised excuse;
the retired Mac bridge left seven such names and a "type `expand
bridge to <path>`" instruction whose interceptor no longer existed.
This pins the surface the prompt describes to the surface the registry
serves, and pins the A6 statements the prompt must make plainly.
"""

from __future__ import annotations

import re

import astra.runtime.tools  # noqa: F401 (populate the registry)
from astra.core.system_prompt import get_system_prompt
from astra.runtime.tool_registry import REGISTRY, _FORBIDDEN

PROMPT = get_system_prompt()

# Backticked snake_case identifiers that are NOT tools, each with the
# reason it may appear. Anything else backticked in the prompt that
# is not a registered tool fails the test below.
_NOT_TOOLS: dict[str, str] = {
    # autonomy mode names
    "always_ask": "mode", "semi_auto": "mode", "full_auto": "mode",
    # tool arguments the prompt explains
    "args": "argument", "why": "argument", "path": "argument",
    "pattern": "argument", "include": "argument", "old": "argument",
    "new": "argument", "url": "argument", "content": "argument",
    "artifact_id": "argument",
    # forbidden argument KEYS the prompt tells the model never to send
    "reason": "forbidden arg key", "approved": "forbidden arg key",
    "tier": "forbidden arg key", "body_id": "forbidden arg key",
    # A2A agent slugs and the A2A router service
    "bookkeeper": "agent slug", "linkedin": "agent slug",
    "bridge": "the A2A router service, named as NOT the Mac",
    # a scheduler job
    "inbox_triage": "job",
    # deny reasons the body returns, quoted so the model can match on
    # them; they are outcomes, not callable tools.
    "precondition_failed": "a broker deny reason, not a tool",
    # tools the prompt says were DELETED / are not the model's
    "set_mode": "named as not one of your tools",
    "fleet_summary": "named as deleted", "agent_status": "named as deleted",
    "fleet_health": "named as deleted",
}


def _backticked_identifiers() -> set[str]:
    return set(re.findall(r"`([a-z][a-z0-9_]*)(?:\(|`)", PROMPT))


def test_every_backticked_tool_name_is_registered():
    names = set(REGISTRY.names())
    unknown = sorted(
        i for i in _backticked_identifiers()
        if i not in names and i not in _NOT_TOOLS
    )
    assert unknown == [], (
        f"the prompt names tools the registry does not hold: {unknown}. "
        "Register them, or add them to _NOT_TOOLS with the reason."
    )


def test_not_tools_list_is_not_stale():
    """An entry here for a name that IS registered would hide a real
    tool from the check above; an entry the prompt no longer mentions
    is dead weight that trains people to add to the list."""
    names = set(REGISTRY.names())
    idents = _backticked_identifiers()
    registered = sorted(n for n in _NOT_TOOLS if n in names)
    assert registered == [], f"_NOT_TOOLS names registered tools: {registered}"
    unused = sorted(n for n in _NOT_TOOLS if n not in idents)
    assert unused == [], f"_NOT_TOOLS carries names the prompt no longer uses: {unused}"


def test_prompt_names_no_forbidden_tool_and_no_bridge():
    for name in sorted(_FORBIDDEN - {"set_mode"}):
        assert name not in PROMPT, f"the prompt still names {name}"
    for phrase in (
        "expand bridge", "bridge daemon", "bridge_daemon", "Local Bridge",
        "local bridge", "bridge is down", "bridge offline", "tagged 'bridge'",
    ):
        assert phrase not in PROMPT, f"the prompt still says {phrase!r}"


def test_prompt_teaches_the_three_body_tools_and_the_standing_rule():
    for name in ("submit_intent", "poll_status", "body_status"):
        assert f"`{name}(" in PROMPT, f"{name} is not taught"
    assert "Kunal's Mac (the body)" in PROMPT
    assert "2026-07-03" in PROMPT, "Kunal's standing rule must survive"
    assert "tagged 'body'" in PROMPT
    assert "NEVER volunteer" in PROMPT
    assert "code change and a re-sign" in PROMPT, "widening a root is not a chat command"
    assert "no command to widen a root" in PROMPT


def test_prompt_says_plainly_what_is_unavailable():
    assert "Not available from chat" in PROMPT
    assert "git commit and git push" in PROMPT
    assert "Commit and push from chat are NOT available" in PROMPT
    assert "GUI-session helper" in PROMPT
    assert "Never describe a push as pending, gated or queued." in PROMPT
    # the old flow's promise is gone in every spelling
    assert "git -C <path>" not in PROMPT
    assert "add -A && commit && push" not in PROMPT


def test_agent_repos_result_names_no_forbidden_tool():
    import asyncio

    from astra.tools.agent_repos_tools import agent_repos_tool

    td = REGISTRY.get("agent_repos")
    assert td is not None
    out = asyncio.run(agent_repos_tool.handler({}))
    text = out["content"][0]["text"]
    for blob, where in ((td.description, "description"), (text, "result")):
        for name in sorted(_FORBIDDEN):
            assert name not in blob, f"agent_repos {where} still names {name}"
        assert "submit_intent" in blob, where
    assert "NOT available from chat" in text
