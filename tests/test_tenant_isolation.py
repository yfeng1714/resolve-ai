"""M3.3 two-tenant isolation. These tests do not call a paid model."""

from __future__ import annotations

import subprocess
import sys
import threading

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.support import (
    HARBOR_TENANT_ID,
    NORTHWIND_TENANT_ID,
    approver_headers,
    bound_sessions,
    login,
    support_headers,
)
from tests.test_auth_and_workflow import _refund_count
from tests.test_recovery import _worker_env

from resolveai.db.session import make_engine, make_session_factory
from resolveai.services import recovery
from resolveai.settings import repo_root

HARBOR_TICKET = "55555555-5555-4555-8555-555555555511"
NORTHWIND_TICKET = "55555555-5555-4555-8555-555555555501"
HARBOR_ACCOUNT = "44444444-4444-4444-8444-444444444411"
HARBOR_PAYMENT = "66666666-6666-4666-8666-666666666672"
NORTHWIND_PAYMENT = "66666666-6666-4666-8666-666666666612"


def _tickets(client, headers: dict[str, str]) -> list[dict]:
    response = client.get("/tickets", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]["tickets"]


def _investigate_as(client, ticket_id: str, headers: dict[str, str]) -> dict:
    response = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _set(connection, tenant_id: str | None) -> None:
    if tenant_id is None:
        return
    connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": tenant_id},
    )


def test_each_tenant_completes_its_own_refund(client, settings, manifest) -> None:
    del manifest
    northwind = support_headers(client)
    north = _investigate_as(client, NORTHWIND_TICKET, northwind)
    proposal = north["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert (
        client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body).status_code
        == 200
    )
    assert (
        client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body).status_code
        == 200
    )

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate_as(client, HARBOR_TICKET, harbor_support)
    harbor_proposal = harbor["proposals"][-1]
    assert harbor_proposal["amount_cents"] == 1800
    harbor_body = {
        "revision": harbor_proposal["revision"],
        "payload_hash": harbor_proposal["payload_hash"],
    }
    outsider = approver_headers(client)
    denied = client.post(
        f"/proposals/{harbor_proposal['id']}/approve", headers=outsider, json=harbor_body
    )
    assert denied.status_code == 404
    harbor_approver = login(
        client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass"
    )
    assert (
        client.post(
            f"/proposals/{harbor_proposal['id']}/approve",
            headers=harbor_approver,
            json=harbor_body,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/proposals/{harbor_proposal['id']}/execute",
            headers=harbor_approver,
            json=harbor_body,
        ).status_code
        == 200
    )
    from resolveai.worker.__main__ import drain

    assert drain(settings, limit=5) == 2
    assert _refund_count(settings, NORTHWIND_PAYMENT) == 1
    assert _refund_count(settings, HARBOR_PAYMENT) == 1


def test_guessed_cross_tenant_objects_are_denied(client, settings) -> None:
    northwind = support_headers(client)
    assert client.get(f"/tickets/{HARBOR_TICKET}", headers=northwind).status_code == 404
    subjects = {row["subject"] for row in _tickets(client, northwind)}
    assert "Duplicate charge on INV-1001" in subjects
    assert "Duplicate charge on INV-3001" not in subjects
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        evidence_id = connection.execute(
            text(
                """
                SELECT chunk.id
                FROM app.knowledge_chunk AS chunk
                JOIN app.knowledge_document AS document ON document.id = chunk.document_id
                WHERE document.tenant_id = :tenant_id
                  AND document.access_scope = 'operator'
                  AND document.is_current
                LIMIT 1
                """
            ),
            {"tenant_id": HARBOR_TENANT_ID},
        ).scalar_one()
    engine.dispose()
    hidden = client.get(f"/evidence/{evidence_id}", headers=northwind)
    assert hidden.status_code == 404
    harbor = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    visible = client.get(f"/evidence/{evidence_id}", headers=harbor)
    assert visible.status_code == 200, visible.text
    assert client.get(f"/tickets/{NORTHWIND_TICKET}", headers=harbor).status_code == 404


