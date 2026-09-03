"""capability broker: bodies, intents, intent_events

Revision ID: x2q58r3o9m1m
Revises: w1p47q2n8l0l
Create Date: 2026-09-03

Three tables for Workstream A3 — the transport that carries physical
intents from the cloud brain to the broker on Kunal's Mac. Read these
notes before editing; every rule below is here because of a specific
failure, most of them observed in this codebase.

THE REVISION ID SORTS AFTER w1p47q2n8l0l ON PURPOSE
---------------------------------------------------
services/stream/main.py:145-162 resolves the on-disk head by taking the
ALPHABETICALLY LAST filename in versions/, not by asking alembic. A
revision id sorting before the current head makes /health report
`degraded` forever — a monitor that cries wolf permanently, which is
the exact class already burned twice here.

WHY ONE SQL COMMAND PER op.execute()
------------------------------------
alembic runs ONLINE through asyncpg (astra/db/migrations/env.py:
async_engine_from_config + run_sync). asyncpg PREPARES every statement,
and a prepared statement cannot carry multiple commands — it raises
"cannot insert multiple commands into a prepared statement".

That is not theoretical. On 2026-08-30 migration w1p47q2n8l0l batched
`DROP TRIGGER; CREATE TRIGGER` into one op.execute(). It failed, and
because scripts/scheduler-entrypoint.sh runs `set -euo pipefail` (:16),
then `alembic upgrade head` (:49), then `exec python -m
astra.scheduler.app` (:55), the failure aborted the script BEFORE the
exec. The scheduler was down ~25 minutes and Railway reported the
deployment SUCCESS the whole time, because the BUILD passed.

Note the asymmetry that makes this easy to get wrong: psql (used at
entrypoint :44) speaks the SIMPLE protocol and happily accepts
multi-statement strings. A snippet that works pasted into psql can
still kill the scheduler from here.

WHY lock_timeout IS THE FIRST STATEMENT
---------------------------------------
The server runs statement_timeout=0, lock_timeout=0 and
idle_in_transaction_session_timeout=0. Without this line a migration
that blocks on a lock waits FOREVER: the container never crashes, the
restart policy never fires, and the scheduler simply never boots with
no error line anywhere. That is strictly worse than the 25-minute
outage, which at least failed fast and loudly.

NO CREATE INDEX CONCURRENTLY
----------------------------
env.py wraps each revision in ONE transaction (no
transaction_per_migration), and Postgres raises 25001 the moment CIC is
issued inside a transaction block. It would fail the entire run. These
tables are empty at creation, so a plain CREATE INDEX takes no
meaningful lock.

NEVER RUN `alembic revision --autogenerate` IN THIS REPO
-------------------------------------------------------
Verified read-only against production: compare_metadata() emits 34
remove_table operations, including approvals, bridge_calls,
bridge_tokens, tool_grants, browser_tasks, turns, turn_events and
source_items. astra/db/models.py declares 5 of the 40 live tables.
Autogenerate here would delete Astra's memory and its audit trail in a
single transaction. Write migrations by hand.

WHY THE SCHEMA LOOKS LIKE THIS
------------------------------
No `approved` column. No `approver`, no `token`, no `approval_pubkey`,
no `permission_epoch`. There is deliberately NOTHING HERE TO SET.

The brain's DB role is `postgres`, a superuser, and the only login role
in the cluster — and RAILWAY_API_TOKEN in the stream env can re-derive
that password anyway. So any such column is a column the brain can set
on itself. browser_tasks still carries a legacy `approved` boolean that
had to be defused by a JOIN in astra/browser/store.py::claim_next; a
column that exists is a column something will eventually trust.

The A3 design's own draft acceptance test wrote bodies.approval_pubkey
and bodies.permission_epoch. Those columns are absent on purpose: the
broker's trust store is its ACL'd keychain item and nothing else. If a
future change adds them, the cloud has become the broker's config
server and the boundary is gone.

`status` carries a CHECK — a departure from this repo, where the only
other CHECK in any migration is training_counters_singleton. It exists
so that 'authorized' is UNWRITABLE. A status value that names authority
is browser_tasks.approved with a larger vocabulary.

`args_raw` is size-CHECKed and NEVER TRUNCATED. This also departs from
the house idiom, which truncates in Python (approvals.py::_clamp_tool_
input, bridge/store.py result[:1_048_576]). Do NOT "restore
consistency" by adding a Python clamp: a truncated argument is an
argument whose rendered display no longer matches what executes, and
that is the exact deception the display digest exists to prevent.

intent_events.intent_id has NO FOREIGN KEY, deliberately. The audit
chain must survive its intent row being deleted; an FK would let a
DELETE on intents cascade the evidence away.

BYTES, NOT BOOLEANS, for anything attested. display_bytes, token_bytes
(267) and receipt_bytes (194) are stored RAW so poll_status can
RECOMPUTE verification from public keys. There is no `receipt_verified`
column and there must never be one — it would be a boolean the
superuser writes, labelled as though the executor had said it.

intent_events IS APPEND-ONLY AND IS NEVER PRUNED by retention_sweep
(astra/scheduler/jobs.py). A gap in `seq` is DEFINED to mean tampering,
so pruning it would make the chain verifier cry wolf nightly — and a
monitor that cries wolf gets ignored, which is how a real 33-day outage
went unnoticed here before.

DOWNGRADE IS DESTRUCTIVE AND IS NOT AUTOMATED. See downgrade().
"""
from typing import Sequence, Union

