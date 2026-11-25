"""Prove migration 0007 reverses to the 0006 contract and applies again.

The auth and worker roles are cluster-wide, so the reverse keeps the roles
and removes only this database's privileges and objects.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from resolveai.db.session import make_engine
from resolveai.settings import repo_root


def _alembic(settings, *args: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": settings.database_url,
            "API_DATABASE_URL": settings.api_database_url,
            "AUTH_DATABASE_URL": settings.auth_database_url,
            "WORKER_DATABASE_URL": settings.worker_database_url,
            "SIMULATOR_DATABASE_URL": settings.simulator_database_url,
            "ORACLE_DATABASE_URL": settings.oracle_database_url,
            "PYTHONPATH": str(repo_root() / "src"),
        }
    )
    subprocess.check_call(
        [sys.executable, "-m", "alembic", *args],
        cwd=repo_root(),
        env=env,
    )


def _snapshot(connection) -> dict[str, object]:
    counts = connection.execute(
        text(
            """
            SELECT
              (SELECT count(*) FROM app.ticket),
              (SELECT count(*) FROM app.user_account),
              (SELECT count(*) FROM app.message),
              (SELECT count(*) FROM simulator.refund),
              (SELECT count(*) FROM simulator.payment),
              (
                SELECT coalesce(md5(string_agg(
                  id::text || payment_id::text || event_id || amount_cents::text
                    || status || action_id,
                  ',' ORDER BY id
                )), '')
                FROM simulator.refund
              )
            """
        )
    ).one()
    ticket = connection.execute(
        text(
            """
            SELECT id::text, subject, account_id::text, customer_id::text
            FROM app.ticket
            ORDER BY created_at, id
            LIMIT 1
            """
        )
    ).one()
    return {
        "counts": tuple(counts),
        "ticket": tuple(ticket),
    }


def _state(connection) -> dict[str, object]:
    version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    forced = connection.execute(
        text(
            """
            SELECT count(*)
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = 'app'
              AND relation.relkind = 'r'
              AND relation.relforcerowsecurity
            """
        )
    ).scalar_one()
    relations = set(
        connection.execute(
            text(
                """
                SELECT relation.relname
                FROM pg_class AS relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'app' AND relation.relkind = 'r'
                """
            )
        ).scalars()
    )
    indexes = set(
        connection.execute(
            text(
                """
                SELECT index_name.relname
                FROM pg_class AS index_name
                JOIN pg_namespace AS namespace ON namespace.oid = index_name.relnamespace
                WHERE namespace.nspname = 'app' AND index_name.relkind = 'i'
                """
            )
        ).scalars()
    )
    constraints = set(
        connection.execute(
            text(
                """
                SELECT constraint_name
                FROM information_schema.table_constraints
                WHERE constraint_schema = 'app'
                """
            )
        ).scalars()
    )
    functions = set(
        connection.execute(
            text(
                """
                SELECT routine.proname
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
                WHERE namespace.nspname = 'app'
                """
            )
        ).scalars()
    )
    trigger = connection.execute(
        text(
            """
            SELECT count(*)
            FROM pg_trigger
            WHERE tgname = 'ticket_account_binding' AND NOT tgisinternal
            """
        )
    ).scalar_one()
    history = connection.execute(
        text(
            """
            SELECT routine.prosrc
            FROM pg_proc AS routine
            JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
            WHERE namespace.nspname = 'app'
              AND routine.proname = 'reject_approval_history_change'
            """
        )
    ).scalar_one()
    roles = connection.execute(
        text(
            """
            SELECT count(*) FROM pg_roles
            WHERE rolname IN ('resolveai_auth', 'resolveai_worker')
            """
        )
    ).scalar_one()
    worker_ticket = connection.execute(
        text("SELECT has_table_privilege('resolveai_worker', 'app.ticket', 'SELECT')")
    ).scalar_one()
    return {
        "version": version,
        "forced": int(forced),
        "tenant_account": "tenant_account" in relations,
        "email_index": "user_account_normalized_email_key" in indexes,
        "ticket_fk": "ticket_account_same_tenant" in constraints,
        "prior_fk": "recovery_job_proposal_same_tenant" in constraints,
        "claim": "claim_recovery_job" in functions,
        "lookup": "lookup_login" in functions,
        "rebinding": "reject_ticket_rebinding" in functions,
        "trigger": int(trigger),
        "history": history,
        "roles": int(roles),
        "worker_ticket": bool(worker_ticket),
    }


def test_tenant_migration_downgrades_and_reapplies(settings, seeded) -> None:
    del seeded
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            before_data = _snapshot(connection)
            before = _state(connection)
        assert before["version"] == "20261112_0014"
        assert before["tenant_account"] is True
        assert before["worker_ticket"] is True
        try:
            _alembic(settings, "downgrade", "20261027_0007")
            with engine.connect() as connection:
                refund_only = _snapshot(connection)
                at_prior = _state(connection)
                subscription_table = connection.execute(
                    text("SELECT to_regclass('simulator.subscription') IS NOT NULL")
                ).scalar_one()
                choice_table = connection.execute(
                    text("SELECT to_regclass('app.subscription_choice') IS NOT NULL")
                ).scalar_one()
            assert refund_only == before_data
            assert at_prior["version"] == "20261027_0007"
            assert at_prior["forced"] == 18
            assert subscription_table is False
            assert choice_table is False
            _alembic(settings, "downgrade", "20261026_0006")
            with engine.connect() as connection:
                downgraded_data = _snapshot(connection)
                downgraded = _state(connection)
            assert downgraded_data == before_data
            assert downgraded["version"] == "20261026_0006"
            assert downgraded["forced"] == 0
            assert downgraded["tenant_account"] is False
            assert downgraded["email_index"] is False
            assert downgraded["ticket_fk"] is False
            assert downgraded["prior_fk"] is True
            assert downgraded["claim"] is False
            assert downgraded["lookup"] is False
            assert downgraded["rebinding"] is False
            assert downgraded["trigger"] == 0
            assert "resolveai_worker" not in str(downgraded["history"])
            assert "current_user = 'resolveai_api'" in str(downgraded["history"])
            assert downgraded["roles"] == 2
            assert downgraded["worker_ticket"] is False
            _alembic(settings, "upgrade", "20261027_0007")
            with engine.connect() as connection:
                restored_data = _snapshot(connection)
                restored = _state(connection)
                unbound = connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM app.ticket AS ticket
                        WHERE NOT EXISTS (
                          SELECT 1
                          FROM app.tenant_account AS binding
                          WHERE binding.tenant_id = ticket.tenant_id
                            AND binding.account_id = ticket.account_id
                            AND binding.customer_id = ticket.customer_id
                        )
                        """
                    )
                ).scalar_one()
            assert restored_data == before_data
            assert restored["version"] == "20261027_0007"
            assert restored["forced"] == 18
            assert restored["tenant_account"] is True
            assert restored["email_index"] is True
            assert restored["ticket_fk"] is True
            assert restored["prior_fk"] is True
            assert restored["claim"] is True
            assert restored["lookup"] is True
            assert restored["rebinding"] is True
            assert restored["trigger"] == 1
            assert "resolveai_worker" in str(restored["history"])
            assert restored["roles"] == 2
            assert restored["worker_ticket"] is True
            assert unbound == 0
            path = repo_root() / "migrations/versions/20261027_0007_tenant_isolation.py"
            spec = importlib.util.spec_from_file_location("tenant_migration", path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with Operations.context(MigrationContext.configure(connection)):
                        module.downgrade()
                        module.upgrade()
                finally:
                    transaction.rollback()
                version = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                forced = connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_class AS relation
                        JOIN pg_namespace AS namespace
                          ON namespace.oid = relation.relnamespace
                        WHERE namespace.nspname = 'app'
                          AND relation.relkind = 'r'
                          AND relation.relforcerowsecurity
                        """
                    )
                ).scalar_one()
            assert version == "20261027_0007"
            assert forced == 18
        finally:
            _alembic(settings, "upgrade", "head")
        with engine.connect() as connection:
            restored_data = _snapshot(connection)
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            forced = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname = 'app'
                      AND relation.relkind = 'r'
                      AND relation.relforcerowsecurity
                    """
                )
            ).scalar_one()
        assert restored_data == before_data
        assert version == "20261112_0014"
        assert int(forced) == 23
    finally:
        engine.dispose()
