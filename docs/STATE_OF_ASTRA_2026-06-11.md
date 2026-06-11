# State of Astra — full-portfolio deep scan, 2026-06-11

Nine-agent scan against the original scope: compass memory files, system
prompt, git history from the first commit, all five services, the web app,
every subsystem, the test/CI layer, and a live production probe (15/15
smoke green during the scan). Every claim below carries file:line,
endpoint, or test evidence — pulled from the scan transcript at
`subagents/workflows/wf_d4344c9b-fca`.

---

## 0. Incidents surfaced BY the scan (disclosed + remediated)

1. **Local pytest mutates the production database.** `.env`'s
   `DATABASE_URL` points at Railway prod; `tests/test_memory/
   test_store_and_retrieval.py:28` builds its engine straight from
   `settings.database_url` with no test-host guard and no rollback.
   Running the suite (a) wiped all WORKING-type memories via
   `test_clear_working_memory` (unscoped DELETE, `astra/memory/store.py:132`)
   and (b) inserted 10 junk fixture memories into real semantic recall.
   **Remediated 2026-06-11:** the 10 junk rows were verified by ID and
   deleted. The wiped working memories are unrecoverable but transient
   by design (nightly consolidation prunes them). **Open fix:** conftest
   guard refusing DB tests unless DATABASE_URL matches a test host +
   per-test transaction rollback. Until that lands, `pytest tests/test_memory/`
   IS a prod incident.

2. **email-agent and whatsapp-gateway upstream APIs are publicly
   unauthenticated.** `curl https://email.thearrogantclub.com/api/v1/messages/?limit=1`
   → 200 with no credentials; POST send reaches FastAPI validation
   (422 = no auth layer in front). Anyone on the internet can read synced
   mail and send email/WhatsApp as Kunal. astra-web's `/api/artifact/send`
   depends on this openness (forwards no secret despite its docstring).
   Mitigating: the email store is currently EMPTY (see §4-P0-2), so blast
   radius today is "send-as-me," not "read-my-archive." Still the top fix.

---

## 1. The original remit (scope reconstruction)

Conceived April 2026 as a single-user "Jarvis-level" agent OS:
*"Not a chatbot. An autonomous agent with persistent memory, deep tool
surface, and the ability to take real actions on Kunal's computer, his
businesses, and his digital life"* (system_prompt.py). Vision shape from
operating_mode.md: (1) one persistent agent across businesses/calendar/
comms/code; (2) specialist depth per business; (3) **proactive, not
reactive** — "the chat is the LAST resort for 'what should I do today'";
(4) Mac-app reliability; (5) self-improving; (6) local bridge as
first-class. Serves all four businesses in priority order (HelmTech >
Apex > BAY > Top Studios); HelmTech doubly so — "Astra is effectively
Shotgun v0 for one user." Success bar: daily operating rhythm (silent
triage before 13:00, 22:00 briefing), zero visible timeouts/lost
sessions, compass-filtered features only.

Context note: status memory records Kunal calling Astra "a failed agent
rn" (2026-06-09); new agents now default standalone-but-pluggable. This
scan explains *why* it feels failed despite green dashboards — see §5.

---

## 2. Scorecard by layer

