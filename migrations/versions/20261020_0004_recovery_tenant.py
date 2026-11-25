"""Tenant-aware recovery job bindings.

Revision ID: 20261020_0004
Revises: 20261019_0003
Create Date: 2026-10-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261020_0004"
down_revision: str | None = "20261019_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.action_ledger "
        "ADD CONSTRAINT action_ledger_tenant_id_key UNIQUE (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE app.proposal "
        "ADD CONSTRAINT proposal_tenant_id_key UNIQUE (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE app.run ADD CONSTRAINT run_tenant_id_key UNIQUE (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE app.approval "
        "ADD CONSTRAINT approval_tenant_id_key UNIQUE (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE app.user_account "
        "ADD CONSTRAINT user_account_tenant_id_key UNIQUE (tenant_id, id)"
    )
    op.execute(
        """
        ALTER TABLE app.recovery_job
            ADD CONSTRAINT recovery_job_ledger_same_tenant
            FOREIGN KEY (tenant_id, ledger_id)
            REFERENCES app.action_ledger (tenant_id, id)
        """
    )
    op.execute(
        """
        ALTER TABLE app.recovery_job
            ADD CONSTRAINT recovery_job_proposal_same_tenant
            FOREIGN KEY (tenant_id, proposal_id)
            REFERENCES app.proposal (tenant_id, id)
        """
    )
    op.execute(
        """
        ALTER TABLE app.recovery_job
            ADD CONSTRAINT recovery_job_run_same_tenant
            FOREIGN KEY (tenant_id, run_id)
            REFERENCES app.run (tenant_id, id)
        """
    )
    op.execute(
        """
        ALTER TABLE app.recovery_job
            ADD CONSTRAINT recovery_job_approval_same_tenant
            FOREIGN KEY (tenant_id, approval_id)
            REFERENCES app.approval (tenant_id, id)
        """
    )
    op.execute(
        """
        ALTER TABLE app.recovery_job
            ADD CONSTRAINT recovery_job_authorizer_same_tenant
            FOREIGN KEY (tenant_id, authorizer_user_id)
            REFERENCES app.user_account (tenant_id, id)
        """
    )


def downgrade() -> None:
    for name in (
        "recovery_job_authorizer_same_tenant",
        "recovery_job_approval_same_tenant",
        "recovery_job_run_same_tenant",
        "recovery_job_proposal_same_tenant",
        "recovery_job_ledger_same_tenant",
    ):
        op.execute(f"ALTER TABLE app.recovery_job DROP CONSTRAINT {name}")
    for name, table in (
        ("user_account_tenant_id_key", "user_account"),
        ("approval_tenant_id_key", "approval"),
        ("run_tenant_id_key", "run"),
        ("proposal_tenant_id_key", "proposal"),
        ("action_ledger_tenant_id_key", "action_ledger"),
    ):
        op.execute(f"ALTER TABLE app.{table} DROP CONSTRAINT {name}")
