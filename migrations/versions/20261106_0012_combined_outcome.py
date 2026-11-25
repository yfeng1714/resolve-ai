"""Versioned outcomes for one refund and one period-end cancellation.

Revision ID: 20261106_0012
Revises: 20261106_0011
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "20261106_0012"
down_revision: str | None = "20261106_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.combined_half (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES app.tenant (id),
          ticket_id uuid NOT NULL,
          run_id uuid NOT NULL,
          input_revision integer NOT NULL,
          half text NOT NULL,
          outcome_version integer NOT NULL,
          outcome_code text NOT NULL,
          reason_code text NOT NULL,
          action_id text,
          proposal_id uuid,
          recovery_job_id uuid,
          prior_outcome_id uuid,
          created_at timestamptz NOT NULL,
          CONSTRAINT combined_half_tenant_id_key UNIQUE (tenant_id, id),
          CONSTRAINT combined_half_version_key
            UNIQUE (tenant_id, ticket_id, input_revision, half, outcome_version),
          CONSTRAINT combined_half_name CHECK (half IN ('refund', 'cancel_at_period_end')),
          CONSTRAINT combined_half_code CHECK (
            outcome_code IN (
              'confirmed', 'uncertain', 'in_flight', 'not_dispatched',
              'open', 'waiting', 'not_executed'
            )
          ),
          CONSTRAINT combined_half_version CHECK (outcome_version > 0),
          CONSTRAINT combined_half_ticket_same_tenant
            FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id),
          CONSTRAINT combined_half_run_same_tenant
            FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id),
          CONSTRAINT combined_half_prior_same_tenant
            FOREIGN KEY (tenant_id, prior_outcome_id)
            REFERENCES app.combined_half (tenant_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.combined_reply (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES app.tenant (id),
          ticket_id uuid NOT NULL,
          run_id uuid NOT NULL,
          reply_draft_id uuid NOT NULL,
          input_revision integer NOT NULL,
          refund_outcome_id uuid NOT NULL,
          cancellation_outcome_id uuid NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT combined_reply_tenant_id_key UNIQUE (tenant_id, id),
          CONSTRAINT combined_reply_draft_key UNIQUE (tenant_id, reply_draft_id),
          CONSTRAINT combined_reply_ticket_same_tenant
            FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id),
          CONSTRAINT combined_reply_run_same_tenant
            FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id),
          CONSTRAINT combined_reply_draft_same_tenant
            FOREIGN KEY (tenant_id, reply_draft_id)
            REFERENCES app.reply_draft (tenant_id, id),
          CONSTRAINT combined_reply_refund_same_tenant
            FOREIGN KEY (tenant_id, refund_outcome_id)
            REFERENCES app.combined_half (tenant_id, id),
          CONSTRAINT combined_reply_cancellation_same_tenant
            FOREIGN KEY (tenant_id, cancellation_outcome_id)
            REFERENCES app.combined_half (tenant_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.reject_combined_history_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF current_user IN ('resolveai_api', 'resolveai_worker') THEN
            RAISE EXCEPTION 'combined outcome history is immutable'
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
    for table in ("combined_half", "combined_reply"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON app.{table}
            FOR EACH ROW EXECUTE FUNCTION app.reject_combined_history_change()
            """
        )
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON app.{table}
            USING (tenant_id = app.current_tenant_id())
            WITH CHECK (tenant_id = app.current_tenant_id())
            """
        )
        op.execute(f"GRANT SELECT, INSERT ON TABLE app.{table} TO resolveai_api")
        op.execute(f"GRANT SELECT, INSERT ON TABLE app.{table} TO resolveai_worker")


def downgrade() -> None:
    bind = op.get_bind()
    blocking = bind.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM app.combined_half)
              + (SELECT count(*) FROM app.combined_reply)
            """
        )
    ).scalar_one()
    if int(blocking or 0) > 0:
        raise RuntimeError("Downgrade refused because combined outcomes exist")
    for table in ("combined_reply", "combined_half"):
        op.execute(f"DROP POLICY tenant_isolation ON app.{table}")
        op.execute(f"DROP TRIGGER {table}_immutable ON app.{table}")
    op.execute("DROP FUNCTION app.reject_combined_history_change()")
    op.execute("DROP TABLE app.combined_reply")
    op.execute("DROP TABLE app.combined_half")
