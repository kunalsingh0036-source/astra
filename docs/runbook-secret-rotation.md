# Secret rotation runbook

Astra has 6 secret families. Each has a different rotation cadence,
risk profile, and recovery path. Read this before rotating any of
them; some rotations invalidate downstream client state and need
careful sequencing.

| Secret | Rotation cadence | Risk if leaked | Notes |
|---|---|---|---|
| Anthropic API key | Quarterly OR on suspected leak | Cost + data exposure (prompts visible to whoever has the key) | Single key shared across all Astra services; rotation is one env-var update on each service |
| Google OAuth (Gmail + Calendar) | Yearly OR on suspected leak | Email + calendar read/write access | Requires user re-consent flow if rotated |
| Share tokens (per-device) | On lost/sold phone | Anyone with the token can POST to /api/share | Already revocable per-device via `revoke_token` |
| VAPID keys (Web Push) | **Never** unless leaked | Push subscriptions invalidate on rotate; users have to re-subscribe | Stable for the lifetime of Astra |
| WhatsApp Access Token (Meta) | Per Meta policy (60 days for system-user tokens) | Send messages on Kunal's behalf | Long-lived system user token preferred |
| Railway / R2 / BetterStack tokens | Yearly OR on suspected leak | Infra access | Each issued via its dashboard; revoke + re-issue |

---

## Anthropic API key

**Rotate when:** quarterly checkpoint, or any time a key is committed
to git / shared in screenshots / suspected to be leaked.

**Sequence:**

1. `console.anthropic.com` → Settings → API Keys → **Create Key**.
   Name it with the rotation date: `astra-2026-Q3`. Copy the secret
   (only shown once).
2. Update Railway env vars on every service that uses Anthropic.
   The list as of Session 4: `scheduler`, `stream`, `bridge`,
   `email`, `whatsapp`, `finance`, `backup` (no), `web` (no).
   ```
   for s in scheduler stream bridge email whatsapp finance; do
     railway variables --service $s --set "ANTHROPIC_API_KEY=<new-key>"
   done
   ```
   Setting a variable triggers a redeploy of that service. Wait
   ~3 min for the rolling restart of all 6 services.
3. Hit a known endpoint to verify (e.g. POST /stream with a "ping"
   prompt — confirms the new key auths against Anthropic).
4. Old key stays in Anthropic console until step 5 confirms.
5. Once all services are running on the new key, **revoke** the old
   key in Anthropic console.
6. Update `.env` locally too if the laptop ever runs against
   Anthropic for dev work. The local `.env` is git-ignored;
   rotating just means editing the file.

**If the rotation is mid-incident** (key actively leaked): revoke
the old key FIRST in Anthropic console, accept the brief downtime
on Astra (~5 min while new key propagates), then create + deploy
the new one. Order matters — revoke first, deploy second.

---

## Google OAuth (Gmail + Calendar)

The OAuth client ID + secret are in `email-agent/credentials/`
(local) and as env vars `GMAIL_CREDENTIALS_JSON` + `GMAIL_TOKEN_JSON`
on the Railway `email` service.

**Rotate when:** yearly, or when Kunal changes his Google account /
revokes Astra's access.

**Sequence:**

1. `console.cloud.google.com` → APIs & Services → Credentials →
   Create new OAuth 2.0 Client ID (type: Desktop or Web — same as
   the existing one). Note the new client ID + secret.
2. Run the OAuth consent flow locally to mint a fresh refresh token:
   ```
   cd ~/Claude\ Code/email-agent
   python -m email_agent.tools.gmail_oauth_init  # writes credentials/gmail_token.json
   ```
3. Update Railway env vars on the `email` service:
   ```
   railway variables --service email --set \
     "GMAIL_CREDENTIALS_JSON=$(cat email-agent/credentials/gmail_credentials.json)"
   railway variables --service email --set \
     "GMAIL_TOKEN_JSON=$(cat email-agent/credentials/gmail_token.json)"
   ```
4. Same for Calendar credentials if rotating those (separate
   OAuth flow).
5. After verifying email + calendar work on Railway, **delete the
   old OAuth client** in Google Cloud Console.

---

## Share tokens (iOS Share Sheet pairing)

Each paired phone has its own token in the `share_tokens` table.
Tokens are 32-byte URL-safe random strings.

**Rotate when:** phone lost/sold, suspected app compromise.

**Sequence:**

1. Open `astra.thearrogantclub.com/settings/share` (signed-in
   browser).
2. Find the device row → click **Revoke**. The DB row is set to
   `status='revoked'`. Future POSTs to `/api/share` with that token
   return 401.
3. Generate a new token from the same UI. Copy it.
4. Open the iOS app (AstraShare) → Settings → paste new token →
   "Pair this device".