| Layer | Bar (compass) | Verdict | Evidence |
|---|---|---|---|
| Chat runtime | No timeouts, no lost sessions, no silent failures | **~90% — genuinely strong** | 97/97 runtime tests; 15/15 prod smoke; artifacts/vision/cancel/compaction/rehydration all wired with named tests |
| Tool surface | Deep + dependable | **~75%** | 117 tools, 16 namespaces, zero registration drift; but tier-gating split-brain + 5 phantom tools in system prompt |
| Web app | Reliable daily surface | **~85%** | 22 pages, 48 routes, tsc clean, default-deny auth, all 6 artifact kinds render; zero frontend tests |
| Memory | Persistent, consolidating | **~70%** | Recall + nightly consolidation wired; but post-turn extraction dead since May 4 and image uploads poison session history |
| Autonomy | Staged trust | **~50%** | Mode sync fixed cross-service; but always_ask doesn't ask, local_bash bypasses tiers, time-scoped modes never revert |
| Proactive layer | "Briefing answers the day" | **~25% — the headline failure** | 21 jobs registered, but email signal dead, fleet health measures a decommissioned laptop fleet, Apple-Notes jobs can't run on Linux, calendar creds laptop-only |
| Fleet orchestration | A2A across 7 agents | **~15% — dead** | Protocol code complete + tested, but `register_all_external_agents()` lost its only caller in the SDK removal — discovery is never populated |
| Channels | Email/WA/share-sheet as primary signal | **~10%** | Email pipeline end-to-end dead in prod (0 messages); no WhatsApp path to Astra itself ("notify Astra" promised in router.py:8, never implemented) |
| Security | Single-user fortress | **~40%** | NextAuth allowlist + middleware solid; but upstream agents unauthenticated, stream auth fail-open on empty env, share tokens plaintext/no-expiry, local_bash auto-runs in semi_auto |
| Cost/ops | <$1/day, bounded | **~60%** | ~$0.12/day; but no prompt caching (2-5× avoidable input spend), no retention on any table, previews sweep is dead code |

---

## 3. What's working (evidence-backed)

- **Turn lifecycle end-to-end**: create → durable events → tools →
  artifacts → finalize → poll-terminal. 97/97 runtime tests.
  (agent_loop.py, stream main.py:727-805)
- **Artifact pipeline**: all 6 kinds (table/draft/metric/palette/preview/
  image) render end-to-end; sentinel parser + scrub + regression tests.
- **Vision/attachments**: drag/drop/paste/picker → multipart → image
  content blocks → thumbnails. Verified live (smoke test 12).
- **Compaction**: pair-atomic truncation, orphan repair, 22 tests.
- **Session rehydration** across deploys with the quadratic-duplication fix.
- **Cancel**: server task actually stops, row finalizes as interrupted.
- **117 tools / 16 namespaces** via constructor bridge — no stale-import drift.
- **Autonomy mode cross-service sync** (refresh_from_db each turn; the
  split-brain class is fixed for `_mode`).
- **Memory**: MiniLM embeddings prewarmed, pgvector retrieval with
  importance re-rank, 5-step nightly consolidation on the scheduler.
- **Creators**: 44 tools, 203 tests green, all 4 business kits carry real
  content (~18k words; Top Studios voice locked 2026-05-19).
- **Web**: tsc clean, default-deny middleware verified live (401s),
  HealthBadge tri-state, push notifications, dictation, sessions browse/
  resume, deterministic chat intercepts.
- **CI**: check.yml (import smoke + alembic single-head) + smoke.yml
  (15 journeys vs prod on every push, hard-fails without secret).
- **Secrets hygiene**: no keys in git history (pickaxe-verified), bridge
  tokens hashed, .env untracked.
- **Prod**: all 7 deep-health checks ok; 6/7 agents reachable (bookkeeper
  dim = undeployed, expected); 15/15 smoke in 56s during this scan.

---

## 4. What's broken

### P0 — act now

