"""tool_grants: refuse standing grants for no-standing tools, in the DATABASE.

CONTAINMENT §6 shipped the no-standing list in
astra/autonomy/approvals.py — but that is only ONE of the two writers.
astra-web has its own resolver
(astra-web/app/api/approvals/[id]/resolve/route.ts) whose own docstring
says "Both writers MUST stay in sync: the runtime consumes what either
wrote". They drifted: clicking "approve N always" on /approvals wrote a
tool_grants row for local_bash — arbitrary shell on the Mac — with no
no-standing check at all.

A rule enforced in each writer is a rule that gets forgotten by the
next writer. Both writers cross the DATABASE, so the rule belongs
here, at the chokepoint. This trigger is the backstop; the Python and
TypeScript checks stay as the friendly, early-erroring layer.

Honest scope: the agent's DB role is `postgres`, a superuser, so it
can DROP this trigger. This does not stop a determined compromised
agent — it stops the failure that actually happened, which is a second
implementation silently forgetting the rule. Real containment is
Workstream A's broker; see astra-body/docs/SECURITY-MODEL.md §6.

Keep the list in sync with astra.autonomy.approvals.NO_STANDING_TOOLS
(tests/test_autonomy/test_containment_now.py asserts they match).

Revision ID: w1p47q2n8l0l
Revises: v0o36p2n7k9k
Create Date: 2026-08-30
"""
from typing import Sequence, Union

from alembic import op

revision: str = "w1p47q2n8l0l"
down_revision: Union[str, Sequence[str], None] = "v0o36p2n7k9k"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NO_STANDING = (
    # arbitrary execution
    "local_bash", "Bash", "run_creator_tests",
    # sends / publishes
    "send_reply_draft", "approve_content_draft", "send_a2a_task",
    # self-modification + deploy
    "edit_astra_file", "write_astra_file", "local_edit", "local_write",
    "commit_code_changes", "commit_kit_changes", "revert_last_code_commit",
    "apply_self_improvement",
    # deletes / destructive ops
    "forget_memory", "restart_agent", "cancel_a2a_task",
    # permission + autonomy surface
    "set_mode", "resolve_approval", "revoke_tool_grant",
    # exposes the machine
    "start_tunnel", "stop_tunnel",
)


def upgrade() -> None:
    names = ", ".join(f"'{n}'" for n in NO_STANDING)
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION astra_refuse_no_standing_grant()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.tool_name IN ({names}) THEN
                RAISE EXCEPTION
                    'tool_grants: % is on the no-standing list and must be '
                    'approved one call at a time (CONTAINMENT 6)',
                    NEW.tool_name
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_tool_grants_no_standing ON tool_grants;
        CREATE TRIGGER trg_tool_grants_no_standing
            BEFORE INSERT OR UPDATE ON tool_grants
            FOR EACH ROW EXECUTE FUNCTION astra_refuse_no_standing_grant();
        """
    )
    # The live grant this rule exists for: local_bash, source='chat',
    # granted 2026-06-12, argument-blind and permanent. It has been
    # inert since CONTAINMENT §6 (check_grant ignores it), but leaving
    # the row means one rollback or one new consumer re-arms it.
    # Recorded in astra-body/docs/CONTAINMENT-NOW.md §6.
    op.execute(
        "DELETE FROM tool_grants WHERE tool_name IN "
        f"({names})"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_tool_grants_no_standing ON tool_grants"
    )
    op.execute("DROP FUNCTION IF EXISTS astra_refuse_no_standing_grant()")
