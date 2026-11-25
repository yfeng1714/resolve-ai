"""Tighten refund nulls and immutable cancellation command bindings.

Revision ID: 20261029_0009
Revises: 20261028_0008
Create Date: 2026-10-29

Already-applied 0008 databases do not pick up an edit to that file. This
revision replaces the live check and command trigger.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261029_0009"
down_revision: str | None = "20261028_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STRONG_SHAPE = """
ALTER TABLE app.proposal
  ADD CONSTRAINT proposal_shape CHECK (
    (
      action_type = 'refund'
      AND target_payment_id IS NOT NULL
      AND target_event_id IS NOT NULL
      AND amount_cents IS NOT NULL
      AND amount_cents > 0
      AND currency IS NOT NULL
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

_PREVIOUS_SHAPE = """
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

_BOUND_FUNCTION = """
CREATE OR REPLACE FUNCTION app.recovery_command_matches_ledger()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  ledger_type text;
BEGIN
  IF TG_OP = 'UPDATE' AND current_user IN ('resolveai_api', 'resolveai_worker') THEN
    IF NEW.command IS DISTINCT FROM OLD.command
       OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
       OR NEW.subscription_resource_version IS DISTINCT FROM OLD.subscription_resource_version
       OR NEW.payment_resource_version IS DISTINCT FROM OLD.payment_resource_version
       OR NEW.approval_id IS DISTINCT FROM OLD.approval_id
       OR NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
       OR NEW.proposal_revision IS DISTINCT FROM OLD.proposal_revision
       OR NEW.input_revision IS DISTINCT FROM OLD.input_revision
       OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
       OR NEW.ledger_id IS DISTINCT FROM OLD.ledger_id
       OR NEW.command_generation IS DISTINCT FROM OLD.command_generation
       OR NEW.authorizer_user_id IS DISTINCT FROM OLD.authorizer_user_id
    THEN
      RAISE EXCEPTION 'recovery command bindings are immutable'
        USING ERRCODE = '42501';
    END IF;
  END IF;
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
       OR NEW.command ? 'amount_cents'
       OR NEW.command->>'expected_resource_version' IS NULL
       OR (NEW.command->>'expected_resource_version')::integer
            IS DISTINCT FROM NEW.subscription_resource_version
       OR NEW.command->>'attempt_hash' IS DISTINCT FROM NEW.payload_hash
       OR NEW.command->>'effect_hash' IS NULL
       OR NEW.command->>'action_id' IS NULL
       OR NEW.command->>'subscription_id' IS NULL
       OR NEW.command->>'effective_at' IS NULL
       OR NEW.command->>'account_id' IS NULL
       OR NEW.command->>'policy_version' IS DISTINCT FROM NEW.policy_version
    THEN
      RAISE EXCEPTION 'cancellation command does not match its ledger'
        USING ERRCODE = '23514';
    END IF;
  END IF;
  RETURN NEW;
END;
$$
"""

_PREVIOUS_FUNCTION = """
CREATE OR REPLACE FUNCTION app.recovery_command_matches_ledger()
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


def upgrade() -> None:
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_shape")
    op.execute(_STRONG_SHAPE)
    op.execute(_BOUND_FUNCTION)


def downgrade() -> None:
    op.execute(_PREVIOUS_FUNCTION)
    op.execute("ALTER TABLE app.proposal DROP CONSTRAINT proposal_shape")
    op.execute(_PREVIOUS_SHAPE)