5. Verify by sharing something via the iOS Share Sheet.

No service redeploy needed — tokens are checked at the DB layer,
not via env vars.

---

## VAPID keys (Web Push)

Used by both stream/web/scheduler to encrypt push payloads to the
browser's PushManager.

**Rotate when:** **never**, unless the private key is leaked. Rotation
invalidates every existing browser subscription — Kunal would need
to re-subscribe on every device he uses (laptop, iPhone PWA).

If you must rotate (true compromise):

1. Generate new VAPID pair:
   ```
   npx web-push generate-vapid-keys
   ```
2. Update env vars:
   - `VAPID_PUBLIC_KEY` (for the encoded public key, used by both
     stream/web/scheduler)
   - `VAPID_PRIVATE_KEY` (private key for stream/scheduler)
   - `NEXT_PUBLIC_VAPID_PUBLIC_KEY` (for astra-web — same value as
     VAPID_PUBLIC_KEY)
3. **Tell Kunal**: he has to manually re-subscribe in each browser
   (Settings → Notifications → toggle off → toggle on).
4. Truncate `push_subscriptions` table — the old subscriptions can
   no longer be decrypted by the new key, so they're dead anyway:
   ```
   psql ... -c "DELETE FROM push_subscriptions;"
   ```

---

## WhatsApp Access Token (Meta)

System-user token in the `whatsapp` Railway service env
(`WHATSAPP_ACCESS_TOKEN`). Meta defaults system-user tokens to
60-day expiry; long-lived ones are available via Meta's app
dashboard.

**Rotate when:** Meta dashboard says token is expiring, or on
suspected leak.

**Sequence:**

1. Meta Business Suite → System Users → astra → Access Tokens →
   **Generate new token**. Scopes:
   `whatsapp_business_messaging`, `whatsapp_business_management`.
2. Update Railway:
   ```
   railway variables --service whatsapp --set "WHATSAPP_ACCESS_TOKEN=<new>"
   ```
3. Wait for redeploy (~2 min). Test by sending a template message:
   ```
   curl -X POST https://whatsapp.thearrogantclub.com/api/v1/send \
     -H "Content-Type: application/json" \
     -d '{"to":"+919993094281","template":"hello_world"}'
   ```
4. The old token becomes invalid the moment Meta issues the new one
   — they don't run two in parallel. So the brief downtime is
   between step 1 and the redeploy completing.

---

## Railway / R2 / BetterStack tokens

These are infra-access tokens, not Astra-runtime secrets.

**Railway CLI token:** revoke from `railway.com/account/tokens` →
re-login from laptop with `railway login`. No service-side impact.

**R2 access keys** (for backup service):
1. Cloudflare → R2 → Manage R2 API Tokens → revoke old, create new.
2. Update Railway env vars on the `backup` service:
   ```
   railway variables --service backup --set "R2_ACCESS_KEY_ID=<new>"
   railway variables --service backup --set "R2_SECRET_ACCESS_KEY=<new>"
   ```
3. Manually trigger a backup run to verify — `railway up --service backup`.

**BetterStack API token** (for monitor management):
1. BetterStack → Settings → API tokens → revoke + create new.
2. No service uses this token at runtime; it's for ops scripts only.
   Update wherever I keep it (currently nowhere — I'd hand-paste).

---

## What's NOT in this list (intentional)

- **Postgres password.** Railway-managed; not user-rotatable. Railway
  rotates internally on its schedule. `${{Postgres.DATABASE_URL}}`
  always resolves to the current value.
- **Redis password.** Same as above — Railway-managed.
- **NextAuth `AUTH_SECRET`.** Used to sign session JWTs. Rotation
  invalidates every active web session (Kunal has to re-login from
  every device). Generally rotate only on suspected compromise.
  ```
  AUTH_SECRET=$(openssl rand -base64 32)
  railway variables --service web --set "AUTH_SECRET=$AUTH_SECRET"
  ```

---

## Audit checklist (quarterly)

Run through this list every 3 months:

- [ ] Anthropic key age — rotate if >90 days
- [ ] WhatsApp token age — rotate if expiring within 14 days
- [ ] Google OAuth refresh token — verify still works (Gmail watch
      renews automatically; if it fails, the token may have been
      revoked on Google's side)
- [ ] R2 + BetterStack tokens — review access in respective dashboards
- [ ] Active share tokens in `share_tokens` — revoke any for devices
      you no longer use
- [ ] `push_subscriptions` table — old/inactive subscriptions get
      auto-pruned by `prune_dead_subscriptions` job, but verify it's
      running

Last full rotation audit: **2026-05-02** (Session 4).
