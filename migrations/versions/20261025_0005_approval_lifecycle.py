"""Refund approval expiry and append-only revocation.

Revision ID: 20261025_0005
Revises: 20261020_0004
Create Date: 2026-10-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261025_0005"
down_revision: str | None = "20261020_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.approval ADD COLUMN expires_at timestamptz")
    op.execute(
        """
        UPDATE app.approval
        SET expires_at = clock_timestamp() + interval '86400 seconds'
        WHERE expires_at IS NULL
        """
    )
    op.execute("ALTER TABLE app.approval ALTER COLUMN expires_at SET NOT NULL")
    op.execute("ALTER TABLE app.approval DROP CONSTRAINT approval_proposal_id_key")
    op.execute(
        """
        CREATE TABLE app.approval_revocation (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL,
            approval_id uuid NOT NULL,
            actor_user_id uuid NOT NULL,
            reason text NOT NULL,
            created_at timestamptz NOT NULL,
            CONSTRAINT approval_revocation_reason CHECK (char_length(reason) BETWEEN 1 AND 500),
            CONSTRAINT approval_revocation_approval_key UNIQUE (approval_id),
            CONSTRAINT approval_revocation_tenant_approval
                FOREIGN KEY (tenant_id, approval_id)
                REFERENCES app.approval (tenant_id, id),
            CONSTRAINT approval_revocation_tenant_actor
                FOREIGN KEY (tenant_id, actor_user_id)
                REFERENCES app.user_account (tenant_id, id)
        )
        """
    )
    op.execute("REVOKE UPDATE, DELETE ON TABLE app.approval FROM resolveai_api")
    op.execute("GRANT UPDATE (id) ON TABLE app.approval TO resolveai_api")
    op.execute(
        "GRANT SELECT, INSERT ON app.approval_revocation TO resolveai_api"
    )
    op.execute("GRANT SELECT ON app.approval_revocation TO resolveai_oracle")


def downgrade() -> None:
    op.execute("DROP TABLE app.approval_revocation")
    op.execute(
        "ALTER TABLE app.approval "
        "ADD CONSTRAINT approval_proposal_id_key UNIQUE (proposal_id)"
    )
    op.execute("ALTER TABLE app.approval DROP COLUMN expires_at")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.approval TO resolveai_api"
    )
