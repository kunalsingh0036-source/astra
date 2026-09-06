"""retire the Mac bridge: revoke every bridge token

Revision ID: y3r69s4p0n2n
Revises: x2q58r3o9m1m
Create Date: 2026-09-06

Phase A6 (astra-body/docs/WORKSTREAM-A-DESIGN.md "Phase A6 — Retire
the bridge"). The bridge daemon, its LaunchAgent, the wrapper script,
astra/runtime/tools/local.py, astra/runtime/bridge/ and the
/bridge/poll and /bridge/result routes are deleted in the same change
this revision ships with. This revision is the one control that
survives a Railway rollback: an older image still serves those routes,
and the bearer the daemon presented on every poll for months existed
in plaintext in ~/.astra-state/bridge_token (deleted), in the 20 MB
archived bridge.log, in shell history and in Railway request logs.
Deleting the routes closes the door in THIS build; revoking the row
closes it in every build.

ALL rows, not id=1. The design names the one live token (id=1,
kunal-mbp), but scripts/issue_bridge_token.py existed until this
change and could have minted a second bearer; a WHERE on an id is a
guard gated on a hand-maintained fact.

Applied BY HAND before scripts/deploy.sh (the orchestrator runs it and
re-checks `SELECT count(*) FROM bridge_tokens WHERE revoked_at IS
NULL` = 0). The scheduler's entrypoint then no-ops on `alembic upgrade
head`. Either order is safe: the statement is idempotent.

THE RULES THIS FILE FOLLOWS, and why (x2q58r3o9m1m's docstring holds
the incidents):
  - the revision id sorts alphabetically AFTER x2q58r3o9m1m, because
    services/stream/main.py's /health/deep takes the alphabetically
    last file in versions/ as the on-disk head;
  - `SET lock_timeout = '5s'` is the FIRST statement, or a blocked
    UPDATE waits forever with no error anywhere;
  - one SQL command per op.execute(): alembic runs through asyncpg,
    which prepares every statement and refuses multi-command strings;
  - never autogenerate in this repo;
  - no downgrade: un-revoking a bearer is not a schema rollback, it is
    re-arming a retired credential.

NOT DROPPED HERE: bridge_tokens and bridge_calls stay one release as a
record. A later revision drops bridge_calls then bridge_tokens, with a
downgrade that recreates them from p4i70j6h1e3e. Do not call the
tables "read-only" in between: the brain's DB role is a superuser, so
the honest statement is that no code path references them, which
tests/test_autonomy/test_containment_now.py asserts.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "y3r69s4p0n2n"
down_revision: Union[str, Sequence[str], None] = "x2q58r3o9m1m"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # FIRST. See the docstring: without this a blocked migration hangs
    # forever and the scheduler never boots, silently.
    op.execute("SET lock_timeout = '5s'")

    # Every bridge bearer, revoked. Idempotent: rows already revoked
    # keep their original revoked_at.
    op.execute(
        "UPDATE bridge_tokens SET revoked_at = now() WHERE revoked_at IS NULL"
    )


def downgrade() -> None:
    """DELIBERATELY NOT IMPLEMENTED.

    Clearing revoked_at would re-arm a bearer whose plaintext has been
    in a daemon's log, a shell history and Railway's request logs. If a
    bridge token is ever genuinely needed again, mint a new one; do not
    resurrect this one.
    """
    raise RuntimeError(
        "y3r69s4p0n2n has no automated downgrade: un-revoking the retired "
        "bridge tokens would re-arm a leaked credential. See the docstring."
    )
