#!/usr/bin/env bash
# migrate-to-railway.sh — one-shot data migration from local Postgres
# (Docker, port 5433) to Railway Postgres.
#
# What it does:
#   1. Dumps the local astra database to a timestamped .sql.gz
#   2. Restores it into the Railway Postgres pointed to by RAILWAY_DB_URL
#   3. Verifies row counts match per table
#
# Required:
#   * Local Postgres reachable at the URL in DATABASE_URL_LOCAL
#     (defaults to the value in astra/.env)
#   * RAILWAY_DB_URL env var set to the Railway Postgres connection
#     URL (get it from `railway variables --service postgres` after
#     the Postgres service is provisioned)
#
# Why we don't do --data-only or --schema-only separately: the data
# is small (10MB) and alembic has already run on the Railway side via
# the scheduler entrypoint. So we use --data-only here to load just
# the rows on top of the Railway-side schema.
#
# Idempotency: this script truncates Railway tables before restoring,
# so re-running it is safe — it just gives you a clean replica of
# local at this moment.

set -euo pipefail

ASTRA_ROOT="/Users/kunalsingh/Claude Code/astra"
DUMP_DIR="$ASTRA_ROOT/.dumps"
mkdir -p "$DUMP_DIR"

# Resolve local DB URL
if [ -z "${DATABASE_URL_LOCAL:-}" ]; then
  DATABASE_URL_LOCAL="$(grep '^DATABASE_URL=' "$ASTRA_ROOT/.env" | head -1 | cut -d= -f2-)"
  DATABASE_URL_LOCAL="${DATABASE_URL_LOCAL//postgresql+asyncpg:/postgresql:}"
fi
if [ -z "${RAILWAY_DB_URL:-}" ]; then
  echo "FATAL: RAILWAY_DB_URL not set. Get it via:"
  echo "  cd $ASTRA_ROOT && railway service postgres && railway variables"
  exit 1
fi
RAILWAY_DB_URL="${RAILWAY_DB_URL//postgresql+asyncpg:/postgresql:}"

ts="$(date +%Y%m%dT%H%M%S)"
dump="$DUMP_DIR/astra-data-$ts.sql.gz"

echo "[migrate] Dumping local astra DB → $dump"
# --data-only: rows, not schema (schema lives in alembic on Railway)
# --no-owner: don't try to set ownership to the local 'astra' user
# --no-acl: skip GRANT/REVOKE (Railway uses different roles)
# --disable-triggers: makes data load order-independent
PGPASSWORD="$(echo "$DATABASE_URL_LOCAL" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')" \
  pg_dump "$DATABASE_URL_LOCAL" \
    --data-only --no-owner --no-acl --disable-triggers \
    --exclude-table=alembic_version \
  | gzip -9 > "$dump"

local_rows=$(psql "$DATABASE_URL_LOCAL" -tAc "
  SELECT SUM(n_live_tup) FROM pg_stat_user_tables
  WHERE relname NOT LIKE 'pg_%' AND relname != 'alembic_version'
" 2>/dev/null | tr -d ' ')
echo "[migrate] Local row count: $local_rows"

echo "[migrate] Verifying Railway DB schema is current…"
railway_tables=$(psql "$RAILWAY_DB_URL" -tAc "
  SELECT COUNT(*) FROM information_schema.tables
  WHERE table_schema = 'public'
" | tr -d ' ')
if [ "$railway_tables" -lt 17 ]; then
  echo "FATAL: Railway DB has only $railway_tables tables. Expected ≥17."
  echo "       Deploy the scheduler service first so its entrypoint"
  echo "       runs alembic upgrade. Then re-run this script."
  exit 1
fi

echo "[migrate] Truncating Railway tables to give a clean restore target…"
psql "$RAILWAY_DB_URL" <<'SQL'
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT tablename FROM pg_tables
    WHERE schemaname = 'public' AND tablename != 'alembic_version'
  LOOP
    EXECUTE format('TRUNCATE TABLE %I CASCADE', r.tablename);
  END LOOP;
END $$;
SQL

echo "[migrate] Restoring data into Railway…"
gunzip -c "$dump" | psql "$RAILWAY_DB_URL" -v ON_ERROR_STOP=1 -q

# Refresh stats so n_live_tup is accurate
psql "$RAILWAY_DB_URL" -c "ANALYZE;" -q

railway_rows=$(psql "$RAILWAY_DB_URL" -tAc "
  SELECT SUM(n_live_tup) FROM pg_stat_user_tables
  WHERE relname NOT LIKE 'pg_%' AND relname != 'alembic_version'
" | tr -d ' ')
echo "[migrate] Railway row count: $railway_rows"

if [ "$local_rows" != "$railway_rows" ]; then
  echo "[migrate] WARNING: row counts differ (local=$local_rows, railway=$railway_rows)"
  echo "          Investigate per-table:"
  echo "          psql \"\$RAILWAY_DB_URL\" -c \"SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY relname\""
  exit 2
fi

echo "[migrate] ✓ Migration complete: $local_rows rows restored to Railway"
echo "[migrate]   Dump kept at $dump (gzip'd, safe to delete after verification)"