def test_context_survives_commit_and_rollback_and_fails_closed(settings) -> None:
    engine = make_engine(settings.api_database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM app.ticket")).scalar_one() == 0
        _set(connection, "")
        assert connection.execute(text("SELECT count(*) FROM app.ticket")).scalar_one() == 0
        connection.rollback()
        _set(connection, NORTHWIND_TENANT_ID)
        assert connection.execute(text("SELECT count(*) FROM app.ticket")).scalar_one() == 20
        connection.commit()
        assert connection.execute(text("SELECT count(*) FROM app.ticket")).scalar_one() == 0
        _set(connection, NORTHWIND_TENANT_ID)
        connection.rollback()
        assert connection.execute(text("SELECT count(*) FROM app.ticket")).scalar_one() == 0
    factory = make_session_factory(engine)
    first = factory()
    first.info["tenant_id"] = NORTHWIND_TENANT_ID
    assert first.scalar(text("SELECT count(*) FROM app.ticket")) == 20
    first.commit()
    assert first.scalar(text("SELECT count(*) FROM app.ticket")) == 20
    first.rollback()
    assert first.scalar(text("SELECT count(*) FROM app.ticket")) == 20
    first.close()
    second = factory()
    assert second.scalar(text("SELECT count(*) FROM app.ticket")) == 0
    second.close()
    engine.dispose()


def test_api_cannot_write_another_tenant_or_rebind_an_account(settings) -> None:
    engine = make_engine(settings.api_database_url)
    with engine.connect() as connection:
        _set(connection, NORTHWIND_TENANT_ID)
        updated = connection.execute(
            text("UPDATE app.ticket SET subject = 'crossed' WHERE id = :id"),
            {"id": HARBOR_TICKET},
        )
        assert updated.rowcount == 0
        deleted = connection.execute(
            text("DELETE FROM app.ticket WHERE id = :id"),
            {"id": HARBOR_TICKET},
        )
        assert deleted.rowcount == 0
        with pytest.raises(DBAPIError):
            connection.execute(
                text(
                    """
                    INSERT INTO app.ticket (
                      id, tenant_id, customer_id, account_id, customer_label,
                      customer_email, subject, status, created_at, updated_at
                    ) VALUES (
                      '55555555-5555-4555-8555-555555555599', :tenant_id,
                      '33333333-3333-4333-8333-333333333311', :account_id,
                      'Nope', 'nope@customers.demo.test', 'Cross insert', 'open',
                      clock_timestamp(), clock_timestamp()
                    )
                    """
                ),
                {"tenant_id": HARBOR_TENANT_ID, "account_id": HARBOR_ACCOUNT},
            )
            connection.commit()
        connection.rollback()
        _set(connection, NORTHWIND_TENANT_ID)
        with pytest.raises(DBAPIError):
            connection.execute(
                text("UPDATE app.ticket SET account_id = :account_id WHERE id = :id"),
                {"account_id": HARBOR_ACCOUNT, "id": NORTHWIND_TICKET},
            )
            connection.commit()
        connection.rollback()
    engine.dispose()
    assert _refund_count(settings, HARBOR_PAYMENT) == 0
    assert _refund_count(settings, NORTHWIND_PAYMENT) == 0


def test_redirected_command_does_not_reach_the_other_account(client, settings, manifest) -> None:
    del manifest
    proposal = _investigate_as(client, NORTHWIND_TICKET, support_headers(client))["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert (
        client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body).status_code
        == 200
    )
    queued = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert queued.status_code == 200, queued.text
    job_id = queued.json()["data"]["recovery_jobs"][-1]["id"]
    admin = make_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.recovery_job
                SET command = jsonb_set(
                  command, '{account_id}', to_jsonb(CAST(:account_id AS text))
                )
                WHERE id = :id
                """
            ),
            {"account_id": HARBOR_ACCOUNT, "id": job_id},
        )
    admin.dispose()
    from resolveai.worker.__main__ import drain

    drain(settings, limit=2)
    assert _refund_count(settings, HARBOR_PAYMENT) == 0
    assert _refund_count(settings, NORTHWIND_PAYMENT) == 0
    factory = bound_sessions(make_engine(settings.worker_database_url))
    with factory() as session:
        reason = session.execute(
            text("SELECT block_reason FROM app.recovery_job WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
    assert reason == "account_not_bound"


def test_auth_and_claim_functions_are_separately_granted(settings) -> None:
    admin = make_engine(settings.database_url)
    with admin.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT routine.proname, owner.rolname, routine.prosecdef, routine.proconfig
                FROM pg_proc AS routine
                JOIN pg_namespace AS namespace ON namespace.oid = routine.pronamespace
                JOIN pg_roles AS owner ON owner.oid = routine.proowner
                WHERE namespace.nspname = 'app'
                  AND routine.proname IN ('lookup_login', 'lookup_session', 'claim_recovery_job')
                """
            )
        ).all()
        found = {row.proname: row for row in rows}
        assert set(found) == {"lookup_login", "lookup_session", "claim_recovery_job"}
        for row in rows:
            assert row.rolname == "resolveai_demo"
            assert row.prosecdef is True
            assert row.proconfig is not None
            assert any(
                str(item).startswith("search_path=pg_catalog, app") for item in row.proconfig
            )
        privileges = connection.execute(
            text(
                """
                SELECT
                  has_function_privilege('resolveai_auth', 'app.lookup_login(text)', 'execute'),
                  has_function_privilege('resolveai_api', 'app.lookup_login(text)', 'execute'),
                  has_function_privilege('resolveai_worker', 'app.lookup_login(text)', 'execute'),
                  has_function_privilege('resolveai_auth', 'app.claim_recovery_job()', 'execute'),
                  has_function_privilege('resolveai_api', 'app.claim_recovery_job()', 'execute'),
                  has_function_privilege('resolveai_worker', 'app.claim_recovery_job()', 'execute')
                """
            )
        ).one()
        assert privileges == (True, False, False, False, False, True)
        attributes = connection.execute(
            text(
                """
                SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
                FROM pg_roles
                WHERE rolname IN ('resolveai_api', 'resolveai_auth', 'resolveai_worker')
                ORDER BY rolname
                """
            )
        ).all()
        observed = [
            (row.rolname, row.rolsuper, row.rolbypassrls, row.rolcanlogin) for row in attributes
        ]
        assert observed == [
            ("resolveai_api", False, False, True),
            ("resolveai_auth", False, False, True),
            ("resolveai_worker", False, False, True),
        ]
    admin.dispose()
    auth = make_engine(settings.auth_database_url)
    with auth.connect() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT count(*) FROM app.user_account"))
            connection.commit()
        connection.rollback()
        email = connection.execute(
            text("SELECT email FROM app.lookup_login(:email)"),
            {"email": "  Support@demo.resolveai.test "},
        ).scalar_one()
        assert email == "support@demo.resolveai.test"
    auth.dispose()
    api = make_engine(settings.api_database_url)
    with api.connect() as connection:
        with pytest.raises(DBAPIError):
            connection.execute(text("SELECT * FROM app.claim_recovery_job()"))
            connection.commit()
        connection.rollback()
    api.dispose()


