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
- Bridge calls stuck at status='running' (registry's outer
  asyncio.wait_for cancelled the inner wait_for_result before its
  internal deadline check could run)
- Tools timing out before their underlying API calls (we'd see
  partial state and no error message)

Margin of 60s gives the inner layer time to write status, emit a
terminal event, run cleanup, and return cleanly.

## What polling architecture changed

Phase 2b moved the chat path from SSE to polling. The browser no
longer holds a connection open for the lifetime of a turn — each
poll request is sub-second, the agent run is a server-side
asyncio.Task, and nothing in the request path mediates duration
anymore. That eliminated three entire timeout layers:

- **Browser SSE watchdog (was 330s)** — repurposed as a polling
  liveness check. Same value, different meaning: now it triggers
  if the turn has produced no events for that long, signalling
  the server-side task likely wedged.
- **Vercel `/api/chat` maxDuration (was 300s)** — now 10s. The
  route just enqueues a turn and returns `{turn_id}`; if it ever
  takes longer than 10s, `/turns/start` upstream is hung.
- **Stream service per-frame heartbeat (was 15s)** — gone. Lived
  inside the `/stream` SSE endpoint, which itself was deleted on
  2026-05-20 along with the SSE escape hatch.

The remaining timeouts form a much shorter chain.

## Layers (outermost to innermost)

```
[ chatPoller maxPollDurationMs ]   600s   browser-side safety net
  └──[ runner per-turn         ]   240s   actual ceiling on agent work
       └──[ registry per-tool  ]   5-150s tool budget
            └──[ tool internal ]   varies by tool
                 └──[ daemon per-action ] (bridge tools)
```

The poll-loop's max duration sits *outside* the runner — if the
runner's hard cap (240s) ever fired, polling at 600s would still
have plenty of slack to observe the terminal event. Browser-side
maxPollDurationMs is the "stop polling" safety net for orphaned
turns (e.g. DB unreachable so events never land).

### Per-layer specifics

| Layer | Limit | Set in | Notes |
|-------|-------|--------|-------|
| chatPoller `maxPollDurationMs` | **600s** (10 min) | `astra-web/lib/chatPoller.ts` `DEFAULT_MAX_DURATION_MS` | Browser stops polling after this. 2.5× the runner cap so it never triggers during legitimate work |
| ChatProvider stall watchdog | **330s** | `astra-web/components/ChatProvider.tsx` | Fires if no events arrive for 5.5min while `isStreaming` is true. Backstop against a wedged server-side task |
| Vercel `/api/chat` maxDuration | **10s** | `astra-web/app/api/chat/route.ts` | Just enqueue + return turn_id. >10s = `/turns/start` upstream is hung |
| Vercel `/api/turns/[id]/cancel` maxDuration | **5s** | `astra-web/app/api/turns/[id]/cancel/route.ts` | One-off "set a flag" call; abort fast if upstream hangs |
| `/turns/start` upstream timeout | **8s** | `astra-web/app/api/chat/route.ts` AbortSignal | Tighter than Vercel maxDuration so we surface upstream issues fast |
| Runner per-turn hard | **240s** | `astra/runtime/agent_loop.py` `_TURN_HARD_TIMEOUT_SEC` | The actual ceiling on a single turn's work |
| Registry per-tool — fast | **15s** | `astra/runtime/sdk_adapter.py` `_guess_timeout` | DB-bound or pure-CPU tools (recall_*, list_*, simple lookups) |
| Registry per-tool — moderate | **30s** | same | Network-bound (browser_fetch, email_search) |
| Registry per-tool — slow | **120s** | same | Generation/render (draft_*, render_*, analyze_reference_site) |
| Registry per-tool — bridge | **inner + 20s** (local_bash 160s, screenshot 75s, grep 50s, read/glob 40s, write/edit 35s) | `astra/runtime/tools/local.py` | Outer registry wait_for starts before _dispatch's DB round-trips, so it MUST exceed the inner wait_for_result deadline or it fires first and leaves bridge_calls rows stuck at 'running'. Regressed to zero margin once; restored 2026-06-11. |
| Bridge wait_for_result (inner) | **per-tool** (local_bash 140s, screenshot 55s, grep 30s, read/glob 20s, write/edit 15s) | `astra/runtime/tools/local.py` `_dispatch(...)` | Must stay 20s UNDER the registry timeout above |
| Bridge daemon glob | **10s** | `astra/bridge_daemon.py` | Wall-clock cap on os.walk |
| Bridge daemon grep | **15s** | `astra/bridge_daemon.py` | Wall-clock cap on os.walk |
| Bridge daemon bash | **30s default, 120s max** | `astra/bridge_daemon.py` | User-controllable per call |
| Health endpoint per-check | **2.5s** | `astra-web/app/api/health/deep/route.ts` | Tight so a stuck dependency can't hang the whole probe |
| Health endpoint maxDuration | **15s** | same | Generous outer cap on the parallel checks |

### Polling cadence (separate axis from duration limits)

These are how often we poll, not how long we wait:

| Layer | Limit | Set in | Notes |
|-------|-------|--------|-------|
| chatPoller base interval | **500ms** | `astra-web/lib/chatPoller.ts` `DEFAULT_POLL_MS` | Idle UI snappiness vs DB load tradeoff |
| chatPoller backoff growth | **1.5×** per empty poll | same `BACKOFF_GROWTH` | Adaptive scale-up during long thinking |
| chatPoller max backoff cap | **5s** | same `DEFAULT_MAX_BACKOFF_MS` | Throttle floor for idle agent stretches |
| /turns/[id] page tail-poll | **3s** | `astra-web/app/turns/[id]/page.tsx` | After-the-fact viewing, latency-tolerant |

Adaptive backoff: we hit /events at 500ms when the agent is
actively producing tokens, scaling 1.5× per consecutive empty
poll, capped at 5s. ~10× DB load reduction during long tools
that are quiet on the wire. Snaps back to 500ms on the next
batch of events.

### Margin verification table

```
poll cap (600)  - runner (240)        = 360s  ✓ huge
runner (240)    - registry slow (120) = 120s  ✓
registry bridge outer - inner          = 20s   enforced margin, all 7 bridge tools
inner bash (140) - daemon bash (120)  = 20s   margin over daemon's own cap
```

The two tight margins are bridge-internal — they only matter when
a bash command runs at its 120s ceiling. In practice the bash
default is 30s; users who pass timeout=120 are explicitly opting
in to the tight margin.

### Idle-state timeouts

Separate from per-call duration limits, these fire on inactivity:

| Layer | Limit | Notes |
|-------|-------|-------|
| Postgres `pool_recycle` | 300s | Connections older than this get rebuilt before next use |
| Postgres `pool_pre_ping` | n/a | `SELECT 1` before each checkout — kills dead connections |
| TCP keepalives idle | 7200s | OS-level; very long, rarely matters in practice |
| `_running_turns` sweeper | 300s | Stream service prunes finished asyncio.Tasks every 5min |

## Maintenance

- When introducing a new timeout, **add a row to the table above**
  AND verify the hierarchy holds.
- The e2e harness (`scripts/e2e_smoke.py`) probes whether long-
  running tools complete within their declared budgets. Failures
  here usually mean a timeout was tuned too low.
- The `tests/test_timeout_hierarchy.py` (added in commit q5j81k7…)
  asserts the relationships at import time — if anyone changes a
  number that violates the hierarchy, the test catches it before
  deploy.
