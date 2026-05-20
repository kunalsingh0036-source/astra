# Calendared technical debt

Items that are deliberately left in place TEMPORARILY with a known
revisit date. Don't add things here without a specific date — drift
is the failure mode. If something doesn't have a date, it goes in
GitHub issues or docs/ instead.

When you (or Astra) hit a session, scan this file. Past-due items
get the next ~1 hour of work to retire them.

---

## 2026-06-06 — Audit logger: DB-backed reads from agent tools

**What:** `get_audit_log_tool` and `audit_stats_tool` in
`astra/tools/autonomy_tools.py` currently read from
`audit_logger._entries` — a Python-process-local list. The web UI
already reads from the `audit_events` table (correct). Migrate the
tool path to query the same table so agent answers to "what did
you do today" reflect cross-service activity, not just the actions
this uvicorn worker logged.

**Why deferred:** Same class of bug as the autonomy mode split-brain
(commit `7374fd7`), but lower-impact: the audit log is mostly a
debugging surface, not enforcement. P1, not P0.

**Files:**
- `astra/autonomy/audit.py` — add `async get_entries_from_db(...)`
  and `async get_stats_from_db(...)` that SELECT from `audit_events`
  with the same filters the in-memory versions support.
- `astra/tools/autonomy_tools.py` — switch both tools to the new
  async path.
- Keep the in-memory list as a hot cache for the existing audit_logger
  consumers (autonomy hooks). DB read is for cross-service visibility.

**Acceptance:** ask Astra "what tools did you run today" in a fresh
session; response includes actions taken by both stream service and
scheduler in the same window.

---

## 2026-06-06 — Autonomy: persist mode-transition history

**What:** `autonomy_manager._history` (in
`astra/autonomy/manager.py`) is a per-process list of mode
transitions. The `_mode` value itself is now synced via
`refresh_from_db()`, but the journal of how we got here is still
local. Add a small `autonomy_mode_history` table and write a row
on every `set_mode` / `complete_task` / `_check_revert` /
`refresh_from_db` change.

**Why deferred:** Same class as #1 above. Useful for debugging
"why was the agent in always_ask at 14:30" but not blocking. P1.

**Files:**
- New migration: `autonomy_mode_history(id PK, from_mode, to_mode,
  reason, source TEXT, at TIMESTAMPTZ)`.
- `astra/autonomy/manager.py` — fire-and-forget INSERT on every
  transition. Keep `_history` list as a hot in-memory cache.
- `get_status()` / `get_history()` SELECT from the table.

**Acceptance:** transitions logged in stream service show up when
queried via scheduler-service tools, and vice versa.

---

## 2026-06-13 — Multi-replica turn cancel

**What:** `_running_turns: dict[int, asyncio.Task]` in
`services/stream/main.py:790` tracks in-flight turn tasks for the
`/turns/<id>/cancel` endpoint. Works today only because Railway
runs one uvicorn worker per stream service slot. If we ever
autoscale, blue-green deploy, or run a second replica, cancel
silently breaks — the cancel request hits replica A while the turn
is running on replica B; A finds no task, returns 200, user thinks
it worked.

**Why deferred:** Latent. Not biting today because the deploy
topology is single-replica. Becomes a P0 the moment we change that.

**Two options:**
1. **DB-poll cancel flag** — add `turns.cancel_requested BOOL`; the
   in-flight task polls it every few seconds and exits cleanly when
   set. Any service can flip the flag. Simple, slightly higher DB
   load.
2. **Redis pub/sub** — `/turns/<id>/cancel` publishes to
   `cancel:<id>`; every replica subscribes. Lower latency, adds
   Redis as a hard dep for cancel (Astra has Redis already for
   Celery, so this is mostly free).

Pick (1) first — it's simpler and the latency is acceptable for the
cancel UX. Move to (2) only if profiling shows the poll is heavy.

**Files:**
- New migration: add column `cancel_requested BOOLEAN NOT NULL
  DEFAULT FALSE` to `turns`.
- `services/stream/main.py` — cancel endpoint flips the flag; agent
  loop polls the flag once per tool iteration and aborts cleanly.
- Smoke test: a journey that starts a long turn, hits cancel,
  asserts the turn record ends with `status='cancelled'`.

**Acceptance:** can cancel a turn whose runner is on a different
replica than the one that received the cancel request. Verifiable
by running two `services/stream` instances locally and confirming
cancel works across them.

---

(Add new entries here as they're calendared.)
