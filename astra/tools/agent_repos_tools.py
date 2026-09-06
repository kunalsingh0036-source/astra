"""
Agent repo map — the reliable substrate for "fix X through chat".

The ONE thing that made code-level fixes to a federated agent fragile
was path-guessing — the operating mode's recurring failure ("never ask
Kunal to spell a directory; list-then-match"). This tool removes the
guess: it returns the canonical name → local-path → GitHub-remote map
so the fix flow always targets the right repo, and each remote's push
auto-triggers that agent's Railway redeploy.

What the flow can DO from chat is a separate question, and this tool
answers it honestly from the capability broker's catalogue mirror
(astra/broker/client.py) rather than from a sentence written when the
Mac bridge existed. Since Phase A6 the Mac is reached only through
submit_intent: reading code is `fs.read` (and `fs.grep` once wired);
applying a fix is `fs.edit`, running tests is `exec.shell`, both
fingerprint-gated. Commit and push are NOT available from chat: no git
verb exists in the catalogue, and `exec.shell` is jailed away from
credentials even once wired. The rendered flow below says which steps
this build can perform, so the model is never told to run a step the
executor will refuse.

The map is static (these paths don't move); a path that's gone is
reported honestly so a renamed/removed repo surfaces instead of a
silent wrong-directory edit.
"""

from __future__ import annotations

import os

from astra.broker import client as _broker
from astra.runtime.sdk_compat import tool, create_sdk_mcp_server

# name → (local path, GitHub remote, deploy note). Kept here as the
# single source of truth; mirror of the memory file but tool-readable.
_BASE = os.environ.get(
    "ASTRA_CODE_ROOT", "/Users/kunalsingh/Claude Code"
).rstrip("/")

AGENT_REPOS: dict[str, dict[str, str]] = {
    "astra": {
        "path": f"{_BASE}/astra",
        "remote": "kunalsingh0036-source/astra",
        "deploys": (
            "Railway 'astra' project (stream/scheduler/email/finance/"
            "whatsapp/web, plus 'bridge', the A2A router service, "
            "which is not the retired Mac bridge)"
        ),
    },
    "astra-web": {
        "path": f"{_BASE}/astra-web",
        "remote": "kunalsingh0036-source/astra-web",
        "deploys": "Vercel (the web UI)",
    },
    "helmtech": {
        "path": f"{_BASE}/helmtech-outreach-agent",
        "remote": "kunalsingh0036-source/Helm-Sales",
        "deploys": "Railway 'HelmTech Sales' → Helm-Sales service",
    },
    "apex-sales": {
        "path": f"{_BASE}/apex-sales-team",
        "remote": "kunalsingh0036-source/apex-sales-team",
        "deploys": "Railway 'Apex Sales'",
    },
    "apex-experimental": {
        "path": f"{_BASE}/apex-experimental",
        "remote": "kunalsingh0036-source/apex-experimental",
        "deploys": "Railway 'apex experimental'",
    },
    "linkedin": {
        "path": f"{_BASE}/linkedin-agent",
        "remote": "kunalsingh0036-source/linkedin-agent",
        "deploys": "Railway 'LinkedIn Agent'",
    },
    "bookkeeper": {
        "path": f"{_BASE}/bookkeeper-agent",
        "remote": "(no remote — not deployed)",
        "deploys": "not deployed",
    },
}

# The steps of a code fix and the catalogue verb each one needs. A
# step whose verb is unwired is rendered as unavailable, from the
# mirror, so this text cannot lag the executor. Commit/push has no
# verb at all and is stated as unavailable unconditionally.
_FIX_STEPS: tuple[tuple[str, str], ...] = (
    ("locate the bug", "fs.grep"),
    ("read the file", "fs.read"),
    ("apply the edit", "fs.edit"),
    ("run its tests", "exec.shell"),
)


def _step_state(verb: str) -> str:
    spec = _broker.CATALOGUE_BY_NAME.get(verb)
    if spec is None:
        return f"{verb}: NOT a catalogue verb; unavailable"
    gate = ("fingerprint every time" if spec.signed else "no fingerprint")
    if spec.wired:
        return f"{verb} via submit_intent ({gate})"
    return (
        f"{verb} is catalogued but NOT wired in this build: refused "
        "before filing, nobody is asked"
    )


def fix_flow_text() -> str:
    """The fix flow with each step's live availability. Used in the
    tool result (and readable by tests) so the model is told exactly
    what this build can do."""
    lines = ["Fix flow (this build):", "  0. agent_repos (this tool) → the exact path; never guess"]
    for i, (step, verb) in enumerate(_FIX_STEPS, start=1):
        lines.append(f"  {i}. {step}: {_step_state(verb)}")
    lines.append(
        "  5. commit and push: NOT available from chat. No git verb "
        "exists in the catalogue, and exec.shell runs in a jail with no "
        "credentials even once wired. Say so plainly; offer add_task "
        "tagged 'body'. Never describe a push as pending, gated or queued."
    )
    lines.append(
        "  6. after Kunal pushes: fleet_status to confirm the redeploy "
        "came back healthy."
    )
    return "\n".join(lines)


@tool(
    "agent_repos",
    "The canonical map of every agent's LOCAL repo path + GitHub "
    "remote + what it deploys. Use this BEFORE any code-level fix so "
    "you target the right directory (never guess a path). Pushing to "
    "an agent's remote auto-triggers its Railway redeploy. The result "
    "also carries the fix flow with each step's availability in this "
    "build: reading code is fs.read/fs.grep through submit_intent, "
    "editing is fs.edit and tests are exec.shell (fingerprint-gated), "
    "and commit/push from chat is NOT available until a parameterised "
    "git verb exists; say so rather than improvise.",
    {},
)
async def agent_repos_tool(args: dict) -> dict:
    lines = ["Agent repos (name → path → deploy):"]
    for name, info in AGENT_REPOS.items():
        lines.append(f"\n{name}")
        lines.append(f"  path:    {info['path']}")
        lines.append(f"  remote:  {info['remote']}")
        lines.append(f"  deploys: {info['deploys']}")
    lines.append("")
    lines.append(fix_flow_text())
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_agent_repos_mcp_server():
    return create_sdk_mcp_server(
        name="astra-agent-repos",
        version="0.1.0",
        tools=[agent_repos_tool],
    )
