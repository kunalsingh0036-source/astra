# Migration debt audit — tools surface

Audit of every tool defined in `astra/tools/*.py` plus the Phase-1-ported runtime tools in `astra/runtime/tools/`. For each: is its factory imported by `astra/runtime/tools/__init__.py`, what wire format does its handler return, and is that wire format actually consumable by the lean runtime in `astra/runtime/agent_loop.py`? The audit covers three known bug classes — broken sentinels, side-channel emissions, and unregistered factories — and a fourth that surfaced during the pass: namespace mis-attribution from the cross-bundled `creators` factory. No fixes; the spawned synthesis pass owns those.

## Tool-by-tool

| Tool | File | Registered? | Wire format | Gap notes | Priority |
| --- | --- | --- | --- | --- | --- |
| `store_memory` | `astra/tools/memory_tools.py` | Yes (`_imp_memory`) | `{content:[text]}` plain | OK | — |
| `recall_memories` | `astra/tools/memory_tools.py` | Yes (`_imp_memory`) | `{content:[text]}` plain | OK | — |
| `forget_memory` | `astra/tools/memory_tools.py` | Yes (`_imp_memory`) | `{content:[text]}` plain | OK | — |
| `list_memories` | `astra/tools/memory_tools.py` | Yes (`_imp_memory`) | `{content:[text]}` plain | OK | — |
| `memory_stats` | `astra/tools/memory_tools.py` | Yes (`_imp_memory`) | `{content:[text]}` plain | OK | — |
| `recall_recent_turns` | `astra/tools/memory_tools.py` | Yes (`_imp_memory`) — but shadowed | `{content:[text]}` plain | Phase-1 port in `astra/runtime/tools/memory.py` registers the same name FIRST (returns bare `str`). Adapter `skip_existing=True` causes the SDK version to be silently skipped. Both work; only the runtime-port version actually dispatches. Cosmetic redundancy. | P2 |
| `recall_recent_turns` (port) | `astra/runtime/tools/memory.py` | Direct register | bare `str` | Normalized by `_normalize` to a ToolResult — fine. | — |
| `list_recent_shares` | `astra/tools/shares_tools.py` | Yes (`_imp_shares`) | `{content:[text]}` plain | OK | — |
| `search_shares` | `astra/tools/shares_tools.py` | Yes (`_imp_shares`) | `{content:[text]}` plain | OK | — |
| `get_share` | `astra/tools/shares_tools.py` | Yes (`_imp_shares`) | `{content:[text]}` plain | OK | — |
| `calendar_status` | `astra/tools/calendar_tools.py` | Yes (`_imp_calendar`) | `{content:[text]}` plain | OK | — |
| `calendar_today` | `astra/tools/calendar_tools.py` | Yes (`_imp_calendar`) | `{content:[text]}` plain | OK | — |
| `calendar_tomorrow` | `astra/tools/calendar_tools.py` | Yes (`_imp_calendar`) | `{content:[text]}` plain | OK | — |
| `calendar_week` | `astra/tools/calendar_tools.py` | Yes (`_imp_calendar`) | `{content:[text]}` plain | OK | — |
| `calendar_search` | `astra/tools/calendar_tools.py` | Yes (`_imp_calendar`) | `{content:[text]}` plain | OK | — |
| `email_digest` | `astra/tools/email_tools.py` | Yes (`_imp_email`) | `{content:[text]}` plain | OK | — |
| `email_unanswered` | `astra/tools/email_tools.py` | Yes (`_imp_email`) | `{content:[text]}` plain | OK | — |
| `email_search` | `astra/tools/email_tools.py` | Yes (`_imp_email`) | `{content:[text]}` plain | OK | — |
| `email_top_senders` | `astra/tools/email_tools.py` | Yes (`_imp_email`) | `{content:[text]}` plain | OK | — |
| `email_classify_sweep` | `astra/tools/email_tools.py` | Yes (`_imp_email`) | `{content:[text]}` plain | OK | — |
| `browser_fetch` | `astra/tools/browser_tools.py` | Yes (`_imp_browser`) | `{content:[text]}` plain | OK | — |
| `browser_search` | `astra/tools/browser_tools.py` | Yes (`_imp_browser`) | `{content:[text]}` plain | OK | — |
| `emit_table` | `astra/tools/artifact_tools.py` | Yes (`_imp_artifacts`) | Sentinel `⟦ASTRA_ARTIFACT⟧{…json…}⟦/ASTRA_ARTIFACT⟧` | Parser handles. | — |
| `emit_draft` | `astra/tools/artifact_tools.py` | Yes (`_imp_artifacts`) | Sentinel | Parser handles. | — |
| `emit_palette` | `astra/tools/artifact_tools.py` | Yes (`_imp_artifacts`) | Sentinel | Parser handles. | — |
| `emit_metric` | `astra/tools/artifact_tools.py` | Yes (`_imp_artifacts`) | Sentinel | Parser handles. | — |
| `prepare_preview` | `astra/tools/artifact_tools.py` | Yes (`_imp_artifacts`) | Sentinel (inline + url modes) | Parser handles. | — |
| `get_mode` | `astra/tools/autonomy_tools.py` | Yes (`_imp_autonomy`) | `{content:[text]}` plain | OK | — |
| `set_mode` | `astra/tools/autonomy_tools.py` | Yes (`_imp_autonomy`) | `{content:[text]}` plain | OK | — |
| `get_audit_log` | `astra/tools/autonomy_tools.py` | Yes (`_imp_autonomy`) | `{content:[text]}` plain | OK | — |
| `audit_stats` | `astra/tools/autonomy_tools.py` | Yes (`_imp_autonomy`) | `{content:[text]}` plain | OK | — |
| `list_agents` | `astra/tools/agent_fleet_tools.py` | Yes (`_imp_fleet`) | `{content:[text]}` plain | OK | — |
| `agent_status` | `astra/tools/agent_fleet_tools.py` | Yes (`_imp_fleet`) | `{content:[text]}` plain | OK | — |
| `recommend_agent` | `astra/tools/agent_fleet_tools.py` | Yes (`_imp_fleet`) | `{content:[text]}` plain | OK | — |
| `fleet_summary` | `astra/tools/agent_fleet_tools.py` | Yes (`_imp_fleet`) | `{content:[text]}` plain | OK | — |
| `notes_search` | `astra/tools/notes_tools.py` | Yes (`_imp_notes`) | `{content:[text]}` plain | OK | — |
| `notes_list` | `astra/tools/notes_tools.py` | Yes (`_imp_notes`) | `{content:[text]}` plain | OK | — |
| `notes_get` | `astra/tools/notes_tools.py` | Yes (`_imp_notes`) | `{content:[text]}` plain | OK | — |
| `notes_sync` | `astra/tools/notes_tools.py` | Yes (`_imp_notes`) | `{content:[text]}` plain | OK | — |
| `add_task` | `astra/tools/task_tools.py` | Yes (`_imp_tasks`) | `{content:[text]}` plain | OK | — |
| `list_tasks` | `astra/tools/task_tools.py` | Yes (`_imp_tasks`) | `{content:[text]}` plain | OK | — |
| `complete_task` | `astra/tools/task_tools.py` | Yes (`_imp_tasks`) | `{content:[text]}` plain | OK | — |
| `research` | `astra/tools/research_tools.py` | Yes (`_imp_research`) | `{content:[text]}` plain | OK | — |
| `research_list` | `astra/tools/research_tools.py` | Yes (`_imp_research`) | `{content:[text]}` plain | OK | — |
| `research_get` | `astra/tools/research_tools.py` | Yes (`_imp_research`) | `{content:[text]}` plain | OK | — |
| `list_business_kits` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `read_business_kit` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_deck` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_one_pager` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_doc` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_brand_kit` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `critique_artifact` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `generate_hero_image` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | Image bytes are stored on the artifact row, NOT emitted via the artifact sentinel. UI can't render the image inline from the tool result — must re-fetch by artifact id. Not a wire-format break, but an asymmetry vs. `screenshot_url` (which DOES emit a sentinel image). | P1 |
| `render_deck_pdf` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | Returns R2 signed URL as text. No artifact sentinel — user sees a URL in chat, not an inline preview card. Same asymmetry as `generate_hero_image`. | P2 |
| `render_deck_pptx` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | Same as above. | P2 |
| `render_one_pager_pdf` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | Same as above. | P2 |
| `render_doc_pdf` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | Same as above. | P2 |
| `analyze_reference_site` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_site_brief` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_page_content` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_component_spec` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `render_site_preview` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | Returns URL as text; no `prepare_preview` artifact wrapper, so the user has to click a URL rather than scrub through the preview inline. Consistency gap. | P2 |
| `draft_carousel` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_thread` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_caption_set` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_hashtag_set` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_video_brief` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_voiceover_script` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `draft_subtitle_set` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `list_creator_artifacts` | `astra/tools/creator_tools.py` | Yes (`_imp_creators`) | `{content:[text]}` plain | OK | — |
| `system_info` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | OK | — |
| `health_check` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | OK | — |
| `trigger_briefing` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | OK — note: never sets `is_error=True` on except path; failures are returned as a "Failed to run …" text block. P2 cosmetic. | P2 |
| `trigger_fleet_health` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | Same — no `is_error`. | P2 |
| `trigger_consolidation` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | Same — no `is_error`. | P2 |
| `start_tunnel` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | OK | — |
| `stop_tunnel` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | OK | — |
| `tunnel_status` | `astra/tools/system_tools.py` | Yes (`_imp_system`) | `{content:[text]}` plain | OK | — |
| `start_service` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `stop_service` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `start_fleet` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `stop_fleet` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `fleet_status` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `fleet_health` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `service_logs` | `astra/tools/service_tools.py` | Yes (`_imp_services`) | `{content:[text]}` plain | OK | — |
| `discover_agent` | `astra/tools/a2a_tools.py` | Yes (`_imp_a2a`) | `{content:[text]}` plain | OK | — |
| `send_a2a_task` | `astra/tools/a2a_tools.py` | Yes (`_imp_a2a`) | `{content:[text]}` plain | OK | — |
| `get_a2a_task` | `astra/tools/a2a_tools.py` | Yes (`_imp_a2a`) | `{content:[text]}` plain | OK | — |
| `cancel_a2a_task` | `astra/tools/a2a_tools.py` | Yes (`_imp_a2a`) | `{content:[text]}` plain | OK | — |
| `list_discovered_agents` | `astra/tools/a2a_tools.py` | Yes (`_imp_a2a`) | `{content:[text]}` plain | OK | — |
| `a2a_health_check` | `astra/tools/a2a_tools.py` | Yes (`_imp_a2a`) | `{content:[text]}` plain | OK | — |
| `read_astra_file` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Registered under namespace="creators" by `_imp_creators` (first wins), then `_imp_code_editor` is skipped via `skip_existing`. Tool works; namespace label is wrong. | P2 |
| `list_astra_files` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `edit_astra_file` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `write_astra_file` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `show_astra_diff` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `run_creator_tests` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `commit_code_changes` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `revert_last_code_commit` | `astra/tools/code_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `add_forbidden_phrase` | `astra/tools/kit_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same namespace-collision pattern as code_editor. | P2 |
| `add_voice_note` | `astra/tools/kit_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `add_proof_point` | `astra/tools/kit_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `add_audience_objection` | `astra/tools/kit_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `commit_kit_changes` | `astra/tools/kit_editor_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `observe_issue` | `astra/tools/self_improve_tools.py` | Yes (twice) | `{content:[text]}` plain | Same namespace-collision pattern. | P2 |
| `list_self_improvements` | `astra/tools/self_improve_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `propose_self_improvement` | `astra/tools/self_improve_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `review_proposal` | `astra/tools/self_improve_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `apply_self_improvement` | `astra/tools/self_improve_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `dismiss_self_improvement` | `astra/tools/self_improve_tools.py` | Yes (twice) | `{content:[text]}` plain | Same. | P2 |
| `local_read` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `local_write` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `local_edit` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `local_bash` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `local_glob` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `local_grep` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `local_bridge_status` | `astra/runtime/tools/local.py` | Direct register | `{content:[text]}` plain | OK | — |
| `screenshot_url` | `astra/runtime/tools/local.py` | Direct register | Sentinel (`type:"image"`) | Parser handles. Note: artifact payload's `type` is `"image"` — the agent loop yields `artifact(type="image", …)`. UI must render images from the `image` artifact type. | — |

