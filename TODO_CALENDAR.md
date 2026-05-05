# Calendared technical debt

Items that are deliberately left in place TEMPORARILY with a known
revisit date. Don't add things here without a specific date — drift
is the failure mode. If something doesn't have a date, it goes in
GitHub issues or docs/ instead.

When you (or Astra) hit a session, scan this file. Past-due items
get the next ~1 hour of work to retire them.

---

## 2026-05-12 — Remove legacy SSE escape hatch

**What:** Delete the `USE_LEGACY_SSE=1` fallback path that lets the
chat layer fall back to SSE proxying.

**Why it exists:** Phase 2b (commit `3007b18`) shipped the polling
architecture. Polling is now the default and verified end-to-end
against production via `scripts/e2e_smoke.py`. The SSE path stays
behind an env var for one week as rollback insurance — if a
regression surfaces, flipping `USE_LEGACY_SSE=1` on Vercel reverts
the chat path without a code deploy.

**By the date:** the polling path will have been live for 7+ days.
Any regression would have surfaced. Time to delete.

**Files to remove / clean up:**

In `astra-web/`:
- `app/api/chat/route.ts` — delete `USE_LEGACY_SSE` branch + the
  `proxyLegacyStream()` helper. Inline the polling enqueue into
  the main `POST` handler.
- `lib/chatStream.ts` — likely deletable entirely. The only
  remaining export is the `ChatEvent` type, which `chatPoller.ts`
  imports. Move that type into `chatPoller.ts` (or rename
  `chatStream.ts` to `chatTypes.ts`) and drop the SSE consumer
  code.

In `astra/services/stream/`:
- `main.py` — delete the `/stream` endpoint. Audit for SSE-
  specific helpers (per-frame heartbeat, frame builders) that no
  longer have callers.

In `astra-stream/stream/main.py`:
- Mirror the deletion (or drop the directory entirely if #3 is
  done).

In `astra-web/components/ChatProvider.tsx`:
- Remove the comment about "Phase 2b will remove this entirely
  (polling has no watchdog needed)" — the watchdog stays as a
  liveness check, the comment is stale.

**Don't forget:**
- Update `docs/timeout_hierarchy.md` — drop the "Legacy SSE path"
  section.
- Run `scripts/e2e_smoke.py` after deletion to confirm nothing
  regressed.
- Delete this entry from TODO_CALENDAR.md.

---

(Add new entries here as they're calendared.)
