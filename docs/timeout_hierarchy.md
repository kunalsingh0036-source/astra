# Timeout hierarchy

Single document covering every duration limit in the Astra stack.
Maintained as the source of truth — when adding a new timeout,
update this document AND ensure the hierarchy below holds.

## The rule

**Outer ≥ Inner + 60 seconds margin.**

When an outer layer's timeout fires before an inner layer's, the
inner work is cancelled mid-flight without the chance to clean up,
mark its state, or report a useful error. That's the bug class
behind:
- "Stream ended without a terminal event" (Vercel timed out before
  the agent finished + the runner could yield `done`)
- Bridge calls stuck at status='running' (registry's outer
  asyncio.wait_for cancelled the inner wait_for_result before its
  internal deadline check could run)
- The browser's "thinking" indicator getting stuck (watchdog fired
  before the upstream stream actually closed, leaving state
  inconsistent)

Margin of 60s gives the inner layer time to write status, emit a
terminal event, run cleanup, and return cleanly.

## Layers (outermost to innermost)

```
[ browser watchdog              ]   330s
  └──[ Vercel maxDuration       ]   300s   margin: 30s ⚠ tight
       └──[ runner per-turn     ]   240s   margin: 60s ✓
            └──[ registry per-tool ] 5-150s margin: ≥60s for slow tools
                 └──[ tool internal  ] varies by tool
                      └──[ daemon per-action ] (bridge tools)
```

### Per-layer specifics

| Layer | Limit | Set in | Notes |
|-------|-------|--------|-------|
| Browser watchdog | **330s** | `astra-web/components/ChatProvider.tsx` | Phase 2b will remove this entirely (polling has no watchdog needed) |
| Vercel `/api/chat` maxDuration | **300s** | `astra-web/app/api/chat/route.ts` | Pro-plan ceiling. Hobby auto-caps at 60s |
| Cloudflare Tunnel idle | **~100s** | external | Heartbeats every 15s keep this from firing during normal turns |
| Stream service per-frame timeout | **15s** | `services/stream/main.py` | If runner doesn't yield in 15s, send heartbeat to keep proxy alive |
| Runner per-turn hard | **240s** | `astra/runtime/agent_loop.py` `_TURN_HARD_TIMEOUT_SEC` | Was 300s, lowered to give Vercel its margin |
| Registry per-tool — fast | **15s** | `astra/runtime/sdk_adapter.py` `_guess_timeout` | DB-bound or pure-CPU tools (recall_*, list_*, simple lookups) |
| Registry per-tool — moderate | **30s** | same | Network-bound (browser_fetch, email_search) |
| Registry per-tool — slow | **120s** | same | Generation/render (draft_*, render_*, analyze_reference_site) |
| Registry per-tool — bridge | **150s** | `astra/runtime/tools/local.py` | Bridge calls; needs margin over wait_for_result (130s) |
| Bridge wait_for_result | **130s** | `astra/runtime/bridge/store.py` | Margin under registry tool timeout (150s) |
| Bridge daemon glob | **10s** | `astra/bridge_daemon.py` | Wall-clock cap on os.walk |
| Bridge daemon grep | **15s** | `astra/bridge_daemon.py` | Wall-clock cap on os.walk |
| Bridge daemon bash | **30s default, 120s max** | `astra/bridge_daemon.py` | User-controllable per call |
| Health endpoint per-check | **2.5s** | `astra-web/app/api/health/deep/route.ts` | Tight so a stuck dependency can't hang the whole probe |
| Health endpoint maxDuration | **15s** | same | Generous outer cap on the parallel checks |

### Margin verification table

```
browser (330) - vercel (300)        = 30s   ⚠ tight; widen to 60s when convenient
vercel (300)  - runner (240)        = 60s   ✓
runner (240)  - registry slow (120) = 120s  ✓
registry slow (150) - wait_for (130) = 20s  ⚠ tight; only matters for bridge tools
wait_for (130) - daemon bash (120)  = 10s   ⚠ tight; daemon caps at 120 anyway
```

The two tight margins are Phase-2b candidates (kill them by removing
the layer entirely once polling lands).

### Idle-state timeouts

Separate from per-call duration limits, these fire on inactivity:

| Layer | Limit | Notes |
|-------|-------|-------|
| Postgres `pool_recycle` | 300s | Connections older than this get rebuilt before next use |
| Postgres `pool_pre_ping` | n/a | `SELECT 1` before each checkout — kills dead connections |
| TCP keepalives idle | 7200s | OS-level; very long, rarely matters in practice |

## Phase 2b plans

Once the browser switches from SSE to polling:
- Browser watchdog goes away (each poll request is sub-second; nothing
  to wait on)
- Vercel maxDuration on `/api/chat` drops to ~10s (just the time to
  enqueue the turn + return `turn_id`)
- Stream service per-frame heartbeat goes away (no SSE consumers)
- Runner per-turn hard timeout becomes the SOLE end-to-end cap

That collapses the stack to: runner (240s) → registry (varies) →
tool internals. Three layers, clean hierarchy, no proxy-mediated
duration limits anywhere.

## Maintenance

- When introducing a new timeout, **add a row to the table above**
  AND verify the hierarchy holds.
- The e2e harness (`scripts/e2e_smoke.py`) probes whether long-
  running tools complete within their declared budgets. Failures
  here usually mean a timeout was tuned too low.
- The `tests/test_timeout_hierarchy.py` (added in commit q5...)
  asserts the relationships at import time — if anyone changes a
  number that violates the hierarchy, the test catches it before
  deploy.
