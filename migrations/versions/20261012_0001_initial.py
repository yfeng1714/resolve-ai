"""Initial app and simulator schemas.

Revision ID: 20261012_0001
Revises:
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261012_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUN_STATES = (
    "analyzing",
    "gathering_evidence",
    "proposing",
    "awaiting_customer",
    "awaiting_approval",
    "executing",
    "verifying",
    "awaiting_reply_review",
    "completed",
    "handed_off",
    "failed",
    "cancelled",
    "reconciling",
)


def _quote_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _demo_password(env_name: str, default: str) -> str:
    import os

    value = os.environ.get(env_name, default)
    if not value.isascii() or not value.replace("_", "").isalnum():
        raise RuntimeError(f"{env_name} must be an alphanumeric local demo password")
    return value


def _ensure_role(role: str, password: str) -> None:
    op.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                CREATE ROLE {role} LOGIN PASSWORD '{password}';
              END IF;
            END
            $$;
            """
        )
    )
    op.execute(sa.text(f"ALTER ROLE {role} WITH LOGIN PASSWORD '{password}'"))


def upgrade() -> None:
    api_password = _demo_password("RESOLVEAI_API_DB_PASSWORD", "resolveai_api_local")
    simulator_password = _demo_password(
        "RESOLVEAI_SIMULATOR_DB_PASSWORD", "resolveai_simulator_local"
    )
    oracle_password = _demo_password("RESOLVEAI_ORACLE_DB_PASSWORD", "resolveai_oracle_local")

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE SCHEMA IF NOT EXISTS app")
    op.execute("CREATE SCHEMA IF NOT EXISTS simulator")
    _ensure_role("resolveai_api", api_password)
    _ensure_role("resolveai_simulator", simulator_password)
    _ensure_role("resolveai_oracle", oracle_password)

    op.create_table(
        "tenant",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        schema="app",
    )
    op.create_table(
        "user_account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.UniqueConstraint("tenant_id", "email"),
        schema="app",
    )
    op.create_table(
        "membership",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.user_account.id"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('support', 'approver')", name="membership_role"),
        sa.UniqueConstraint("tenant_id", "user_id"),
        schema="app",
    )
    op.create_table(
        "user_session",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.user_account.id"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("csrf_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="app",
    )
    op.create_table(
        "ticket",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False, index=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_label", sa.Text(), nullable=False),
        sa.Column("customer_email", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('open', 'awaiting_approval', 'awaiting_reply', 'handed_off', 'reply_sent')",
            name="ticket_status",
        ),
        schema="app",
    )
    op.create_table(
        "message",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.ticket.id"), nullable=False, index=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("author_kind", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "author_kind IN ('customer', 'operator', 'internal', 'system')",
            name="message_author_kind",
        ),
        sa.UniqueConstraint("ticket_id", "revision"),
        schema="app",
    )
    op.create_table(
        "run",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.ticket.id"), nullable=False, index=True),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("message_revision", sa.Integer(), nullable=False),
        sa.Column("observations", postgresql.JSONB(), nullable=False),
        sa.Column("concise_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("replans", sa.Integer(), nullable=False),
        sa.Column("read_retries", sa.Integer(), nullable=False),
        sa.Column("budget_usd_micros", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"state IN ({_quote_list(RUN_STATES)})", name="run_state"),
        schema="app",
    )
    op.create_table(
        "proposal",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.ticket.id"), nullable=False, index=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.run.id"), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("target_payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_event_id", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("preconditions", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action_type IN ('refund')", name="proposal_action_type"),
        sa.CheckConstraint("currency = 'USD'", name="proposal_currency"),
        sa.CheckConstraint("amount_cents > 0", name="proposal_amount"),
        sa.CheckConstraint(
            "status IN ('open', 'approved', 'rejected', 'superseded', 'executed', 'stale', 'failed')",
            name="proposal_status",
        ),
        sa.UniqueConstraint("tenant_id", "action_id", "revision"),
        schema="app",
    )
    op.create_table(
        "approval",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.proposal.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.user_account.id"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("decision IN ('approved', 'rejected')", name="approval_decision"),
        sa.UniqueConstraint("proposal_id"),
        schema="app",
    )
    op.create_table(
        "reply_draft",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.ticket.id"), nullable=False, index=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.run.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("send_action_id", sa.Text(), nullable=False),
        sa.Column("receipt", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'sent', 'rejected', 'superseded')",
            name="reply_status",
        ),
        sa.UniqueConstraint("ticket_id", "revision"),
        sa.UniqueConstraint("tenant_id", "send_action_id"),
        schema="app",
    )
    op.create_table(
        "action_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.proposal.id"), nullable=True),
        sa.Column("reply_draft_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.reply_draft.id"), nullable=True),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dispatch_state", sa.Text(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action_type IN ('refund', 'reply')", name="ledger_action_type"),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected', 'unknown')",
            name="ledger_status",
        ),
        sa.CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatched', 'settled')",
            name="ledger_dispatch_state",
        ),
        sa.UniqueConstraint("tenant_id", "action_id"),
        schema="app",
    )
    op.create_table(
        "tool_invocation",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.run.id"), nullable=False, index=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("operation_id", sa.Text(), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        schema="app",
    )
    op.create_table(
        "audit_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("app.tenant.id"), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="app",
    )

    op.create_table(
        "customer",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        schema="simulator",
    )
    op.create_table(
        "account",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulator.customer.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("currency = 'USD'", name="account_currency"),
        schema="simulator",
    )
    op.create_table(
        "invoice",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulator.account.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("currency = 'USD'", name="invoice_currency"),
        sa.CheckConstraint("amount_cents >= 0", name="invoice_amount"),
        sa.CheckConstraint("status IN ('open', 'paid', 'void')", name="invoice_status"),
        sa.UniqueConstraint("account_id", "number"),
        schema="simulator",
    )
    op.create_table(
        "payment",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulator.account.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("invoice_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("simulator.invoice.id")),
        sa.Column("event_id", sa.Text(), nullable=False, index=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("currency = 'USD'", name="payment_currency"),
        sa.CheckConstraint("amount_cents > 0", name="payment_amount"),
        sa.CheckConstraint("status IN ('pending', 'settled', 'failed')", name="payment_status"),
        sa.CheckConstraint(
            "status <> 'settled' OR settled_at IS NOT NULL",
            name="payment_settled_timestamp",
        ),
        schema="simulator",
    )
    op.create_table(
        "refund",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("simulator.account.id"), nullable=False),
        sa.Column(
            "payment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("simulator.payment.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("action_id", sa.Text(), nullable=False, unique=True),
        sa.Column("operation_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("currency = 'USD'", name="refund_currency"),
        sa.CheckConstraint("amount_cents > 0", name="refund_amount"),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="refund_status"),
        schema="simulator",
    )
    op.create_index(
        "refund_one_open_outcome_per_event",
        "refund",
        ["event_id"],
        unique=True,
        schema="simulator",
        postgresql_where=sa.text("status IN ('pending', 'succeeded')"),
    )
    op.create_table(
        "operation",
        sa.Column("action_id", sa.Text(), primary_key=True),
        sa.Column("operation_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("write_attempts", sa.Integer(), nullable=False),
        sa.Column("replay_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'rejected')", name="operation_status"),
        schema="simulator",
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION simulator.enforce_refund_balance()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
          payment_amount integer;
          already integer;
        BEGIN
          SELECT amount_cents INTO payment_amount
          FROM simulator.payment
          WHERE id = NEW.payment_id;
          IF payment_amount IS NULL THEN
            RAISE EXCEPTION 'refund payment is missing' USING ERRCODE = '23503';
          END IF;
          SELECT COALESCE(SUM(amount_cents), 0) INTO already
          FROM simulator.refund
          WHERE payment_id = NEW.payment_id
            AND status IN ('pending', 'succeeded')
            AND id <> NEW.id;
          IF already + NEW.amount_cents > payment_amount THEN
            RAISE EXCEPTION 'refund exceeds payment balance' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER refund_balance
        BEFORE INSERT OR UPDATE ON simulator.refund
        FOR EACH ROW
        WHEN (NEW.status IN ('pending', 'succeeded'))
        EXECUTE FUNCTION simulator.enforce_refund_balance();
        """
    )

    op.execute("REVOKE ALL ON SCHEMA app FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA simulator FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA app TO resolveai_api, resolveai_oracle")
    op.execute("GRANT USAGE ON SCHEMA simulator TO resolveai_simulator, resolveai_oracle")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO resolveai_api"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA simulator "
        "TO resolveai_simulator"
    )
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA app TO resolveai_oracle")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA simulator TO resolveai_oracle")
    op.execute(
        """
        DO $$
        BEGIN
          EXECUTE format(
            'GRANT CONNECT ON DATABASE %I TO resolveai_api, resolveai_simulator, resolveai_oracle',
            current_database()
          );
        END
        $$;
        """
    )
    op.execute("ALTER ROLE resolveai_api SET search_path = app")
    op.execute("ALTER ROLE resolveai_simulator SET search_path = simulator")
    op.execute("ALTER ROLE resolveai_oracle SET search_path = app, simulator")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS app CASCADE")
    op.execute("DROP SCHEMA IF EXISTS simulator CASCADE")
    op.execute("DROP EXTENSION IF EXISTS vector")
    for role in ("resolveai_api", "resolveai_simulator", "resolveai_oracle"):
        op.execute(f"REVOKE ALL ON DATABASE resolveai_demo FROM {role}")
        op.execute(f"DROP OWNED BY {role}")
        op.execute(f"DROP ROLE IF EXISTS {role}")
