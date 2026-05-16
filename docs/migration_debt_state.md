# Migration debt: cross-service state split-brain audit

Astra runs as several independent Railway containers (`stream`, `scheduler`,
`email_agent`, `finance`, `gateway`) plus the Vercel-hosted `astra-web`
Next.js service. They share Postgres but each Python process has its own
memory. Any mutable state that lives in a module-level singleton, a
boot-time-loaded `pydantic-settings` Settings, or a class instance
attribute is invisible to every other process — if a second writer
exists, the two diverge silently. This audit lists every candidate the
grep turned up, classified by whether multiple services actually touch
it. The `autonomy_manager._mode` bug (fixed in `7374fd7`) is the
canonical instance of this pattern; we're hunting siblings.

| # | State item (file:line) | Writer(s) | Reader(s) | Split-brain? | Fix sketch | Priority |
|---|------------------------|-----------|-----------|--------------|------------|----------|
| 1 | `audit_logger._entries` list — `astra/autonomy/audit.py:71` (singleton at :179) | `audit_logger.log()` from every Python service that runs the agent loop / autonomy hooks (stream, scheduler) — each writes to its OWN in-memory list AND fire-and-forgets a row to `audit_events` table | astra-web `/api/audit` reads from DB (correct); agent tools `get_audit_log_tool` + `audit_stats_tool` (`astra/tools/autonomy_tools.py:114,140`) read from in-memory list only | Y | Make `get_entries()` / `get_stats()` query `audit_events` via SQL when called from a tool path; treat the in-memory list as a hot cache only. | P1 — when Kunal asks Astra "what did you do today" in the stream service, it can only see actions THIS uvicorn worker performed; scheduler-side actions are invisible until DB-backed read is added. |
| 2 | `autonomy_manager._history` list — `astra/autonomy/manager.py:110` | `set_mode`, `complete_task`, `_check_revert`, `refresh_from_db` — all on the local singleton; never persisted | Same singleton's `get_history()` exposed via autonomy tools | Y | Persist mode transitions to a small `autonomy_mode_history` table; tool reads from DB. The `_mode` fix only synced current value, not the journal. | P1 — fixing `_mode` mid-flight means transitions logged in stream are invisible to scheduler / future debugging; same root cause as the bug just fixed. |
| 3 | `agent_registry._agents` dict — `astra/agents/registry.py:79` (singleton :123) | Populated at boot by `register_all_external_agents()` in `astra/agents/external/registry.py`; `record_usage()` increments `usage_count` and updates `last_used` purely in-memory | Agent fleet tools (`astra/tools/agent_fleet_tools.py:32,63,126`), recommender (`astra/agents/recommender.py:140`) | Y for `usage_count` / `last_used`; N for definitions (deterministic from code) | Move usage counters to an `agent_usage` table keyed by name; static definitions stay in code. | P2 — counters are advisory only; nothing user-facing yet, but the "most-used agent" stat the recommender wants is currently per-process noise. |
| 4 | `settings.briefing_hour` / `briefing_minute` / `consolidation_hour` — `astra/config.py:82-85`, read in `astra/scheduler/app.py:95,96,151` | Env var only (boot-time) | APScheduler `CronTrigger` at scheduler boot | N today (no UI writer) | If a UI/CLI ever sets briefing times, store in `app_settings`; have a refresh job re-register the cron trigger. | P2 — purely latent; no second writer exists yet. |
| 5 | `settings.notes_writeback_mode` — `astra/config.py:38`, read in `astra/scheduler/catchup.py:388` | Env var only | Scheduler catchup job, per-fire | N today | Same as #4 — DB-backed if UI added. | P2 |
| 6 | `settings.briefing_channel` — `astra/config.py:44`, read in `astra/scheduler/jobs.py:836`, `astra/scheduler/catchup.py:88` | Env var only | Scheduler jobs, per-fire | N today | Same as #4. | P2 |
| 7 | `settings.default_autonomy_mode` — `astra/config.py:32`, read in `AutonomyManager.__init__` (`astra/autonomy/manager.py:105`) | Env var only | Stream service at boot (now superseded by `refresh_from_db()` each turn) | N (fixed via `refresh_from_db()`) | Already addressed by commit `7374fd7`. | P2 |
| 8 | `_running_turns: dict[int, asyncio.Task]` — `services/stream/main.py:790` | Stream service's own `/turns/start` writes; `/turns/<id>/cancel` reads | Only the stream service (cancel proxied by astra-web's `/api/turns/[id]/cancel/route.ts`) | Y if stream service ever runs >1 uvicorn worker or replicas; N today (single worker per Railway slot) | Move task tracking to Redis (or rely on DB `turns.status='cancel_requested'` flag that the running task polls). | P1 — Railway autoscale or a second deployment for blue-green will silently break cancel; current "best effort 200" mask the bug. |
| 9 | `agent_discovery._cache` dict (`astra/a2a/discovery.py:85`, singleton :257) + 300s TTL | Each process discovers/registers locally at boot; per-process TTL re-fetch | Same process only — A2A tools | N (self-healing via TTL) | None needed; cache is per-process by design. | P2 |
| 10 | `tunnel_manager._public_url` / `_process` — `astra/services/tunnel.py:275` | Only the local host's tunnel runner | Status tool on same host | N (local-only daemon) | None. | P2 |
| 11 | `service_manager` — `astra/services/manager.py:525` | Local pid-file + filesystem | Local CLI / health endpoint | N (local-only) | None. | P2 |
| 12 | `_push_counter` dict — `services/email_agent/api/routes/webhook.py:33` | Gmail webhook handler (single service) | `/webhook/gmail/diag` on same service | N (diagnostic, single-writer single-reader) | Acceptable. | P2 |
| 13 | `_scheduler` singleton — `astra/scheduler/app.py:58` | Scheduler service boot only; job definitions hard-coded; jobstore is Postgres-backed | Scheduler service introspects own jobstore | N (jobstore IS the cross-process source of truth — APScheduler does this right) | None. | P2 |
| 14 | `_get_model` `@lru_cache` — `astra/memory/embeddings.py:19` | Model file on disk loaded once | Local process | N (deterministic, model identity tied to `settings.embedding_model`) | None. | P2 |
| 15 | `_DEFAULT_MODE = "semi_auto"` constant — `astra-web/app/api/autonomy/route.ts:27` | N/A | Returned to client when `app_settings` row is missing | N (DB is the read source); but note **mismatch**: backend `Settings.default_autonomy_mode = "always_ask"` (`astra/config.py:32`). Cold-start before any UI write, Python uses `always_ask` and astra-web shows `semi_auto`. | Pick one default and source both from same constant (or seed the `app_settings` row on first boot). | P1 — directly user-visible: first-time UI shows "semi_auto" while the agent enforces "always_ask" until the user clicks anything. |

## Findings summary

P0 (causing user-visible bugs): none beyond the already-fixed `autonomy_manager._mode`.

P1 (latent, will bite):
- **#1 `audit_logger._entries`** — agent's in-process audit log diverges from the DB the web shows; tool answers will be wrong for cross-service activity.
- **#2 `autonomy_manager._history`** — mode-transition journal is per-process; same class of bug as the `_mode` fix but not addressed by it.
- **#8 `_running_turns` dict in stream** — cancel breaks the moment stream service is replicated or restarted mid-flight; today's "best effort 200" hides the failure.
- **#15 `_DEFAULT_MODE` mismatch** — astra-web defaults to `semi_auto`, Python defaults to `always_ask`; first-load UI lies about agent behavior until first DB write.

P2 (theoretical): briefing/consolidation/notes_writeback/default_autonomy settings (#4–#7) — safe today because no second writer exists, but the moment a `/settings` form is added for any of them, they become #1-class bugs. Worth adding a TODO in `app/settings/page.tsx` that any new toggle MUST round-trip via `app_settings`, not env vars.
