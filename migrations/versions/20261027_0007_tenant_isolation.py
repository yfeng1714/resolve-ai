"""Two-tenant row security, auth lookup, and worker claim.

Revision ID: 20261027_0007
Revises: 20261026_0006
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261027_0007"
down_revision: str | None = "20261026_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "user_account",
    "membership",
    "user_session",
    "ticket",
    "message",
    "run",
    "proposal",
    "approval",
    "approval_revocation",
    "action_ledger",
    "tool_invocation",
    "audit_event",
    "reply_draft",
    "recovery_job",
    "knowledge_document",
    "knowledge_chunk",
    "tenant_account",
)


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
    op.execute(sa.text(f"ALTER ROLE {role} WITH LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"))


def upgrade() -> None:
    auth_password = _demo_password("RESOLVEAI_AUTH_DB_PASSWORD", "resolveai_auth_local")
    worker_password = _demo_password("RESOLVEAI_WORKER_DB_PASSWORD", "resolveai_worker_local")
    _ensure_role("resolveai_auth", auth_password)
    _ensure_role("resolveai_worker", worker_password)
    op.execute("ALTER ROLE resolveai_auth SET search_path = app")
    op.execute("ALTER ROLE resolveai_worker SET search_path = app")
    op.execute("GRANT USAGE ON SCHEMA app TO resolveai_auth, resolveai_worker")
    op.execute(
        """
        DO $$
        BEGIN
          EXECUTE format(
            'GRANT CONNECT ON DATABASE %I TO resolveai_auth, resolveai_worker',
            current_database()
          );
        END
        $$;
        """
    )

    op.execute(
        """
        CREATE FUNCTION app.current_tenant_id()
        RETURNS uuid
        LANGUAGE sql
        STABLE
        SET search_path = pg_catalog
        AS $$
          SELECT nullif(current_setting('app.tenant_id', true), ''::text)::uuid
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.lookup_login(p_email text)
        RETURNS TABLE (
          user_id uuid,
          tenant_id uuid,
          password_hash text,
          role text,
          display_name text,
          email text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, app
        AS $$
          SELECT
            user_account.id,
            user_account.tenant_id,
            user_account.password_hash,
            membership.role,
            user_account.display_name,
            user_account.email
          FROM app.user_account AS user_account
          JOIN app.membership AS membership
            ON membership.tenant_id = user_account.tenant_id
           AND membership.user_id = user_account.id
          WHERE lower(pg_catalog.btrim(user_account.email))
              = lower(pg_catalog.btrim(p_email))
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.lookup_session(p_token_hash text)
        RETURNS TABLE (
          session_id uuid,
          user_id uuid,
          tenant_id uuid,
          role text,
          display_name text,
          email text,
          csrf_hash text,
          expires_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, app
        AS $$
          SELECT
            user_session.id,
            user_account.id,
            user_session.tenant_id,
            membership.role,
            user_account.display_name,
            user_account.email,
            user_session.csrf_hash,
            user_session.expires_at
          FROM app.user_session AS user_session
          JOIN app.user_account AS user_account
            ON user_account.id = user_session.user_id
           AND user_account.tenant_id = user_session.tenant_id
          JOIN app.membership AS membership
            ON membership.user_id = user_account.id
           AND membership.tenant_id = user_account.tenant_id
          WHERE user_session.token_hash = p_token_hash
            AND user_session.expires_at > pg_catalog.clock_timestamp()
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.claim_recovery_job()
        RETURNS TABLE (
          job_id uuid,
          tenant_id uuid,
          owner_token uuid,
          owner_generation integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, app
        AS $$
        BEGIN
          RETURN QUERY
          WITH candidate AS (
            SELECT recovery_job.id
            FROM app.recovery_job AS recovery_job
            WHERE (
              recovery_job.state = 'queued'
              AND recovery_job.next_attempt_at <= pg_catalog.clock_timestamp()
            ) OR (
              recovery_job.state = 'leased'
              AND recovery_job.leased_until <= pg_catalog.clock_timestamp()
            )
            ORDER BY recovery_job.next_attempt_at, recovery_job.created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
          )
          UPDATE app.recovery_job AS recovery_job
          SET state = 'leased',
              owner_token = pg_catalog.gen_random_uuid(),
              owner_generation = recovery_job.owner_generation + 1,
              leased_until = pg_catalog.clock_timestamp() + pg_catalog.make_interval(secs => 30),
              updated_at = pg_catalog.clock_timestamp()
          FROM candidate
          WHERE recovery_job.id = candidate.id
          RETURNING
            recovery_job.id,
            recovery_job.tenant_id,
            recovery_job.owner_token,
            recovery_job.owner_generation;
        END;
        $$
        """
    )
    for name, signature in (
        ("lookup_login", "app.lookup_login(text)"),
        ("lookup_session", "app.lookup_session(text)"),
        ("claim_recovery_job", "app.claim_recovery_job()"),
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(
            "REVOKE ALL ON FUNCTION "
            f"{signature} FROM resolveai_api, resolveai_auth, resolveai_worker, "
            "resolveai_oracle, resolveai_simulator"
        )
    op.execute("GRANT EXECUTE ON FUNCTION app.lookup_login(text) TO resolveai_auth")
    op.execute("GRANT EXECUTE ON FUNCTION app.lookup_session(text) TO resolveai_auth")
    op.execute("GRANT EXECUTE ON FUNCTION app.claim_recovery_job() TO resolveai_worker")

    op.execute(
        """
        CREATE UNIQUE INDEX user_account_normalized_email_key
        ON app.user_account (lower(btrim(email)))
        """
    )
    op.execute(
        """
        CREATE TABLE app.tenant_account (
          tenant_id uuid NOT NULL REFERENCES app.tenant (id),
          account_id uuid NOT NULL,
          customer_id uuid NOT NULL,
          PRIMARY KEY (tenant_id, account_id),
          UNIQUE (account_id),
          UNIQUE (tenant_id, account_id, customer_id)
        )
        """
    )
    op.execute(
        """
        INSERT INTO app.tenant_account (tenant_id, account_id, customer_id)
        SELECT DISTINCT tenant_id, account_id, customer_id
        FROM app.ticket
        """
    )
    op.execute(
        """
        CREATE FUNCTION app.reject_ticket_rebinding()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'UPDATE'
             AND current_user IN ('resolveai_api', 'resolveai_worker')
             AND (
               NEW.account_id IS DISTINCT FROM OLD.account_id
               OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
             ) THEN
            RAISE EXCEPTION 'ticket account binding is immutable'
              USING ERRCODE = '42501';
          END IF;
          IF NOT EXISTS (
            SELECT 1
            FROM app.tenant_account
            WHERE tenant_id = NEW.tenant_id
              AND account_id = NEW.account_id
              AND customer_id = NEW.customer_id
          ) THEN
            RAISE EXCEPTION 'ticket account is not bound to this tenant'
              USING ERRCODE = '42501';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER ticket_account_binding
        BEFORE INSERT OR UPDATE ON app.ticket
        FOR EACH ROW EXECUTE FUNCTION app.reject_ticket_rebinding()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.reject_approval_history_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF current_user IN ('resolveai_api', 'resolveai_worker') THEN
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

    op.execute("ALTER TABLE app.ticket ADD CONSTRAINT ticket_tenant_id_key UNIQUE (tenant_id, id)")
    op.execute(
        "ALTER TABLE app.knowledge_document "
        "ADD CONSTRAINT knowledge_document_tenant_id_key UNIQUE (tenant_id, id)"
    )
    op.execute(
        "ALTER TABLE app.reply_draft ADD CONSTRAINT reply_draft_tenant_id_key UNIQUE (tenant_id, id)"
    )
    statements = (
        """
        ALTER TABLE app.membership
          ADD CONSTRAINT membership_user_same_tenant
          FOREIGN KEY (tenant_id, user_id) REFERENCES app.user_account (tenant_id, id)
        """,
        """
        ALTER TABLE app.user_session
          ADD CONSTRAINT user_session_user_same_tenant
          FOREIGN KEY (tenant_id, user_id) REFERENCES app.user_account (tenant_id, id)
        """,
        """
        ALTER TABLE app.approval
          ADD CONSTRAINT approval_proposal_same_tenant
          FOREIGN KEY (tenant_id, proposal_id) REFERENCES app.proposal (tenant_id, id)
        """,
        """
        ALTER TABLE app.approval
          ADD CONSTRAINT approval_actor_same_tenant
          FOREIGN KEY (tenant_id, actor_user_id) REFERENCES app.user_account (tenant_id, id)
        """,
        """
        ALTER TABLE app.approval_revocation
          ADD CONSTRAINT approval_revocation_approval_same_tenant
          FOREIGN KEY (tenant_id, approval_id) REFERENCES app.approval (tenant_id, id)
        """,
        """
        ALTER TABLE app.approval_revocation
          ADD CONSTRAINT approval_revocation_actor_same_tenant
          FOREIGN KEY (tenant_id, actor_user_id) REFERENCES app.user_account (tenant_id, id)
        """,
        """
        ALTER TABLE app.message
          ADD CONSTRAINT message_ticket_same_tenant
          FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id)
        """,
        """
        ALTER TABLE app.run
          ADD CONSTRAINT run_ticket_same_tenant
          FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id)
        """,
        """
        ALTER TABLE app.proposal
          ADD CONSTRAINT proposal_ticket_same_tenant
          FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id)
        """,
        """
        ALTER TABLE app.proposal
          ADD CONSTRAINT proposal_run_same_tenant
          FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id)
        """,
        """
        ALTER TABLE app.reply_draft
          ADD CONSTRAINT reply_draft_ticket_same_tenant
          FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id)
        """,
        """
        ALTER TABLE app.reply_draft
          ADD CONSTRAINT reply_draft_run_same_tenant
          FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id)
        """,
        """
        ALTER TABLE app.action_ledger
          ADD CONSTRAINT action_ledger_proposal_same_tenant
          FOREIGN KEY (tenant_id, proposal_id) REFERENCES app.proposal (tenant_id, id)
        """,
        """
        ALTER TABLE app.action_ledger
          ADD CONSTRAINT action_ledger_reply_same_tenant
          FOREIGN KEY (tenant_id, reply_draft_id) REFERENCES app.reply_draft (tenant_id, id)
        """,
        """
        ALTER TABLE app.tool_invocation
          ADD CONSTRAINT tool_invocation_run_same_tenant
          FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id)
        """,
        """
        ALTER TABLE app.audit_event
          ADD CONSTRAINT audit_event_actor_same_tenant
          FOREIGN KEY (tenant_id, actor_user_id) REFERENCES app.user_account (tenant_id, id)
        """,
        """
        ALTER TABLE app.audit_event
          ADD CONSTRAINT audit_event_ticket_same_tenant
          FOREIGN KEY (tenant_id, ticket_id) REFERENCES app.ticket (tenant_id, id)
        """,
        """
        ALTER TABLE app.audit_event
          ADD CONSTRAINT audit_event_run_same_tenant
          FOREIGN KEY (tenant_id, run_id) REFERENCES app.run (tenant_id, id)
        """,
        """
        ALTER TABLE app.knowledge_chunk
          ADD CONSTRAINT knowledge_chunk_document_same_tenant
          FOREIGN KEY (tenant_id, document_id)
          REFERENCES app.knowledge_document (tenant_id, id)
        """,
        """
        ALTER TABLE app.ticket
          ADD CONSTRAINT ticket_account_same_tenant
          FOREIGN KEY (tenant_id, account_id, customer_id)
          REFERENCES app.tenant_account (tenant_id, account_id, customer_id)
        """,
    )
    for statement in statements:
        op.execute(statement)

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON app.{table}
            USING (tenant_id = app.current_tenant_id())
            WITH CHECK (tenant_id = app.current_tenant_id())
            """
        )
    op.execute("ALTER TABLE app.tenant ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.tenant FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON app.tenant
        USING (id = app.current_tenant_id())
        WITH CHECK (id = app.current_tenant_id())
        """
    )

    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA app TO resolveai_worker"
    )
    op.execute("GRANT SELECT ON TABLE app.tenant_account TO resolveai_api, resolveai_oracle")
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE ON TABLE app.tenant_account "
        "FROM resolveai_api, resolveai_worker"
    )
    op.execute("REVOKE UPDATE, DELETE ON TABLE app.approval FROM resolveai_worker")
    op.execute("GRANT UPDATE (id) ON TABLE app.approval TO resolveai_worker")
    op.execute(
        "REVOKE INSERT, UPDATE, DELETE ON TABLE app.approval_revocation FROM resolveai_worker"
    )
    op.execute("GRANT SELECT ON TABLE app.approval_revocation TO resolveai_worker")