def test_concurrent_claims_do_not_share_a_live_lease(client, settings, manifest) -> None:
    del manifest
    proposal = _investigate_as(client, NORTHWIND_TICKET, support_headers(client))["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert (
        client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body).status_code
        == 200
    )
    assert (
        client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body).status_code
        == 200
    )
    barrier = threading.Barrier(2)
    found: list[object] = []

    def claim() -> None:
        engine = make_engine(settings.worker_database_url)
        factory = make_session_factory(engine)
        with factory() as session:
            barrier.wait(timeout=5)
            found.append(recovery._claim(session))
        engine.dispose()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sum(item is not None for item in found) == 1
    again = make_engine(settings.worker_database_url)
    with make_session_factory(again)() as session:
        assert recovery._claim(session) is None
    again.dispose()


def test_worker_restart_alternates_due_tenants(client, settings, manifest) -> None:
    del manifest
    north = _investigate_as(client, NORTHWIND_TICKET, support_headers(client))["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": north["revision"], "payload_hash": north["payload_hash"]}
    approved = client.post(f"/proposals/{north['id']}/approve", headers=approver, json=body)
    assert approved.status_code == 200, approved.text
    queued = client.post(f"/proposals/{north['id']}/execute", headers=approver, json=body)
    assert queued.status_code == 200, queued.text
    north_job = queued.json()["data"]["recovery_jobs"][-1]["id"]
    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate_as(client, HARBOR_TICKET, harbor_support)["proposals"][-1]
    harbor_approver = login(client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass")
    harbor_body = {"revision": harbor["revision"], "payload_hash": harbor["payload_hash"]}
    assert (
        client.post(
            f"/proposals/{harbor['id']}/approve", headers=harbor_approver, json=harbor_body
        ).status_code
        == 200
    )
    harbor_queued = client.post(
        f"/proposals/{harbor['id']}/execute", headers=harbor_approver, json=harbor_body
    )
    assert harbor_queued.status_code == 200, harbor_queued.text
    holder = make_engine(settings.worker_database_url)
    with make_session_factory(holder)() as session:
        claimed = recovery._claim(session)
    holder.dispose()
    assert claimed is not None
    assert str(claimed[0]) == north_job
    env = _worker_env(settings)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from resolveai.settings import load_settings\n"
            "from resolveai.worker.__main__ import drain\n"
            "raise SystemExit(drain(load_settings(), limit=1))",
        ],
        cwd=repo_root(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1, completed.stderr
    assert _refund_count(settings, HARBOR_PAYMENT) == 1
    assert _refund_count(settings, NORTHWIND_PAYMENT) == 0
    admin = make_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(
            text(
                "UPDATE app.recovery_job "
                "SET leased_until = clock_timestamp() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": north_job},
        )
    admin.dispose()
    restarted = subprocess.run(
        [
            sys.executable,
            "-c",
            "from resolveai.settings import load_settings\n"
            "from resolveai.worker.__main__ import drain\n"
            "raise SystemExit(drain(load_settings(), limit=1))",
        ],
        cwd=repo_root(),
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert restarted.returncode == 1, restarted.stderr
    assert _refund_count(settings, NORTHWIND_PAYMENT) == 1
    assert _refund_count(settings, HARBOR_PAYMENT) == 1
