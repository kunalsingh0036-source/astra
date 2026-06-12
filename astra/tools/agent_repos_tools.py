"""
Agent repo map — the reliable substrate for "fix X through chat".

Code-level fixes to a federated agent already work: the repos are on
Kunal's Mac and Astra has the local bridge (local_read/edit/bash) +
the Phase-C approval gate on the destructive git push. The ONE thing
that made this fragile was path-guessing — the operating mode's
recurring failure ("never ask Kunal to spell a directory; list-then-
match"). This tool removes the guess: it returns the canonical
name → local-path → GitHub-remote map so the fix flow always targets
the right repo, and each remote's push auto-triggers that agent's
Railway redeploy.

The map is static (these paths don't move); a path that's gone is
reported honestly so a renamed/removed repo surfaces instead of a
silent wrong-directory edit.
"""

from __future__ import annotations

import os

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
        "deploys": "Railway 'astra' project (stream/scheduler/email/finance/whatsapp/bridge/web)",
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


@tool(
    "agent_repos",
    "The canonical map of every agent's LOCAL repo path + GitHub "
    "remote + what it deploys. Use this BEFORE any code-level fix so "
    "you target the right directory (never guess a path). Pushing to "
    "an agent's remote auto-triggers its Railway redeploy. For the "
    "actual fix use the local bridge: local_read/local_grep to find "
    "the bug, local_edit to fix, local_bash for git add/commit/push "
    "(the push is gated — Kunal approves).",
    {},
)
async def agent_repos_tool(args: dict) -> dict:
    lines = ["Agent repos (name → path → deploy):"]
    for name, info in AGENT_REPOS.items():
        lines.append(f"\n{name}")
        lines.append(f"  path:    {info['path']}")
        lines.append(f"  remote:  {info['remote']}")
        lines.append(f"  deploys: {info['deploys']}")
    lines.append(
        "\nFix flow: agent_repos → local_grep/read to locate → "
        "local_edit → (test if possible) → local_bash 'git -C <path> "
        "add -A && git commit -m ... && git push' (gated) → "
        "fleet_status to confirm the redeploy came back healthy."
    )
    return {"content": [{"type": "text", "text": "\n".join(lines)}]}


def create_agent_repos_mcp_server():
    return create_sdk_mcp_server(
        name="astra-agent-repos",
        version="0.1.0",
        tools=[agent_repos_tool],
    )