from alembic import op

revision: str = "x2q58r3o9m1m"
down_revision: Union[str, Sequence[str], None] = "w1p47q2n8l0l"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # FIRST. See the docstring: without this a blocked migration hangs
    # forever and the scheduler never boots, silently.
    op.execute("SET lock_timeout = '5s'")

    # A registered body. `token_hash` is SHA-256 of the bearer token;
    # the plaintext is never stored, and the broker's copy lives in its
    # ACL'd keychain item. No key material, no policy, no epoch.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS bodies (
            id                SERIAL       PRIMARY KEY,
            label             VARCHAR(128) NOT NULL DEFAULT '',
            token_hash        CHAR(64)     NOT NULL UNIQUE,
            created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
            last_poll_at      TIMESTAMPTZ,
            last_completed_at TIMESTAMPTZ,
            revoked_at        TIMESTAMPTZ
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS intents (
            id            BIGSERIAL   PRIMARY KEY,
            body_id       INTEGER     NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            verb          VARCHAR(64) NOT NULL,
            args_raw      JSONB       NOT NULL DEFAULT '{}',
            why           TEXT        NOT NULL DEFAULT '',
            status        VARCHAR(16) NOT NULL DEFAULT 'pending',
            deny_reason   TEXT,
            display_bytes BYTEA,
            token_bytes   BYTEA,
            receipt_bytes BYTEA,
            result_bytes  BYTEA,
            result_note   TEXT        NOT NULL DEFAULT '',
            session_claim VARCHAR(64) NOT NULL DEFAULT '',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            claimed_at    TIMESTAMPTZ,
            resolved_at   TIMESTAMPTZ,
            expires_at    TIMESTAMPTZ NOT NULL,
            CONSTRAINT intents_args_raw_size CHECK (pg_column_size(args_raw) < 1048576),
            CONSTRAINT intents_token_len     CHECK (token_bytes   IS NULL OR octet_length(token_bytes)   = 267),
            CONSTRAINT intents_receipt_len   CHECK (receipt_bytes IS NULL OR octet_length(receipt_bytes) = 194),
            CONSTRAINT intents_result_size   CHECK (result_bytes  IS NULL OR octet_length(result_bytes) <= 1048576),
            CONSTRAINT intents_display_size  CHECK (display_bytes IS NULL OR octet_length(display_bytes) <= 4096),
            CONSTRAINT intents_status_vocab  CHECK (status IN ('pending','claimed','awaiting_human','running','succeeded','failed','denied','expired'))
        )
        """
    )

    # The mirrored audit chain. The BROKER's local log is the source of
    # truth; this is a copy the cloud holds so the chain verifier can
    # compare. Deletion here is DETECTABLE (a gap in seq) but not
    # preventable — the brain is a superuser. That asymmetry is the
    # point: it converts silent erasure into loud erasure.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS intent_events (
            id             BIGSERIAL   PRIMARY KEY,
            body_id        INTEGER     NOT NULL REFERENCES bodies(id) ON DELETE CASCADE,
            seq            BIGINT      NOT NULL,
            ts             TIMESTAMPTZ NOT NULL,
            mirrored_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            intent_id      BIGINT,
            verb           VARCHAR(64) NOT NULL DEFAULT '',
            decision       VARCHAR(24) NOT NULL,
            reason         TEXT        NOT NULL DEFAULT '',
            args_sha256    CHAR(64),
            display_sha256 CHAR(64),
            receipt_sha256 CHAR(64),
            actor_claimed  VARCHAR(64) NOT NULL DEFAULT '',
            prev_hash      CHAR(64)    NOT NULL,
            record_hash    CHAR(64)    NOT NULL,
            CONSTRAINT intent_events_body_seq UNIQUE (body_id, seq)
        )
        """
    )

    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_intents_claim "
        "ON intents (body_id, status, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_intents_deadline "
        "ON intents (expires_at) "
        "WHERE status IN ('pending','claimed','awaiting_human','running')"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_intent_events_intent "
        "ON intent_events (intent_id)"
    )
    op.execute(
        "COMMENT ON COLUMN intents.status IS "
        "'pending|claimed|awaiting_human|running|succeeded|failed|denied|expired. "
        "No value here names authority — see the migration docstring.'"
    )


def downgrade() -> None:
    """DELIBERATELY NOT IMPLEMENTED.

    Dropping intent_events destroys the audit chain — the one artifact
    a compromised superuser cannot forge, and the thing the acceptance
    test proves is tamper-evident. A one-command way to erase that is
    not a downgrade, it is the attack.

    If you genuinely must reverse this: take a backup, verify the chain
    off-host FIRST, then drop the three tables by hand and
    UPDATE alembic_version SET version_num = 'w1p47q2n8l0l'.
    """
    raise RuntimeError(
        "x2q58r3o9m1m has no automated downgrade: dropping intent_events "
        "erases the audit chain. See the docstring."
    )
