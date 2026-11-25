"""M3.2 refund approval expiry and revocation. No paid model calls."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.support import approver_headers, bound_sessions, case_by_key, support_headers
from tests.test_auth_and_workflow import _refund_count
from tests.test_recovery import _job, _ledger_status, _operation_attempts, _prepare, _queue

from resolveai.services import recovery
from resolveai.settings import SettingsError, load_settings
from resolveai.worker.__main__ import drain


def _expire(settings, approval_id: str) -> None:
    from uuid import UUID

    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        result = connection.execute(
            text(
                "UPDATE app.approval SET expires_at = clock_timestamp() "
                "- interval '1 second' WHERE id = :id"
            ),
            {"id": UUID(approval_id)},
        )
        assert result.rowcount == 1, approval_id
    engine.dispose()


def _grant_id(payload: dict) -> str:
    approval = payload["proposals"][-1]["approval"]
    assert approval is not None
    return str(approval["id"])


def test_omitted_ttl_uses_the_development_default() -> None:
    settings = load_settings(environ={})
    assert settings.approval_ttl_seconds == 86400
    with pytest.raises(SettingsError):
        load_settings(environ={"APPROVAL_TTL_SECONDS": "0"})
    with pytest.raises(SettingsError):
        load_settings(environ={"APPROVAL_TTL_SECONDS": "2592001"})


def test_api_role_cannot_rewrite_approval_history(client, settings, manifest) -> None:
    from uuid import uuid4

    from resolveai.db.session import make_engine

    _case, _proposal, approver = _prepare(client, manifest)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    detail = client.get(f"/tickets/{ticket_id}", headers=approver)
    grant_id = _grant_id(detail.json()["data"])
    statements = [
        "UPDATE app.approval SET id = :new_id WHERE id = :id",
        "UPDATE app.approval SET decision = 'rejected' WHERE id = :id",
        "UPDATE app.approval SET expires_at = clock_timestamp() WHERE id = :id",
        "UPDATE app.approval SET actor_user_id = :new_id WHERE id = :id",
        "DELETE FROM app.approval WHERE id = :id",
        "DELETE FROM app.approval_revocation",
    ]
    for url in (settings.api_database_url, settings.worker_database_url):
        engine = make_engine(url)
        with engine.connect() as connection:
            for statement in statements:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": "11111111-1111-4111-8111-111111111111"},
                )
                with pytest.raises(DBAPIError):
                    connection.execute(
                        text(statement),
                        {"id": grant_id, "new_id": str(uuid4())},
                    )
                    connection.commit()
                connection.rollback()
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": "11111111-1111-4111-8111-111111111111"},
            )
            seen = connection.execute(
                text("SELECT id FROM app.approval WHERE id = :id"),
                {"id": grant_id},
            ).scalar_one()
            assert str(seen) == grant_id
        engine.dispose()


def test_expired_grant_without_a_job_can_be_approved_again(client, settings, manifest) -> None:
    _case, proposal, approver = _prepare(client, manifest)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    current = client.get(f"/tickets/{ticket_id}", headers=approver)
    assert current.status_code == 200, current.text
    first_id = _grant_id(current.json()["data"])
    _expire(settings, first_id)
    second = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
    assert second.status_code == 200, second.text
    assert _grant_id(second.json()["data"]) != first_id
    assert second.json()["data"]["proposals"][-1]["approval"]["label"] == "valid"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def test_blocked_pre_admission_grant_can_be_replaced_without_a_worker(
    client, settings, manifest
) -> None:
    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    detail = client.get(f"/tickets/{ticket_id}", headers=approver)
    grant_id = _grant_id(detail.json()["data"])
    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.recovery_job
                SET state = 'blocked', block_reason = 'authorization_stale',
                    owner_token = NULL, admitted_at = NULL
                WHERE id = :id
                """
            ),
            {"id": job_id},
        )
    engine.dispose()
    _expire(settings, grant_id)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    replaced = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
    assert replaced.status_code == 200, replaced.text
    assert _job(settings)["state"] == "cancelled"
    executed = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert executed.status_code == 200, executed.text
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def test_expiry_during_eligibility_blocks_admission(
    client, settings, manifest, monkeypatch
) -> None:
    proposal, _job_id = _queue(client, manifest)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    detail = client.get(f"/tickets/{ticket_id}", headers=approver_headers(client))
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    engine = make_engine(settings.worker_database_url)
    admin = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    with factory() as db:
        claimed = recovery._claim(db)
    with admin.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.approval
                SET expires_at = clock_timestamp() + interval '600 milliseconds'
                WHERE id = :id
                """
            ),
            {"id": _grant_id(detail.json()["data"])},
        )

    def slow(*_args, **_kwargs):
        time.sleep(1.2)
        return True

    monkeypatch.setattr(recovery, "_proposal_matches_live_state", slow)
    try:
        with factory() as db:
            try:
                recovery._admit(db, tools, *claimed)
            except recovery.GrantInvalid:
                db.rollback()
        assert _job(settings)["admitted"] is False
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        tools.close()
        engine.dispose()
        admin.dispose()


def test_expiry_during_audit_flush_blocks_admission(client, settings, manifest) -> None:
    import threading

    proposal, _job_id = _queue(client, manifest)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    detail = client.get(f"/tickets/{ticket_id}", headers=approver_headers(client))
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    engine = make_engine(settings.worker_database_url)
    admin = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    with factory() as db:
        claimed = recovery._claim(db)
    with admin.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.approval
                SET expires_at = clock_timestamp() + interval '2 seconds'
                WHERE id = :id
                """
            ),
            {"id": _grant_id(detail.json()["data"])},
        )
    blocker = admin.connect()
    blocker.execute(text("LOCK TABLE app.audit_event IN SHARE MODE"))
    outcome: dict[str, bool] = {}

    def admit() -> None:
        with factory() as db:
            try:
                recovery._admit(db, tools, *claimed)
                outcome["admitted"] = True
            except recovery.GrantInvalid:
                db.rollback()
                outcome["admitted"] = False

    thread = threading.Thread(target=admit)
    try:
        thread.start()
        expired = False
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as watch:
            for _ in range(250):
                waiting = watch.execute(
                    text(
                        """
                        SELECT count(*) FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND wait_event_type = 'Lock'
                          AND query ILIKE '%audit_event%'
                        """
                    )
                ).scalar_one()
                expired = bool(
                    watch.execute(
                        text(
                            "SELECT expires_at <= clock_timestamp() "
                            "FROM app.approval WHERE id = :id"
                        ),
                        {"id": _grant_id(detail.json()["data"])},
                    ).scalar()
                )
                if int(waiting) > 0 and expired:
                    break
                time.sleep(0.05)
        assert int(waiting) > 0 and expired
        blocker.rollback()
        thread.join(timeout=5)
        assert outcome["admitted"] is False
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        blocker.rollback()
        blocker.close()
        thread.join(timeout=5)
        tools.close()
        engine.dispose()
        admin.dispose()