def downgrade() -> None:
    """Reverse this revision's database-local objects and restore the 0006 contract.

    ``resolveai_auth`` and ``resolveai_worker`` are cluster-wide roles and may
    already exist for the other local database. Downgrade revokes this
    database's privileges and resets their search paths. It does not drop the
    roles.
    """
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA app FROM resolveai_worker")
    op.execute("REVOKE USAGE ON SCHEMA app FROM resolveai_auth, resolveai_worker")
    op.execute(
        """
        DO $$
        BEGIN
          EXECUTE format(
            'REVOKE CONNECT ON DATABASE %I FROM resolveai_auth, resolveai_worker',
            current_database()
          );
        END
        $$;
        """
    )
    op.execute("ALTER ROLE resolveai_auth RESET search_path")
    op.execute("ALTER ROLE resolveai_worker RESET search_path")

    for table in ("tenant", *TENANT_TABLES):
        op.execute(f"DROP POLICY tenant_isolation ON app.{table}")
        op.execute(f"ALTER TABLE app.{table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} DISABLE ROW LEVEL SECURITY")

    for name, table in (
        ("ticket_account_same_tenant", "ticket"),
        ("membership_user_same_tenant", "membership"),
        ("user_session_user_same_tenant", "user_session"),
        ("approval_proposal_same_tenant", "approval"),
        ("approval_actor_same_tenant", "approval"),
        ("approval_revocation_approval_same_tenant", "approval_revocation"),
        ("approval_revocation_actor_same_tenant", "approval_revocation"),
        ("message_ticket_same_tenant", "message"),
        ("run_ticket_same_tenant", "run"),
        ("proposal_ticket_same_tenant", "proposal"),
        ("proposal_run_same_tenant", "proposal"),
        ("reply_draft_ticket_same_tenant", "reply_draft"),
        ("reply_draft_run_same_tenant", "reply_draft"),
        ("action_ledger_proposal_same_tenant", "action_ledger"),
        ("action_ledger_reply_same_tenant", "action_ledger"),
        ("tool_invocation_run_same_tenant", "tool_invocation"),
        ("audit_event_actor_same_tenant", "audit_event"),
        ("audit_event_ticket_same_tenant", "audit_event"),
        ("audit_event_run_same_tenant", "audit_event"),
        ("knowledge_chunk_document_same_tenant", "knowledge_chunk"),
    ):
        op.execute(f"ALTER TABLE app.{table} DROP CONSTRAINT {name}")
    for name, table in (
        ("ticket_tenant_id_key", "ticket"),
        ("knowledge_document_tenant_id_key", "knowledge_document"),
        ("reply_draft_tenant_id_key", "reply_draft"),
    ):
        op.execute(f"ALTER TABLE app.{table} DROP CONSTRAINT {name}")
    op.execute("DROP INDEX app.user_account_normalized_email_key")
    op.execute("DROP TRIGGER ticket_account_binding ON app.ticket")
    op.execute("DROP FUNCTION app.reject_ticket_rebinding()")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.reject_approval_history_change()
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
    op.execute("DROP TABLE app.tenant_account")
    op.execute("DROP FUNCTION app.claim_recovery_job()")
    op.execute("DROP FUNCTION app.lookup_session(text)")
    op.execute("DROP FUNCTION app.lookup_login(text)")
    op.execute("DROP FUNCTION app.current_tenant_id()")
