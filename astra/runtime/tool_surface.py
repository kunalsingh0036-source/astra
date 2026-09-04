"""
Tool surfaces — the hard rule that the unattended tool set is a
strict subset of the interactive one (CONTAINMENT §4).

Two surfaces:

  interactive — Kunal is in the chair, watching the actual UI: the
      web PWA or the local CLI. Diffs render, artifacts render, he
      can read what a code edit actually does before it lands.
  unattended  — everything else: WhatsApp turns (a phone chat where
      diffs don't render and authorship of the transport cannot be
      proven — see astra-body/docs/SECURITY-MODEL.md §5), any future
      scheduler- or objective-driven turn, and any caller that does
      not explicitly claim interactive.

The self-modification tool families (code editor, kit editor,
self-improve — the tools that edit, test, commit and deploy Astra's
own code and kits) exist ONLY on the interactive surface. This
breaks the documented injection chain (group text → edit_astra_file
→ in-process Python → self-approval → commit → deploy) at its third
step: the tools simply are not in the unattended runtime.

The exclusion set is DERIVED from the same module constants that
register the tools (CODE_EDITOR_TOOLS, KIT_EDITOR_TOOLS,
SELF_IMPROVE_TOOLS), so a tool added to those families later is
excluded automatically — the list cannot drift. A hardcoded fallback
covers the case where those imports break: fail closed, never open.

The Mac-bridge writing verbs (local_edit / local_write / local_bash)
are also interactive-only: an unattended agent that can write
arbitrary files on Kunal's Mac can write launchd plists, shell rc
files, and the sidecar's trust-root config — self-modification
through the side door.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SURFACE_INTERACTIVE = "interactive"
SURFACE_UNATTENDED = "unattended"

_VALID_SURFACES = frozenset({SURFACE_INTERACTIVE, SURFACE_UNATTENDED})

# Fallback if the family constants cannot be imported. Kept in sync
# by test_containment_now.py::test_fallback_matches_families — if a
# tool is added to a family module, that test fails until this list
# is updated, so the fallback can only ever lag LOUDLY.
_FALLBACK_INTERACTIVE_ONLY = frozenset({
    # code editor family
    "read_astra_file",
    "list_astra_files",
    "edit_astra_file",
    "write_astra_file",
    "show_astra_diff",
    "run_creator_tests",
    "commit_code_changes",
    "revert_last_code_commit",
    # kit editor family
    "add_forbidden_phrase",
    "add_voice_note",
    "add_proof_point",
    "add_audience_objection",
    "commit_kit_changes",
    # self-improve family
    "observe_issue",
    "list_self_improvements",
    "propose_self_improvement",
    "review_proposal",
    "apply_self_improvement",
    "dismiss_self_improvement",
})

# Interactive-only tools that live outside the three families.
_EXTRA_INTERACTIVE_ONLY = frozenset({
    "local_edit",
    "local_write",
    "local_bash",

    # Workstream A3. An EXPLICIT decision, not an omission.
    #
    # submit_intent cannot itself cause anything — the broker holds the
    # gate and signed verbs need Kunal's fingerprint. But exec.shell,
    # fs.write and fs.edit are already compiled into the shipped broker
    # catalogue, and filing an intent for one of them RAISES A TOUCH ID
    # PROMPT on his Mac. Habituation is the attack that software cannot
    # eliminate, and precise control over WHEN a human is asked is most
    # of it.
    #
    # Today a prompt-injected WhatsApp turn cannot even name local_bash
    # (CONTAINMENT §4). Leaving submit_intent off this list would hand
    # that capability straight back through a new door. Widening it is a
    # per-verb decision for a later phase, taken deliberately, with the
    # broker's per-hour rate limit in place first — that limit does not
    # exist yet.
    #
    # poll_status is deliberately NOT here: reading a status causes
    # nothing, and an unattended turn should be able to report progress.
    "submit_intent",
})


def interactive_only_tool_names() -> frozenset[str]:
    """The tool names that must never appear on the unattended
    surface. Derived from the family module constants, unioned with
    the fallback (so a partial import failure can only ADD names,
    never lose them) and the extra names above."""
    names: set[str] = set(_FALLBACK_INTERACTIVE_ONLY) | set(
        _EXTRA_INTERACTIVE_ONLY
    )
    try:
        from astra.tools.code_editor_tools import CODE_EDITOR_TOOLS
        from astra.tools.kit_editor_tools import KIT_EDITOR_TOOLS
        from astra.tools.self_improve_tools import SELF_IMPROVE_TOOLS

        for t in (*CODE_EDITOR_TOOLS, *KIT_EDITOR_TOOLS, *SELF_IMPROVE_TOOLS):
            name = getattr(t, "name", None)
            if name:
                names.add(name)
    except Exception:
        # Fail closed: the fallback set still excludes the known
        # families; log so the drift is visible, never silent.
        logger.exception(
            "[tool-surface] family constants unavailable — using "
            "fallback interactive-only set"
        )
    return frozenset(names)


def normalize_surface(surface: str | None) -> str:
    """Coerce a caller-supplied surface to a valid one.

    Anything unknown or unset is UNATTENDED — least privilege is the
    default; interactive is claimed explicitly by the two surfaces
    where Kunal demonstrably is (web PWA, local CLI)."""
    s = (surface or "").strip().lower()
    if s not in _VALID_SURFACES:
        return SURFACE_UNATTENDED
    return s


def surface_for_channel(channel: str | None) -> str:
    """Map a /turns/start channel to a surface.

    web (and the web default of None from the PWA) → interactive.
    whatsapp → unattended: the transport cannot prove authorship
    (HMAC authenticates Meta, not Kunal), diffs and artifacts don't
    render there, and WhatsApp is being deleted as an authorization
    channel in the target security model. Everything unknown →
    unattended."""
    c = (channel or "web").strip().lower()
    if c == "web":
        return SURFACE_INTERACTIVE
    return SURFACE_UNATTENDED


def allowed_tool_names(
    all_names: list[str], surface: str
) -> tuple[list[str], list[str]]:
    """Split names into (allowed, excluded) for the given surface."""
    s = normalize_surface(surface)
    if s == SURFACE_INTERACTIVE:
        return list(all_names), []
    blocked = interactive_only_tool_names()
    allowed = [n for n in all_names if n not in blocked]
    excluded = [n for n in all_names if n in blocked]
    return allowed, excluded
