"""Period-end cancellation shape, choice history, and simulator subscriptions.

Revision ID: 20261028_0008
Revises: 20261027_0007
Create Date: 2026-10-28
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "20261028_0008"
down_revision: str | None = "20261027_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_action_type")
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_currency")
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_amount")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN target_payment_id DROP NOT NULL")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN target_event_id DROP NOT NULL")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN amount_cents DROP NOT NULL")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN currency DROP NOT NULL")
    op.execute("ALTER TABLE app.proposal ADD COLUMN target_subscription_id uuid")
    op.execute("ALTER TABLE app.proposal ADD COLUMN effective_at timestamptz")
    op.execute("ALTER TABLE app.proposal ADD COLUMN subscription_resource_version integer")
    op.execute(
        """
        ALTER TABLE app.proposal
          ADD CONSTRAINT proposal_action_type
          CHECK (action_type IN ('refund', 'cancel_at_period_end'))
        """
    )
    op.execute(
        """
        ALTER TABLE app.proposal
          ADD CONSTRAINT proposal_shape CHECK (
            (
              action_type = 'refund'
              AND target_payment_id IS NOT NULL
              AND target_event_id IS NOT NULL
              AND amount_cents > 0
              AND currency = 'USD'
              AND target_subscription_id IS NULL
              AND effective_at IS NULL
              AND subscription_resource_version IS NULL
            ) OR (
              action_type = 'cancel_at_period_end'
              AND target_payment_id IS NULL
              AND target_event_id IS NULL
              AND amount_cents IS NULL
              AND currency IS NULL
              AND target_subscription_id IS NOT NULL
              AND effective_at IS NOT NULL
              AND subscription_resource_version IS NOT NULL
            )
          )
        """
    )
    op.execute("ALTER TABLE app.action_ledger DROP CONSTRAINT ledger_action_type")
    op.execute(
        """
        ALTER TABLE app.action_ledger
          ADD CONSTRAINT ledger_action_type
          CHECK (action_type IN ('refund', 'reply', 'cancel_at_period_end'))
        """
    )
    op.execute(
        "ALTER TABLE app.recovery_job ALTER COLUMN payment_resource_version DROP NOT NULL"
    )
    op.execute("ALTER TABLE app.recovery_job ADD COLUMN subscription_resource_version integer")
    op.execute(
        """
        ALTER TABLE app.recovery_job
          ADD CONSTRAINT recovery_job_resource_version CHECK (
            (
              payment_resource_version IS NOT NULL
              AND subscription_resource_version IS NULL
            ) OR (
              payment_resource_version IS NULL
              AND subscription_resource_version IS NOT NULL
            )
          )
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.recovery_command_matches_ledger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          ledger_type text;
        BEGIN
          SELECT action_type INTO ledger_type
          FROM app.action_ledger
          WHERE id = NEW.ledger_id AND tenant_id = NEW.tenant_id;
          IF ledger_type = 'refund' THEN
            IF NEW.payment_resource_version IS NULL
               OR NEW.subscription_resource_version IS NOT NULL
               OR NEW.command->>'action_id' IS NULL
               OR NEW.command ? 'subscription_id' THEN
              RAISE EXCEPTION 'refund command does not match its ledger'
                USING ERRCODE = '23514';
            END IF;
          ELSIF ledger_type = 'cancel_at_period_end' THEN
            IF NEW.subscription_resource_version IS NULL
               OR NEW.payment_resource_version IS NOT NULL
               OR NEW.command->>'action' IS DISTINCT FROM 'cancel_at_period_end'
               OR NEW.command ? 'payment_id'
               OR NEW.command ? 'amount_cents' THEN
              RAISE EXCEPTION 'cancellation command does not match its ledger'
                USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER recovery_command_matches_ledger
        BEFORE INSERT OR UPDATE ON app.recovery_job
        FOR EACH ROW EXECUTE FUNCTION app.recovery_command_matches_ledger()
        """
    )
    op.execute(
        """
        CREATE TABLE app.subscription_choice (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES app.tenant (id),
          ticket_id uuid NOT NULL,
          account_id uuid NOT NULL,
          customer_id uuid NOT NULL,
          subscription_id uuid NOT NULL,
          actor_user_id uuid NOT NULL,
          input_revision integer NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT subscription_choice_tenant_id_key UNIQUE (tenant_id, id),
          CONSTRAINT subscription_choice_ticket_same_tenant
            FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id),
          CONSTRAINT subscription_choice_actor_same_tenant
            FOREIGN KEY (tenant_id, actor_user_id)
            REFERENCES app.user_account (tenant_id, id),
          CONSTRAINT subscription_choice_account_same_tenant
            FOREIGN KEY (tenant_id, account_id, customer_id)
            REFERENCES app.tenant_account (tenant_id, account_id, customer_id)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.reject_subscription_choice_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF current_user IN ('resolveai_api', 'resolveai_worker') THEN
            RAISE EXCEPTION 'subscription choice history is immutable'
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
        CREATE TRIGGER subscription_choice_immutable
        BEFORE UPDATE OR DELETE ON app.subscription_choice
        FOR EACH ROW EXECUTE FUNCTION app.reject_subscription_choice_change()
        """
    )
    op.execute("ALTER TABLE app.subscription_choice ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.subscription_choice FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON app.subscription_choice
        USING (tenant_id = app.current_tenant_id())
        WITH CHECK (tenant_id = app.current_tenant_id())
        """
    )
    op.execute(
        "GRANT SELECT, INSERT ON TABLE app.subscription_choice TO resolveai_api"
    )
    op.execute(
        """
        CREATE TABLE simulator.subscription (
          id uuid PRIMARY KEY,
          account_id uuid NOT NULL REFERENCES simulator.account (id),
          plan_code text NOT NULL,
          status text NOT NULL,
          current_period_end timestamptz NOT NULL,
          cancel_at_period_end boolean NOT NULL,
          resource_version integer NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT subscription_status CHECK (status = 'active'),
          CONSTRAINT subscription_version CHECK (resource_version > 0)
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE simulator.subscription TO resolveai_simulator")
    op.execute("GRANT SELECT ON TABLE simulator.subscription TO resolveai_oracle")


def downgrade() -> None:
    """Refuse while choice or cancellation history exists. Otherwise restore 0007.

    Refund, approval, recovery, reply, and audit rows are not deleted. A choice
    row blocks downgrade even when no cancellation proposal exists.
    """
    bind = op.get_bind()
    blocking = bind.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM app.subscription_choice)
              + (SELECT count(*) FROM app.proposal
                 WHERE action_type = 'cancel_at_period_end')
              + (SELECT count(*) FROM app.action_ledger
                 WHERE action_type = 'cancel_at_period_end')
              + (SELECT count(*) FROM app.approval
                 WHERE proposal_id IN (
                   SELECT id FROM app.proposal WHERE action_type = 'cancel_at_period_end'
                 ))
              + (SELECT count(*) FROM app.approval_revocation
                 WHERE approval_id IN (
                   SELECT approval.id
                   FROM app.approval AS approval
                   JOIN app.proposal AS proposal ON proposal.id = approval.proposal_id
                   WHERE proposal.action_type = 'cancel_at_period_end'
                 ))
              + (SELECT count(*) FROM app.recovery_job
                 WHERE subscription_resource_version IS NOT NULL
                    OR ledger_id IN (
                      SELECT id FROM app.action_ledger
                      WHERE action_type = 'cancel_at_period_end'
                    ))
              + (SELECT count(*) FROM app.reply_draft
                 WHERE id IN (
                   SELECT reply_draft_id FROM app.action_ledger
                   WHERE action_type = 'cancel_at_period_end'
                     AND reply_draft_id IS NOT NULL
                 ))
            """
        )
    ).scalar_one()
    if int(blocking or 0) > 0:
        raise RuntimeError(
            "Downgrade refused because subscription choices or cancellation history exist"
        )
    op.execute("DROP POLICY tenant_isolation ON app.subscription_choice")
    op.execute("DROP TRIGGER subscription_choice_immutable ON app.subscription_choice")
    op.execute("DROP FUNCTION app.reject_subscription_choice_change()")
    op.execute("DROP TABLE app.subscription_choice")
    op.execute("DROP TABLE simulator.subscription")
    op.execute("DROP TRIGGER recovery_command_matches_ledger ON app.recovery_job")
    op.execute("DROP FUNCTION app.recovery_command_matches_ledger()")
    op.execute("ALTER TABLE app.recovery_job DROP CONSTRAINT recovery_job_resource_version")
    op.execute("ALTER TABLE app.recovery_job DROP COLUMN subscription_resource_version")
    op.execute(
        "ALTER TABLE app.recovery_job ALTER COLUMN payment_resource_version SET NOT NULL"
    )
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_shape")
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_action_type")
    op.execute("ALTER TABLE app.proposal DROP COLUMN subscription_resource_version")
    op.execute("ALTER TABLE app.proposal DROP COLUMN effective_at")
    op.execute("ALTER TABLE app.proposal DROP COLUMN target_subscription_id")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN currency SET NOT NULL")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN amount_cents SET NOT NULL")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN target_event_id SET NOT NULL")
    op.execute("ALTER TABLE app.proposal ALTER COLUMN target_payment_id SET NOT NULL")
    op.execute(
        """
        ALTER TABLE app.proposal
          ADD CONSTRAINT proposal_action_type CHECK (action_type IN ('refund'))
        """
    )
    op.execute(
        "ALTER TABLE app.proposal ADD CONSTRAINT proposal_currency CHECK (currency = 'USD')"
    )
    op.execute(
        "ALTER TABLE app.proposal ADD CONSTRAINT proposal_amount CHECK (amount_cents > 0)"
    )
    op.execute("ALTER TABLE app.action_ledger DROP CONSTRAINT ledger_action_type")
    op.execute(
        """
        ALTER TABLE app.action_ledger
          ADD CONSTRAINT ledger_action_type
          CHECK (action_type IN ('refund', 'reply'))
        """
    )
