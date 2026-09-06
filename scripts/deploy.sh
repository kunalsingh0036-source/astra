#!/usr/bin/env bash
# Deploy the astra Python services to Railway. THE PUSH IS THE DEPLOY.
#
# Usage:
#   scripts/deploy.sh            push main, wait for every repo-backed
#                                service to build it, verify /health
#   scripts/deploy.sh --no-wait  push only, print what to watch
#
# Why this script no longer runs `railway up`
# -------------------------------------------
# The Railway services are connected to the GitHub repo as their source.
# Every earlier deploy went out with `railway up` from the local working
# tree, which uploads bytes directly and NEVER updates that source. The
# two drifted 47 commits apart, and on 2026-09-06 a single unrelated
# dashboard action (setting the stream healthcheck path) rebuilt stream
# from GitHub main and rolled production back past the whole capability
# broker: /broker/* went 404, the retired /bridge/* came back, and the
# self-approval tool A5 had deleted was live again for nine minutes.
#
# A deploy path that does not update the connected source is a rollback
# waiting for any dashboard action. So: this script pushes, and then
# treats Railway's own GitHub-triggered build as the deploy. There is
# one source of truth and it is origin/main.
#
# What it does, in order:
#   1. Refuses a dirty tree and any branch but main.
#   2. Pushes main to origin. That is what starts the builds.
#   3. Polls the Railway API until every service that rebuilt for this
#      commit reaches SUCCESS. stream and scheduler are REQUIRED to
#      appear and succeed; others (email, whatsapp) are reported.
#   4. Verifies stream /health reports the pushed sha, when the running
#      build is new enough to report one at all.
#
# Not covered here:
#   - astra-web: its own GitHub repo, deployed by Vercel on push.
#   - Migrations. The scheduler entrypoint runs `alembic upgrade head`
#     at boot, so a migration meant to be applied BY HAND must be
#     applied before this script runs, or the container applies it.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STREAM_URL="${STREAM_PUBLIC_URL:-https://stream.thearrogantclub.com}"
REQUIRED_SERVICES="stream scheduler"
WATCH_SERVICES="stream scheduler email whatsapp"
WAIT=1
for arg in "$@"; do
  case "$arg" in
    --no-wait) WAIT=0 ;;
    --wait) WAIT=1 ;;   # accepted for compatibility; waiting is the default
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "deploy.sh: unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$REPO"
command -v railway >/dev/null || { echo "deploy.sh: railway CLI not on PATH" >&2; exit 1; }
railway whoami >/dev/null 2>&1 || { echo "deploy.sh: not logged in (railway login)" >&2; exit 1; }

# 1. Clean tree, on main.
branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$branch" == "main" ]] || {
  echo "deploy.sh: on branch '$branch'; production tracks origin/main. Merge first." >&2; exit 1; }
dirty="$(git status --porcelain --untracked-files=all)"
if [[ -n "$dirty" ]]; then
  echo "deploy.sh: refusing to deploy a dirty tree (the push would not carry these):" >&2
  echo "$dirty" >&2
  exit 1
fi
sha="$(git rev-parse HEAD)"; short="${sha:0:8}"

# 2. The push IS the deploy.
git fetch -q origin main
behind="$(git rev-list --count HEAD..origin/main)"
[[ "$behind" == "0" ]] || {
  echo "deploy.sh: origin/main has $behind commit(s) you do not have. Pull and re-run." >&2; exit 1; }
ahead="$(git rev-list --count origin/main..HEAD)"
if [[ "$ahead" == "0" ]]; then
  echo "origin/main is already at $short; no push needed (re-verifying what is live)"
else
  echo "== git push origin main ($ahead commit(s), HEAD $short) =="
  git push origin main
fi

if [[ "$WAIT" == "0" ]]; then
  echo
  echo "pushed $short; Railway builds start from GitHub. Watch:"
  echo "  railway deployment list --service stream"
  echo "  $STREAM_URL/health   (build_sha should read $short)"
  exit 0
fi

# 3. Railway's build of THIS commit is the deploy. Poll for it.
echo
echo "== waiting for Railway to build $short from GitHub =="
deadline=$(( $(date +%s) + 900 ))
# No associative arrays: macOS ships bash 3.2 and `declare -A` is a
# syntax error there, which killed this script mid-deploy once. A
# newline-separated "svc=STATUS" list works on every bash.
finals=""
final_of() { printf '%s\n' "$finals" | sed -n "s/^$1=//p" | head -1; }

while :; do
  pending=0
  for svc in $WATCH_SERVICES; do
    [ -n "$(final_of "$svc")" ] && continue
    line="$(railway deployment list --service "$svc" --json 2>/dev/null | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("? "); raise SystemExit
x = d[0] if d else {}
m = x.get("meta") or {}
print(x.get("status", "?"), (m.get("commitHash") or "") if isinstance(m, dict) else "")
' 2>/dev/null || echo "? ")"
    status="${line%% *}"; commit="${line#* }"
    case "$commit" in
      "$sha"*) ;;
      *) pending=1; continue ;;   # not building our commit (yet, or not repo-backed)
    esac
    case "$status" in
      SUCCESS)  finals="$finals
$svc=SUCCESS"; echo "  $svc: SUCCESS at $short" ;;
      FAILED|CRASHED|REMOVED) finals="$finals
$svc=$status"; echo "  $svc: $status at $short" >&2 ;;
      *) pending=1 ;;
    esac
  done
  req_done=1
  for svc in $REQUIRED_SERVICES; do [ -n "$(final_of "$svc")" ] || req_done=0; done
  if [ "$req_done" = 1 ] && [ "$pending" = 0 ]; then break; fi
  if [ "$(date +%s)" -gt "$deadline" ]; then
    echo "deploy.sh: timed out after 15 min waiting for $short" >&2
    for svc in $WATCH_SERVICES; do
      echo "  $svc: $(final_of "$svc" | grep . || echo 'still building or never started')" >&2
    done
    exit 1
  fi
  sleep 15
done

bad=0
for svc in $REQUIRED_SERVICES; do
  [ "$(final_of "$svc")" = "SUCCESS" ] || { echo "deploy.sh: $svc is $(final_of "$svc" | grep . || echo missing)" >&2; bad=1; }
done
[[ "$bad" == "0" ]] || { echo "deploy.sh: NOT deployed. Check the build logs in the Railway dashboard." >&2; exit 1; }

# 4. Read the sha back from the running service. A build that succeeded
#    and a service that is serving it are different claims.
echo
echo "== verifying $STREAM_URL/health =="
for _ in $(seq 1 20); do
  body="$(curl -fsS --max-time 10 "$STREAM_URL/health" 2>/dev/null || true)"
  live="$(printf '%s' "$body" | python3 -c 'import json,sys; print((json.load(sys.stdin) or {}).get("build_sha","unknown"))' 2>/dev/null || echo unknown)"
  case "$live" in
    "$sha"*) echo "  live build_sha $short — deployed."; exit 0 ;;
    unknown) echo "  /health does not report a build_sha (build predates it): $body"
             echo "  deployed by Railway build status; sha not confirmed from the running process." ; exit 0 ;;
  esac
  sleep 10
done
echo "deploy.sh: stream built $short but /health still reports a different sha" >&2
echo "  last /health: $body" >&2
exit 1
