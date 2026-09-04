"""
Autonomy mode definitions.

Three modes that control how much Astra can do without asking:

1. ALWAYS_ASK — Every action requires user approval
2. SEMI_AUTO — Read-only actions auto-approved, destructive actions need approval
3. FULL_AUTO — Everything executes immediately, audit log generated

Each tool action is classified into a tier:
- READ: No side effects (file reads, searches, API GETs)
- WRITE: Modifiable but recoverable (file edits, memory updates)
- DESTRUCTIVE: Hard to reverse (file deletes, sending emails, API mutations)
"""

import enum


class AutonomyMode(str, enum.Enum):
    ALWAYS_ASK = "always_ask"
    SEMI_AUTO = "semi_auto"
    FULL_AUTO = "full_auto"


class ActionTier(str, enum.Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class PermissionDecision(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


# Permission matrix: mode × action tier → decision
PERMISSION_MATRIX: dict[AutonomyMode, dict[ActionTier, PermissionDecision]] = {
    AutonomyMode.ALWAYS_ASK: {
        ActionTier.READ: PermissionDecision.ASK,
        ActionTier.WRITE: PermissionDecision.ASK,
        ActionTier.DESTRUCTIVE: PermissionDecision.ASK,
    },
    AutonomyMode.SEMI_AUTO: {
        ActionTier.READ: PermissionDecision.ALLOW,
        ActionTier.WRITE: PermissionDecision.ALLOW,
        ActionTier.DESTRUCTIVE: PermissionDecision.ASK,
    },
    AutonomyMode.FULL_AUTO: {
        ActionTier.READ: PermissionDecision.ALLOW,
        ActionTier.WRITE: PermissionDecision.ALLOW,
        ActionTier.DESTRUCTIVE: PermissionDecision.ALLOW,
    },
}


# Tool name → action tier mapping.
#
# CONTAINMENT (2026-08-28): this map now covers the FULL registered
# tool surface (145 tools enumerated from astra.runtime.tools), and
# the default for a name NOT in this map is DESTRUCTIVE — not WRITE.
# WRITE-by-default meant every unmapped tool was auto-allowed in
# semi_auto (the default mode): edit_astra_file, commit_code_changes,
# send_reply_draft, approve_content_draft and ~80 others all landed in
# an auto-allow tier because nobody had classified them. A new tool
# that nobody classified must land in the tier that ASKS, not the tier
# that acts. See astra-body/docs/CONTAINMENT-NOW.md §1.
#
# Classification rules used (be honest, not convenient):
#   READ        — pure lookup; no state changes anywhere.
#   WRITE       — recoverable state: drafts, memory, labels, task rows.
#   DESTRUCTIVE — sends, publishes, commits/deploys code, executes
#                 shell/tests, exposes the machine, deletes the
#                 irreplaceable, or authorizes any of the above.
TOOL_TIERS: dict[str, ActionTier] = {
    # ── Legacy SDK names ────────────────────────────────────
    "Read": ActionTier.READ,
    "Glob": ActionTier.READ,
    "Grep": ActionTier.READ,
    "WebSearch": ActionTier.READ,
    "WebFetch": ActionTier.READ,
    "Edit": ActionTier.WRITE,
    "Write": ActionTier.WRITE,
    "Bash": ActionTier.DESTRUCTIVE,
    # Legacy service-management names (deleted from the runtime
    # 2026-06-13; kept so the laptop-only CLI path stays classified)
    "agent_status": ActionTier.READ,
    "cost_report": ActionTier.READ,
    "fleet_health": ActionTier.READ,
    "service_logs": ActionTier.READ,
    "start_service": ActionTier.WRITE,
    "start_fleet": ActionTier.WRITE,
    "stop_service": ActionTier.DESTRUCTIVE,
    "stop_fleet": ActionTier.DESTRUCTIVE,

    # ── a2a ─────────────────────────────────────────────────
    "a2a_health_check": ActionTier.READ,
    "get_a2a_task": ActionTier.READ,
    "list_discovered_agents": ActionTier.READ,
    "discover_agent": ActionTier.WRITE,
    # Dispatches work to another agent that can itself act — gate it.
    "send_a2a_task": ActionTier.DESTRUCTIVE,
    "cancel_a2a_task": ActionTier.DESTRUCTIVE,

    # ── agent_repos ─────────────────────────────────────────
    "agent_repos": ActionTier.READ,

    # ── artifacts (chat-UI renders) ─────────────────────────
    "emit_draft": ActionTier.WRITE,
    "emit_metric": ActionTier.WRITE,
    "emit_palette": ActionTier.WRITE,
    "emit_table": ActionTier.WRITE,
    "prepare_preview": ActionTier.WRITE,

    # ── autonomy ────────────────────────────────────────────
    "get_mode": ActionTier.READ,
    "get_audit_log": ActionTier.READ,
    "audit_stats": ActionTier.READ,
    "list_pending_approvals": ActionTier.READ,
    # Gate-exempt today (they ARE the approval mechanism until the
    # capability broker replaces them) — classified DESTRUCTIVE so
    # that if the exemption is ever removed they fail safe.
    "resolve_approval": ActionTier.DESTRUCTIVE,
    "revoke_tool_grant": ActionTier.DESTRUCTIVE,
    # Removed from the model's tool surface (CONTAINMENT §3); tier
    # kept so any residual caller is gated, not defaulted.
    "set_mode": ActionTier.DESTRUCTIVE,

    # ── browser (extension; ACT staging is gated downstream
    #    by the approvals JOIN in astra/browser/store.py) ────
    "browser_read": ActionTier.READ,
    "browser_extract": ActionTier.READ,
    "browser_stage_action": ActionTier.WRITE,

    # ── business state ──────────────────────────────────────
    "helm_state": ActionTier.READ,
    "apex_state": ActionTier.READ,
    "bay_state": ActionTier.READ,
    "topstudios_state": ActionTier.READ,
    "fleet_status": ActionTier.READ,

    # ── calendar ────────────────────────────────────────────
    "calendar_search": ActionTier.READ,
    "calendar_status": ActionTier.READ,
    "calendar_today": ActionTier.READ,
    "calendar_tomorrow": ActionTier.READ,
    "calendar_week": ActionTier.READ,

    # ── content ─────────────────────────────────────────────
    "content_metrics": ActionTier.READ,
    "get_content_draft": ActionTier.READ,
    "list_content_drafts": ActionTier.READ,
    "draft_linkedin_now": ActionTier.WRITE,
    "edit_content_draft": ActionTier.WRITE,
    "refine_content_draft": ActionTier.WRITE,
    "discard_content_draft": ActionTier.WRITE,
    # Approval feeds the publish executor queue — this IS the
    # authorize-a-post step, not a draft edit.
    "approve_content_draft": ActionTier.DESTRUCTIVE,

    # ── creators: reads ─────────────────────────────────────
    "list_astra_files": ActionTier.READ,
    "read_astra_file": ActionTier.READ,
    "show_astra_diff": ActionTier.READ,
    "list_business_kits": ActionTier.READ,
    "read_business_kit": ActionTier.READ,
    "list_creator_artifacts": ActionTier.READ,
    "list_self_improvements": ActionTier.READ,

    # ── creators: drafts + renders (recoverable artifacts) ──
    "draft_brand_kit": ActionTier.WRITE,
    "draft_caption_set": ActionTier.WRITE,
    "draft_carousel": ActionTier.WRITE,
    "draft_component_spec": ActionTier.WRITE,
    "draft_deck": ActionTier.WRITE,
    "draft_doc": ActionTier.WRITE,
    "draft_hashtag_set": ActionTier.WRITE,
    "draft_one_pager": ActionTier.WRITE,
    "draft_page_content": ActionTier.WRITE,
    "draft_site_brief": ActionTier.WRITE,
    "draft_subtitle_set": ActionTier.WRITE,
    "draft_thread": ActionTier.WRITE,
    "draft_video_brief": ActionTier.WRITE,
    "draft_voiceover_script": ActionTier.WRITE,
    "generate_hero_image": ActionTier.WRITE,
    "render_deck_pdf": ActionTier.WRITE,
    "render_deck_pptx": ActionTier.WRITE,
    "render_doc_pdf": ActionTier.WRITE,
    "render_one_pager_pdf": ActionTier.WRITE,
    "render_site_preview": ActionTier.WRITE,
    "analyze_reference_site": ActionTier.WRITE,
    "critique_artifact": ActionTier.WRITE,

    # ── creators: kit editor ────────────────────────────────
    "add_forbidden_phrase": ActionTier.WRITE,
    "add_voice_note": ActionTier.WRITE,
    "add_proof_point": ActionTier.WRITE,
    "add_audience_objection": ActionTier.WRITE,
    "commit_kit_changes": ActionTier.DESTRUCTIVE,

    # ── creators: code editor + self-improve (the
    #    self-modification chain — also interactive-only, see
    #    astra/runtime/tool_surface.py) ─────────────────────
    "edit_astra_file": ActionTier.DESTRUCTIVE,
    "write_astra_file": ActionTier.DESTRUCTIVE,
    "run_creator_tests": ActionTier.DESTRUCTIVE,   # executes repo code
    "commit_code_changes": ActionTier.DESTRUCTIVE,  # push:bool → deploy
    "revert_last_code_commit": ActionTier.DESTRUCTIVE,
    "apply_self_improvement": ActionTier.DESTRUCTIVE,
    "observe_issue": ActionTier.WRITE,
    "propose_self_improvement": ActionTier.WRITE,
    "review_proposal": ActionTier.WRITE,
    "dismiss_self_improvement": ActionTier.WRITE,

    # ── email ───────────────────────────────────────────────
    "email_digest": ActionTier.READ,
    "email_search": ActionTier.READ,
    "email_top_senders": ActionTier.READ,
    "email_unanswered": ActionTier.READ,
    "email_classify_sweep": ActionTier.WRITE,
    "mark_emails_read": ActionTier.WRITE,

    # ── fleet ───────────────────────────────────────────────
    "list_agents": ActionTier.READ,
    "recommend_agent": ActionTier.READ,

    # ── local (Mac bridge; these also declare tiers at
    #    registration in astra/runtime/tools/local.py) ───────
    "local_read": ActionTier.READ,
    "local_glob": ActionTier.READ,
    "local_grep": ActionTier.READ,
    "local_bridge_status": ActionTier.READ,
    # Spawns headless Chrome on the Mac — a physical act, not a lookup.
    "screenshot_url": ActionTier.WRITE,
    # Arbitrary file writes on the Mac — launchd plists, shell rc
    # files, the sidecar's plaintext trust root. Not "recoverable".
    "local_edit": ActionTier.DESTRUCTIVE,
    "local_write": ActionTier.DESTRUCTIVE,
    "local_bash": ActionTier.DESTRUCTIVE,

    # ── capability broker (A3) ────────────────────────────
    # The gate is the BROKER, not the tier: submit_intent files a row
    # and causes nothing, and no part of Astra can approve it. But WRITE
    # rather than READ, because it inserts a durable row that interrupts
    # a human — READ means "changes nothing", and queueing work for
    # someone changes something.
    "submit_intent": ActionTier.WRITE,
    "poll_status": ActionTier.READ,

    # ── memory ──────────────────────────────────────────────
    "recall_memories": ActionTier.READ,
    "recall_recent_turns": ActionTier.READ,
    "list_memories": ActionTier.READ,
    "memory_stats": ActionTier.READ,
    "store_memory": ActionTier.WRITE,
    "forget_memory": ActionTier.DESTRUCTIVE,

    # ── notes ───────────────────────────────────────────────
    "notes_get": ActionTier.READ,
    "notes_list": ActionTier.READ,
    "notes_search": ActionTier.READ,
    "notes_sync": ActionTier.WRITE,

    # ── objectives ──────────────────────────────────────────
    "list_objectives": ActionTier.READ,
    "create_objective": ActionTier.WRITE,
    "close_objective": ActionTier.WRITE,

    # ── railway_ops ─────────────────────────────────────────
    "agent_logs": ActionTier.READ,
    "list_scheduled_jobs": ActionTier.READ,
    "restart_agent": ActionTier.DESTRUCTIVE,

    # ── replies ─────────────────────────────────────────────
    "list_pending_replies": ActionTier.READ,
    "reply_draft_metrics": ActionTier.READ,
    "voice_profiles": ActionTier.READ,
    "draft_personal_reply": ActionTier.WRITE,
    "refine_reply_draft": ActionTier.WRITE,
    "discard_reply_draft": ActionTier.WRITE,
    "ingest_voice_export": ActionTier.WRITE,
    "learn_my_voice": ActionTier.WRITE,
    "mine_my_voice": ActionTier.WRITE,
    # POSTs to the email agent's /drafts/{id}/send — actually sends.
    "send_reply_draft": ActionTier.DESTRUCTIVE,

    # ── research ────────────────────────────────────────────
    "research": ActionTier.WRITE,
    "research_get": ActionTier.READ,
    "research_list": ActionTier.READ,

    # ── shares ──────────────────────────────────────────────
    "get_share": ActionTier.READ,
    "list_recent_shares": ActionTier.READ,
    "search_shares": ActionTier.READ,

    # ── system ──────────────────────────────────────────────
    "health_check": ActionTier.READ,
    "system_info": ActionTier.READ,
    "tunnel_status": ActionTier.READ,
    "trigger_briefing": ActionTier.WRITE,
    "trigger_consolidation": ActionTier.WRITE,
    "trigger_fleet_health": ActionTier.WRITE,
    # Exposes localhost to the internet / kills webhook connectivity.
    "start_tunnel": ActionTier.DESTRUCTIVE,
    "stop_tunnel": ActionTier.DESTRUCTIVE,

    # ── tasks ───────────────────────────────────────────────
    "list_tasks": ActionTier.READ,
    "add_task": ActionTier.WRITE,
    "complete_task": ActionTier.WRITE,

    # ── training ────────────────────────────────────────────
    "training_status": ActionTier.READ,
    "log_training": ActionTier.WRITE,
}


def get_action_tier(tool_name: str) -> ActionTier:
    """Get the action tier for a tool.

    Defaults to DESTRUCTIVE if unknown (CONTAINMENT §1). An
    unclassified tool must land in the tier that asks a human, not
    the tier that auto-executes in semi_auto.
    """
    return TOOL_TIERS.get(tool_name, ActionTier.DESTRUCTIVE)


def get_permission_for_tier(
    mode: AutonomyMode, tier: ActionTier
) -> PermissionDecision:
    """Permission decision for a KNOWN tier.

    This is the path the lean runtime uses: the tool registry already
    declares every tool's tier at registration (ToolDef.tier), so the
    gate must trust that — not the name-keyed TOOL_TIERS map below.
    The map only knows 36 legacy names out of 117 registered tools;
    everything else silently fell to WRITE, which auto-allowed
    local_bash (arbitrary shell on Kunal's Mac, registered
    DESTRUCTIVE) in semi_auto because the map only listed the old
    SDK name "Bash". Same split-brain class as the autonomy-mode bug
    fixed in 7374fd7: two sources of truth, the stale one consulted.
    """
    return PERMISSION_MATRIX[mode][tier]


def get_permission(mode: AutonomyMode, tool_name: str) -> PermissionDecision:
    """Name-based permission lookup — LEGACY path.

    Only for callers that genuinely have no ToolDef (SDK-era hooks,
    tests). Anything with access to the tool registry must use
    get_permission_for_tier with the registered tier instead. Unknown
    names now fall to DESTRUCTIVE (ask), closing the
    silent-permission-bypass the old WRITE default caused.
    """
    tier = get_action_tier(tool_name)
    return get_permission_for_tier(mode, tier)