## Findings summary

**P0 (broken in prod):** none. No sentinel-emitting tool is unparseable. No factory is unimported. No side-channel event emissions (queue puts, callbacks, direct event_emitter writes) exist in any `astra/tools/*.py` file — every handler returns either a string, an SDK-shape dict `{content:[{type:"text", text:…}]}`, or a sentinel-wrapped variant of the same. `_normalize` in `tool_registry.py` handles all three. The cross-runtime contract is intact.

**P1 (works but fragile):**
- `generate_hero_image` (`astra/tools/creator_tools.py`) stores rendered PNG bytes on the artifact row but returns only a text summary. There is NO inline image artifact emission — the model has to tell the user "image saved as artifact #N" and the user has to navigate elsewhere to see it. `screenshot_url` (`astra/runtime/tools/local.py`) emits a `type:"image"` sentinel and renders inline. Consistency gap; user-visible UX miss for what is one of the most demo-worthy tools.

**P2 (cosmetic / future cleanup):**
- `recall_recent_turns` is registered twice (Phase-1 port wins, SDK version silently skipped). One should be the single source of truth.
- Namespace mis-attribution for 19 tools: `code_editor_tools.py`, `kit_editor_tools.py`, `self_improve_tools.py` tools are first registered with `namespace="creators"` because `creator_tools.py`'s `create_creators_mcp_server()` embeds them via `*CODE_EDITOR_TOOLS` / `*KIT_EDITOR_TOOLS` / `*SELF_IMPROVE_TOOLS`. The dedicated `_imp_code_editor` / `_imp_kit_editor` / `_imp_self_improve` bridges run AFTER and silently skip via `skip_existing=True`. The dedicated namespaces in `__init__.py` therefore log "registered 0 tool(s)" — exactly the silent-drift symptom the bridge was built to prevent. Tools work; namespace-scoped tool subsets (`tool_namespaces=["code_editor"]`) would return zero tools.
- Render tools (`render_deck_pdf`, `render_deck_pptx`, `render_one_pager_pdf`, `render_doc_pdf`, `render_site_preview`) return R2 signed URLs as plain text. None wrap the result in a `prepare_preview` sentinel for inline UI rendering. Asymmetry with how `prepare_preview` itself works.
- `trigger_briefing`, `trigger_fleet_health`, `trigger_consolidation` never set `is_error=True` on the exception path — failures surface as text only, so the autonomy / audit layer can't distinguish "ran and succeeded" from "raised". Cheap to fix.