def test_late_revoke_of_an_old_grant_leaves_the_replacement(
    client, settings, manifest
) -> None:
    _case, proposal, approver = _prepare(client, manifest)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    current = client.get(f"/tickets/{ticket_id}", headers=approver)
    first_id = _grant_id(current.json()["data"])
    _expire(settings, first_id)
    second = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
    assert second.status_code == 200, second.text
    second_id = _grant_id(second.json()["data"])
    queued = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert queued.status_code == 200, queued.text
    revoked = client.post(
        f"/approvals/{first_id}/revoke",
        headers=approver,
        json={"reason": "The first grant is historical."},
    )
    assert revoked.status_code == 200, revoked.text
    payload = revoked.json()["data"]
    assert payload["proposals"][-1]["status"] == "approved"
    assert payload["proposals"][-1]["approval"]["id"] == second_id
    assert payload["proposals"][-1]["approval"]["label"] == "valid"
    assert payload["recovery_jobs"][-1]["state"] == "queued"
    denied = client.post(
        f"/approvals/{second_id}/revoke",
        headers=support_headers(client),
        json={"reason": "Support cannot revoke."},
    )
    assert denied.status_code == 403
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def test_revocation_after_admission_keeps_the_confirmed_result(
    client, settings, manifest
) -> None:
    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
    drain(settings, limit=1)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    detail = client.get(f"/tickets/{ticket_id}", headers=approver)
    grant_id = _grant_id(detail.json()["data"])
    revoked = client.post(
        f"/approvals/{grant_id}/revoke",
        headers=approver,
        json={"reason": "Revoke after the refund was admitted."},
    )
    assert revoked.status_code == 200, revoked.text
    assert _job(settings)["state"] == "settled"
    assert _ledger_status(settings, job_id) == "confirmed"
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    repeated = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert repeated.status_code == 200, repeated.text
    reconciled = client.post(f"/actions/{proposal['action_id']}/reconcile", headers=approver)
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["data"]["actions"][0]["status"] == "confirmed"
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def test_unsent_uncertainty_can_become_a_confirmed_draft_after_revocation(
    client, settings, manifest, monkeypatch
) -> None:
    from resolveai.db.session import make_engine
    from resolveai.services import execution

    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE app.recovery_job SET max_attempts = 1 WHERE id = :id"),
            {"id": job_id},
        )
    engine.dispose()
    original = execution._verified_receipt
    monkeypatch.setattr(execution, "_verified_receipt", lambda *_a, **_k: ("unavailable", None))
    drain(settings, limit=1)
    monkeypatch.setattr(execution, "_verified_receipt", original)
    assert _job(settings)["state"] == "blocked"
    assert _job(settings)["admitted"] is True
    detail = client.get(f"/tickets/{ticket_id}", headers=approver)
    draft = detail.json()["data"]["replies"][-1]
    assert draft["status"] == "draft"
    assert draft["receipt"] is None
    revoked = client.post(
        f"/approvals/{_grant_id(detail.json()['data'])}/revoke",
        headers=approver,
        json={"reason": "Revoke after an uncertain admission."},
    )
    assert revoked.status_code == 200, revoked.text
    assert _job(settings)["state"] == "blocked"
    requeued = client.post(f"/recovery-jobs/{job_id}/requeue", headers=approver)
    assert requeued.status_code == 200, requeued.text
    drain(settings, limit=1)
    assert _job(settings)["state"] == "settled"
    assert _ledger_status(settings, job_id) == "confirmed"
    confirmed = client.get(f"/tickets/{ticket_id}", headers=approver)
    reply = confirmed.json()["data"]["replies"][-1]
    assert reply["status"] == "draft"
    assert "confirmed a refund" in reply["body"]
    assert reply["receipt"] is None
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def test_historical_revoke_does_not_reopen_an_admitted_replacement(
    client, settings, manifest
) -> None:
    case, proposal, approver = _prepare(client, manifest)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    current = client.get(f"/tickets/{case['ticket_id']}", headers=approver)
    first_id = _grant_id(current.json()["data"])
    _expire(settings, first_id)
    second = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
    assert second.status_code == 200, second.text
    second_id = _grant_id(second.json()["data"])
    queued = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert queued.status_code == 200, queued.text
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    engine = make_engine(settings.worker_database_url)
    tools = SimulatorClient(settings)
    try:
        with bound_sessions(engine)() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            assert recovery._admit(db, tools, *claimed) is True
        _expire(settings, second_id)
        revoked = client.post(
            f"/approvals/{first_id}/revoke",
            headers=approver,
            json={"reason": "Historical cleanup only."},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["data"]["proposals"][-1]["status"] == "approved"
        third = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
        assert third.status_code == 409
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        tools.close()
        engine.dispose()


def test_sent_reply_survives_grant_expiry_and_revocation(client, settings, manifest) -> None:
    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    drain(settings, limit=1)
    detail = client.get(f"/tickets/{ticket_id}", headers=approver)
    reply = detail.json()["data"]["replies"][-1]
    sent = client.post(
        f"/reply-drafts/{reply['id']}/approve-and-send",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert sent.status_code == 200, sent.text
    sent_reply = sent.json()["data"]["replies"][0]
    grant_id = _grant_id(sent.json()["data"])
    _expire(settings, grant_id)
    expired = client.get(f"/tickets/{ticket_id}", headers=approver)
    assert expired.json()["data"]["replies"][0]["body"] == sent_reply["body"]
    assert (
        expired.json()["data"]["replies"][0]["receipt"]["receipt_id"]
        == sent_reply["receipt"]["receipt_id"]
    )
    revoked = client.post(
        f"/approvals/{grant_id}/revoke",
        headers=approver,
        json={"reason": "Revoke after the reply was sent."},
    )
    assert revoked.status_code == 200, revoked.text
    payload = revoked.json()["data"]
    assert payload["replies"][0]["body"] == sent_reply["body"]
    assert payload["replies"][0]["receipt"]["receipt_id"] == sent_reply["receipt"]["receipt_id"]
    assert payload["actions"][0]["status"] == "confirmed"
    assert _ledger_status(settings, job_id) == "confirmed"
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def test_revocation_before_admission_cancels_the_job(client, settings, manifest) -> None:
    import threading
    from types import SimpleNamespace

    from resolveai.db.session import make_engine
    from resolveai.services.grants import revoke_approval
    from resolveai.tools.client import SimulatorClient

    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    admin = make_engine(settings.database_url)
    api_engine = make_engine(settings.api_database_url)
    factory = bound_sessions(engine)
    held = bound_sessions(api_engine)()
    tools = SimulatorClient(settings)
    thread = None
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            row = db.execute(
                text(
                    """
                    SELECT j.approval_id, j.tenant_id, a.actor_user_id
                    FROM app.recovery_job j
                    JOIN app.approval a ON a.id = j.approval_id
                    WHERE j.id = :id
                    """
                ),
                {"id": job_id},
            ).one()
        revoke_approval(
            held,
            actor=SimpleNamespace(
                role="approver", tenant_id=row.tenant_id, user_id=row.actor_user_id
            ),
            approval_id=row.approval_id,
            reason="Revocation wins before admission.",
        )

        def worker() -> None:
            with factory() as db:
                try:
                    recovery._admit(db, tools, *claimed)
                except recovery.OwnershipLost:
                    db.rollback()

        thread = threading.Thread(target=worker)
        thread.start()
        _wait_for_lock(admin)
        held.commit()
        thread.join(timeout=5)
        assert _job(settings)["state"] == "cancelled"
        assert _job(settings)["block_reason"] == "approval_revoked"
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        held.rollback()
        held.close()
        if thread is not None:
            thread.join(timeout=5)
        tools.close()
        engine.dispose()
        api_engine.dispose()
        admin.dispose()


def test_admission_before_revocation_can_still_be_confirmed(
    client, settings, manifest, monkeypatch
) -> None:
    import threading
    from types import SimpleNamespace
    from uuid import UUID

    from resolveai.db.models import RecoveryJob
    from resolveai.db.session import make_engine
    from resolveai.services.grants import revoke_approval
    from resolveai.tools.client import SimulatorClient

    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    admin = make_engine(settings.database_url)
    api_engine = make_engine(settings.api_database_url)
    factory = bound_sessions(engine)
    api_factory = bound_sessions(api_engine)
    tools = SimulatorClient(settings)
    with factory() as db:
        claimed = recovery._claim(db)
        row = db.execute(
            text(
                """
                SELECT j.approval_id, j.tenant_id, a.actor_user_id
                FROM app.recovery_job j
                JOIN app.approval a ON a.id = j.approval_id
                WHERE j.id = :id
                """
            ),
            {"id": job_id},
        ).one()
    ready = threading.Event()
    release = threading.Event()
    original = recovery._proposal_matches_live_state

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        ready.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(recovery, "_proposal_matches_live_state", pause)
    outcome: dict[str, bool] = {}

    def worker() -> None:
        with factory() as db:
            outcome["admitted"] = bool(recovery._admit(db, tools, *claimed))

    def revoker() -> None:
        with api_factory() as db:
            revoke_approval(
                db,
                actor=SimpleNamespace(
                    role="approver", tenant_id=row.tenant_id, user_id=row.actor_user_id
                ),
                approval_id=row.approval_id,
                reason="Admission wins; keep the original effect.",
            )
            db.commit()
            outcome["revoked"] = True

    worker_thread = threading.Thread(target=worker)
    revoke_thread = threading.Thread(target=revoker)
    try:
        worker_thread.start()
        assert ready.wait(5)
        revoke_thread.start()
        _wait_for_lock(admin)
        release.set()
        worker_thread.join(timeout=5)
        revoke_thread.join(timeout=5)
        assert outcome.get("admitted") is True
        assert outcome.get("revoked") is True
        with factory() as db:
            job = db.get(RecoveryJob, UUID(job_id))
            assert job is not None
            command = dict(job.command)
            tools.create_refund(
                account_id=UUID(command["account_id"]),
                payment_id=UUID(command["payment_id"]),
                event_id=str(command["event_id"]),
                amount_cents=int(command["amount_cents"]),
                action_id=str(command["action_id"]),
                payload_hash=str(command["payload_hash"]),
                expected_payment_version=int(command["expected_payment_version"]),
                policy_version=str(command["policy_version"]),
            )
            recovery._settle_or_retry(db, tools, *claimed)
        assert _job(settings)["state"] == "settled"
        assert _refund_count(settings, proposal["target_payment_id"]) == 1
        assert _operation_attempts(settings, proposal["action_id"]) == 1
    finally:
        release.set()
        worker_thread.join(timeout=5)
        revoke_thread.join(timeout=5)
        tools.close()
        engine.dispose()
        api_engine.dispose()
        admin.dispose()


def test_new_worker_queries_an_admitted_command_after_the_grant_expires(
    client, settings, manifest, tmp_path
) -> None:
    import os
    import signal
    import subprocess
    import sys
    from pathlib import Path

    from tests.test_recovery import _operation_attempts, _worker_env

    proposal, _job_id = _queue(client, manifest)
    approver = approver_headers(client)
    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    held = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(
            settings,
            RESOLVEAI_RECOVERY_HOLD="before_local_confirm",
            RESOLVEAI_RECOVERY_READY=str(ready),
            RESOLVEAI_RECOVERY_RELEASE=str(release),
        ),
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    for _ in range(200):
        if ready.exists() or held.poll() is not None:
            break
        time.sleep(0.05)
    assert ready.exists()
    os.kill(held.pid, signal.SIGKILL)
    held.wait(timeout=5)
    assert _refund_count(settings, proposal["target_payment_id"]) == 1
    detail = client.get(f"/tickets/{ticket_id}", headers=approver)
    _expire(settings, _grant_id(detail.json()["data"]))
    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app.recovery_job SET leased_until = "
                "clock_timestamp() - interval '1 second'"
            )
        )
    engine.dispose()
    restarted = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(settings),
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        for _ in range(200):
            if _job(settings)["state"] == "settled":
                break
            time.sleep(0.05)
        assert _job(settings)["state"] == "settled"
        assert _refund_count(settings, proposal["target_payment_id"]) == 1
        assert _operation_attempts(settings, proposal["action_id"]) == 1
    finally:
        if restarted.poll() is None:
            os.kill(restarted.pid, signal.SIGTERM)
            restarted.wait(timeout=5)


def _wait_for_lock(engine) -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        for _ in range(100):
            waiting = connection.execute(
                text(
                    """
                    SELECT count(*) FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND wait_event_type = 'Lock'
                    """
                )
            ).scalar_one()
            if int(waiting) > 0:
                return
            time.sleep(0.02)
    raise AssertionError("the overlapping transaction did not wait on the row lock")
