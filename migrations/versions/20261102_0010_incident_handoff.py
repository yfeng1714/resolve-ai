"""Account-private incidents, reports, and local handoffs.

Revision ID: 20261102_0010
Revises: 20261029_0009
Create Date: 2026-11-06
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "20261102_0010"
down_revision: str | None = "20261029_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE simulator.incident (
          id uuid PRIMARY KEY,
          account_id uuid NOT NULL REFERENCES simulator.account (id),
          service_code text NOT NULL,
          region_code text NOT NULL,
          status text NOT NULL,
          started_at timestamptz NOT NULL,
          resolved_at timestamptz,
          summary text NOT NULL,
          reference_ids text[] NOT NULL,
          resource_version integer NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT incident_status CHECK (
            status IN ('investigating', 'identified', 'resolved')
          ),
          CONSTRAINT incident_version CHECK (resource_version > 0),
          CONSTRAINT incident_resolved_window CHECK (
            (
              status = 'resolved'
              AND resolved_at IS NOT NULL
              AND resolved_at >= started_at
            ) OR (
              status <> 'resolved' AND resolved_at IS NULL
            )
          )
        )
        """
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE simulator.incident TO resolveai_simulator"
    )
    op.execute("GRANT SELECT ON TABLE simulator.incident TO resolveai_oracle")
    op.execute(
        """
        ALTER TABLE app.tool_invocation
          ADD CONSTRAINT tool_invocation_tenant_id_key UNIQUE (tenant_id, id)
        """
    )
    op.execute(
        """
        CREATE TABLE app.incident_report (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES app.tenant (id),
          ticket_id uuid NOT NULL,
          account_id uuid NOT NULL,
          customer_id uuid NOT NULL,
          actor_user_id uuid NOT NULL,
          input_revision integer NOT NULL,
          service_code text NOT NULL,
          region_code text NOT NULL,
          reported_started_at timestamptz NOT NULL,
          request_id text,
          created_at timestamptz NOT NULL,
          CONSTRAINT incident_report_tenant_id_key UNIQUE (tenant_id, id),
          CONSTRAINT incident_report_ticket_same_tenant
            FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id),
          CONSTRAINT incident_report_actor_same_tenant
            FOREIGN KEY (tenant_id, actor_user_id)
            REFERENCES app.user_account (tenant_id, id),
          CONSTRAINT incident_report_account_same_tenant
            FOREIGN KEY (tenant_id, account_id, customer_id)
            REFERENCES app.tenant_account (tenant_id, account_id, customer_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.incident_handoff (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL REFERENCES app.tenant (id),
          ticket_id uuid NOT NULL,
          account_id uuid NOT NULL,
          customer_id uuid NOT NULL,
          run_id uuid NOT NULL,
          report_id uuid,
          actor_user_id uuid NOT NULL,
          input_revision integer NOT NULL,
          reply_draft_id uuid,
          tool_invocation_id uuid NOT NULL,
          observed_at timestamptz NOT NULL,
          snapshot_digest text,
          kind text NOT NULL,
          incident_id uuid,
          incident_resource_version integer,
          evidence jsonb NOT NULL,
          compensation_requested boolean NOT NULL,
          reason_code text NOT NULL,
          next_step text NOT NULL,
          created_at timestamptz NOT NULL,
          CONSTRAINT incident_handoff_tenant_id_key UNIQUE (tenant_id, id),
          CONSTRAINT incident_handoff_one_per_run UNIQUE (tenant_id, run_id),
          CONSTRAINT incident_handoff_kind CHECK (
            kind IN (
              'confirmed', 'possible', 'several', 'none', 'compensation',
              'unavailable', 'conflict'
            )
          ),
          CONSTRAINT incident_handoff_next_step CHECK (next_step = 'human_review'),
          CONSTRAINT incident_handoff_confirmed_id CHECK (
            (
              kind = 'confirmed'
              AND incident_id IS NOT NULL
              AND incident_resource_version IS NOT NULL
            ) OR (
              kind <> 'confirmed'
              AND incident_id IS NULL
              AND incident_resource_version IS NULL
            )
          ),
          CONSTRAINT incident_handoff_ticket_same_tenant
            FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id),
          CONSTRAINT incident_handoff_run_same_tenant
            FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id),
          CONSTRAINT incident_handoff_actor_same_tenant
            FOREIGN KEY (tenant_id, actor_user_id)
            REFERENCES app.user_account (tenant_id, id),
          CONSTRAINT incident_handoff_account_same_tenant
            FOREIGN KEY (tenant_id, account_id, customer_id)
            REFERENCES app.tenant_account (tenant_id, account_id, customer_id),
          CONSTRAINT incident_handoff_report_same_tenant
            FOREIGN KEY (tenant_id, report_id)
            REFERENCES app.incident_report (tenant_id, id),
          CONSTRAINT incident_handoff_observation_same_tenant
            FOREIGN KEY (tenant_id, tool_invocation_id)
            REFERENCES app.tool_invocation (tenant_id, id)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.reject_incident_history_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF current_user IN ('resolveai_api', 'resolveai_worker') THEN
            RAISE EXCEPTION 'incident history is immutable'
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
    for table in ("incident_report", "incident_handoff"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_immutable
            BEFORE UPDATE OR DELETE ON app.{table}
            FOR EACH ROW EXECUTE FUNCTION app.reject_incident_history_change()
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


def downgrade() -> None:
    bind = op.get_bind()
    blocking = bind.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM app.incident_report)
              + (SELECT count(*) FROM app.incident_handoff)
            """
        )
    ).scalar_one()
    if int(blocking or 0) > 0:
        raise RuntimeError("Downgrade refused because incident reports or handoffs exist")
    for table in ("incident_handoff", "incident_report"):
        op.execute(f"DROP POLICY tenant_isolation ON app.{table}")
        op.execute(f"DROP TRIGGER {table}_immutable ON app.{table}")
    op.execute("DROP FUNCTION app.reject_incident_history_change()")
    op.execute("DROP TABLE app.incident_handoff")
    op.execute("DROP TABLE app.incident_report")
    op.execute("ALTER TABLE app.tool_invocation DROP CONSTRAINT tool_invocation_tenant_id_key")
    op.execute("DROP TABLE simulator.incident")
