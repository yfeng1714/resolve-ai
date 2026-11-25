"""Reject runtime changes to approval history.

SELECT FOR UPDATE needs an UPDATE privilege, so the API role may lock
``approval.id``. A trigger rejects every actual update or delete from that role.

Revision ID: 20261026_0006
Revises: 20261025_0005
Create Date: 2026-10-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261026_0006"
down_revision: str | None = "20261025_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION app.reject_approval_history_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF current_user = 'resolveai_api' THEN
            RAISE EXCEPTION 'approval history is immutable'
              USING ERRCODE = '42501';
          END IF;
          IF TG_OP = 'DELETE' THEN
            RETURN OLD;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER approval_history_immutable
        BEFORE UPDATE OR DELETE ON app.approval
        FOR EACH ROW EXECUTE FUNCTION app.reject_approval_history_change()
        """
    )
    op.execute(
        """
        CREATE TRIGGER approval_revocation_history_immutable
        BEFORE UPDATE OR DELETE ON app.approval_revocation
        FOR EACH ROW EXECUTE FUNCTION app.reject_approval_history_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER approval_revocation_history_immutable ON app.approval_revocation")
    op.execute("DROP TRIGGER approval_history_immutable ON app.approval")
    op.execute("DROP FUNCTION app.reject_approval_history_change()")
