"""Refund recovery jobs.

Revision ID: 20261019_0003
Revises: 20261013_0002
Create Date: 2026-10-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261019_0003"
down_revision: str | None = "20261013_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.recovery_job (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES app.tenant (id),
            ledger_id uuid NOT NULL REFERENCES app.action_ledger (id),
            command_generation integer NOT NULL,
            proposal_id uuid NOT NULL REFERENCES app.proposal (id),
            proposal_revision integer NOT NULL,
            run_id uuid NOT NULL REFERENCES app.run (id),
            approval_id uuid NOT NULL REFERENCES app.approval (id),
            authorizer_user_id uuid NOT NULL REFERENCES app.user_account (id),
            payload_hash text NOT NULL,
            input_revision integer NOT NULL,
            payment_resource_version integer NOT NULL,
            policy_version text NOT NULL,
            command jsonb NOT NULL,
            config_snapshot jsonb NOT NULL,
            state text NOT NULL,
            owner_token uuid,
            owner_generation integer NOT NULL,
            leased_until timestamptz,
            attempt_count integer NOT NULL,
            max_attempts integer NOT NULL,
            next_attempt_at timestamptz NOT NULL,
            admitted_at timestamptz,
            last_error_code text,
            block_reason text,
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            CONSTRAINT recovery_job_state
                CHECK (state IN ('queued', 'leased', 'blocked', 'settled', 'cancelled'))
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX recovery_job_one_active
            ON app.recovery_job (tenant_id, ledger_id)
            WHERE state IN ('queued', 'leased', 'blocked')
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON app.recovery_job TO resolveai_api"
    )
    op.execute("GRANT SELECT ON app.recovery_job TO resolveai_oracle")


def downgrade() -> None:
    op.execute("DROP TABLE app.recovery_job")