1. **Upstream agent APIs publicly unauthenticated** (email read/send,
   WhatsApp send). Fix: shared-secret middleware on both services +
   forward the secret from astra-web (`/api/artifact/send` claims to but
   doesn't). ~half-day.
2. **Email pipeline end-to-end dead in prod**: store has 0 messages.
   Gmail sync is a Celery task but no Celery worker/beat exists in the
   Railway topology; the cloud scheduler's email jobs all hit a hardcoded
   `localhost:8005` (`astra/email/client.py:23`) that returns empty-on-error
   by design — so `inbox_preview` cheerfully reports "inbox clean" forever.
   Fix: point client at `https://email.thearrogantclub.com` via env;
   replace Celery dependency with APScheduler job or deploy a worker. ~1 day.
3. **Fleet/A2A discovery never populated**: `register_all_external_agents()`
   has zero callers since SDK-removal commit 5f2d256. Every A2A tool and
   the recommender run against an empty registry. Fix: call it at stream
   startup. ~1 hour, restores an entire advertised capability.
4. **Local pytest hits prod DB** (incident above). Fix: conftest guard +
   rollback fixtures. ~2 hours.
5. **Stream auth fail-open**: `_check_secret` skips ALL auth when
   `STREAM_SHARED_SECRET` is empty/unset (main.py:341-353) — one bad env
   save = public unauthenticated agent with 117 tools including local_bash.
   Same fail-open class in the WA webhook signature check
   (gateway/api/webhook.py:65-66). Fix: fail-closed + `hmac.compare_digest`. ~1 hour.

### P1 — latent, will bite

6. **local_bash auto-executes in semi_auto**: TOOL_TIERS keys on legacy
   name "Bash"; `local_bash` falls to WRITE default → ALLOW. The registry's
   DESTRUCTIVE tier is dead metadata on this path. Fix the class: gate
   reads `td.tier` from the registry, drop the name-keyed map.
7. **always_ask doesn't ask**: ASK→ALLOW for read/write (112/117 tools),
   ASK→DENY destructive with no approval UX. The conservative mode is
   semi_auto with extra denials. (Phase-6 approval UX never built.)
8. **Per-turn hard timeout never enforced**: `_TURN_HARD_TIMEOUT_SEC=240`
   defined, documented, AST-tested — nothing wraps the drive task. Worst
   case ~58 min of unbounded server-side token burn.
9. **Bridge timeout margins are zero** on all 7 local tools (e.g. 140/140.0)
   — registry timeout fires first, bridge_calls rows stick at
   running/pending forever. The doc says 150/130 was the fix; code regressed.
10. **Image uploads poison session memory**: base64 blocks saved into
    `turns.messages`, token estimator counts them at ~200× real cost →
    every subsequent turn triggers pass-2 compaction and the session
    "forgets everything" after one screenshot. Fix: strip/reference image
    blocks at save time + estimator special-case.
11. **Scheduler is half-fiction in the cloud**: 5 Apple-Notes/osascript
    jobs registered on Linux (can never succeed); fleet_health probes a
    decommissioned laptop topology (`services/manager.py` hardcoded paths);
    calendar jobs read laptop-only credential paths; briefings fire but
    half their signal sources are dead → the 22:00 briefing exists but
    can't meet its bar.
12. **Time-scoped autonomy modes never revert**: the in-memory revert is
    undone next turn by refresh_from_db reading the persisted temp mode.
13. **System prompt documents 5 tools that don't exist** (get_task,
    update_task, notes_recent, notes_count, research_search) and omits
    the real names — model steered to phantom tools every turn.
14. **Post-turn memory extraction dead** since May 4 (extract_and_store,
    zero callers) — the "store memories proactively" behavior the prompt
    demands has no automatic backstop.
15. **CI pytest gate decorative**: `|| echo` soft-fail still in check.yml;
    6 standing test_e2e failures (SDK-era rot: deleted research_intel
    import, undefined `options`) shipped green for 5+ weeks.
16. **Share tokens plaintext + no expiry** (shares/store.py:79-99) on an
    internet-facing endpoint; bridge tokens got hashing, shares didn't.
17. **No retention anywhere**: turns/turn_events/bridge_calls/usage_events/
    audit_events grow forever; `sweep_expired()` for previews is dead code
    (multi-MB base64 uploads accumulate); no scheduler job covers any of it.
18. **Web DB env split-brain**: `lib/db.ts` requires `ASTRA_DB_URL` while
    9 routes inline-pool on `DATABASE_URL` — the exact documented Railway
    failure class, waiting for the next env migration.
19. **WhatsApp outbound dispatch Celery-only** (no worker deployed) and
    **no WA channel to Astra itself** — router holds unmatched messages
    as pending; the promised "notify Astra" was never written.

### P2 — debt (selected)

- 19 tools mis-namespaced under `creators`; namespace-scoped subsets
  return nothing for code_editor/kit_editor/self_improve.
- No prompt caching on the hot loop (~20-25k tokens of system+schemas
  re-sent uncached every iteration; 2-5× avoidable input spend).
