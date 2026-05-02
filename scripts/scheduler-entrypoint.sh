#!/usr/bin/env bash
# Scheduler container entrypoint.
#
# Runs in this order:
#   1. Wait for Postgres (Railway sometimes promotes Postgres a few
#      seconds after the dependent service starts on first deploy).
#   2. Ensure pgvector extension exists. Railway's managed Postgres
#      does not auto-enable extensions, so we do it ourselves on
#      every boot — idempotent.
#   3. Apply alembic migrations.
#   4. Boot the APScheduler process.
#
# All four steps must succeed for the container to stay up; if any
# fails the container exits non-zero and Railway flags the deploy.

set -euo pipefail

echo "[entrypoint] astra-scheduler starting"

# 1. Wait for Postgres
if [ -z "${DATABASE_URL:-}" ]; then
  echo "[entrypoint] FATAL: DATABASE_URL not set"
  exit 1
fi

# Strip the +asyncpg driver hint for psql (psql doesn't grok SQLAlchemy URLs)
PSQL_URL="$(echo "$DATABASE_URL" | sed 's|postgresql+asyncpg://|postgresql://|')"

echo "[entrypoint] Waiting for Postgres to accept connections…"
for i in $(seq 1 30); do
  if psql "$PSQL_URL" -c 'SELECT 1' >/dev/null 2>&1; then
    echo "[entrypoint] Postgres reachable"
    break
  fi
  if [ "$i" = "30" ]; then
    echo "[entrypoint] FATAL: Postgres unreachable after 30 attempts"
    exit 1
  fi
  sleep 2
done

# 2. Ensure pgvector
echo "[entrypoint] Ensuring pgvector extension…"
psql "$PSQL_URL" -c 'CREATE EXTENSION IF NOT EXISTS vector;' >/dev/null

# 3. Alembic migrations
echo "[entrypoint] Applying alembic migrations…"
cd /app
alembic upgrade head

# 4. Boot the scheduler. Note: we replace the shell with the python
# process (exec) so signals propagate cleanly — Railway sends SIGTERM
# on redeploy and we want APScheduler's shutdown handler to run.
echo "[entrypoint] Booting scheduler…"
exec python -m astra.scheduler.app
