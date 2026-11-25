"""Allow a new combined attempt beside a blocked pre-admission job.

Revision ID: 20261106_0013
Revises: 20261106_0012
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261106_0013"
down_revision: str | None = "20261106_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX app.recovery_job_one_active")
    op.execute(
        """
        CREATE UNIQUE INDEX recovery_job_one_active
            ON app.recovery_job (tenant_id, ledger_id)
            WHERE state IN ('queued', 'leased')
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.recovery_job_one_active")
    op.execute(
        """
        CREATE UNIQUE INDEX recovery_job_one_active
            ON app.recovery_job (tenant_id, ledger_id)
            WHERE state IN ('queued', 'leased', 'blocked')
        """
    )