- render_* tools return bare R2 URLs (no preview artifact);
  generate_hero_image stores PNG but never emits an image artifact.
- Dead code: autonomy/hooks.py, event_emitter.heartbeat, ThoughtStream.tsx,
  renderLite.tsx, orphaned /api/notes routes, finance bookkeeper_url.
- tests/test_tools/ is empty — the @tool handler layer (where the
  is_error bugs live) has zero direct coverage; astra-web has zero tests.
- railway.toml points at nonexistent Dockerfile.stream (works only because
  Railway ignores the block); 2 TODO_CALENDAR items past due (2026-06-06 ×2),
  third due 2026-06-13; migration_debt_state.md item #15 needs marking fixed.
- astra-control (launchd plist, bridge wrapper, CREDENTIALS.md) is not in
  git — the bridge's deployment artifact is unversioned.

---

## 5. Verdict against the compass

**The paradox the scan resolves: every dashboard is green and the product
still feels failed.** The chat runtime — the part rebuilt after the May
fires — is genuinely excellent: tested, observable, smoke-gated. But the
compass bar was never "chat works." It was *"the briefing should answer
'what should I do today' — chat is the last resort."* Measured there:

- The **reactive layer** (ask → answer, with tools) is ~85-90% of bar.
- The **proactive layer** (briefings with real signal) is ~25%: jobs fire
  on schedule into dead integrations — empty email store, fictional fleet
  health, laptop-only paths.
- The **orchestration layer** (one agent commanding seven) is ~15%: the
  protocol is built and tested; the registry that makes it real has been
  empty since the SDK was removed.
- The **channel layer** (email/WA/share-sheet as ambient input) is ~10%.

Weighted by what the compass says matters, **Astra is at roughly 35-40%
of its original remit** — a very good chat agent wearing the skeleton of
an agent OS. The 2026-06-09 "failed agent" judgment is correct about the
OS and unfair to the runtime. The encouraging part: the three biggest
gaps (fleet registry, email client URL, scheduler topology) are *wiring*
— severed in migrations, not unbuilt. The pattern is the project's known
disease: code that was correct on one laptop, silently severed when split
across containers, with no integration test at the seam.

---

## 6. Recommended fix order

| # | Fix | Effort | Buys |
|---|---|---|---|
| 1 | Auth on email/WA upstreams + fail-closed stream secret + hmac compare | half-day | Closes both public holes |
| 2 | Test-DB guard (conftest host check + rollback) | 2h | Never mutate prod from pytest again |
| 3 | Call `register_all_external_agents()` at startup + smoke journey for A2A discovery | 1-2h | Resurrects fleet orchestration |
| 4 | Email client base URL from env → cloud agent; replace Celery-beat dependency | 1 day | Resurrects the email channel + briefing signal |
| 5 | Autonomy tier fix (gate on td.tier; kill name-keyed map) + local_bash back under DESTRUCTIVE | 2h | Closes silent permission bypass |
| 6 | Image-block stripping at save + estimator fix | half-day | Sessions survive screenshots |
| 7 | Enforce per-turn timeout + restore bridge margins | 2h | Bounded worst-case burn |
| 8 | Remove CI soft-fail + fix/delete 6 rotted e2e tests | 2h | CI gate becomes real |
| 9 | Scheduler topology pass: env-gate macOS jobs, fleet probe → /api/state, calendar creds → env | 1 day | Briefings carry real signal |
| 10 | Retention: previews sweep job + turn_events/bridge_calls pruning + prompt caching | 1 day | Bounded growth, 2-5× input-cost cut |

Items 1-5 ≈ two focused days and close every P0. Items 1-10 ≈ one week
to bring the proactive layer from 25% to ~70% and security to ~85%.

---

*Scan: 9 agents, 327 tool calls, ~965k tokens, 16.6 min. Workflow run
`wf_d4344c9b-fca`. Prior audits (migration_debt_tools.md,
migration_debt_state.md) re-verified: 100% of their open claims still
accurate except state-item #15 (fixed in e682304).*
