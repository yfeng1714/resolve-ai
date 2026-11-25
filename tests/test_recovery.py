"""M3.1 refund recovery. These tests do not call a paid model."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.support import (
    approver_headers,
    bound_sessions,
    case_by_key,
    seeing_engine,
    settle_recovery,
    support_headers,
)
from tests.test_auth_and_workflow import _investigate, _refund_count

from resolveai.services import recovery
from resolveai.worker.__main__ import drain


def _execute(client, proposal: dict, headers: dict[str, str]):
    return client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=headers,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )


def _job(settings) -> dict:
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT state, block_reason, admitted_at IS NOT NULL AS admitted,
                       command_generation
                FROM app.recovery_job
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        ).mappings().one()
    engine.dispose()
    return dict(row)


def _worker_env(settings, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": "src",
            "DATABASE_URL": settings.database_url,
            "API_DATABASE_URL": settings.api_database_url,
            "AUTH_DATABASE_URL": settings.auth_database_url,
            "WORKER_DATABASE_URL": settings.worker_database_url,
            "SIMULATOR_DATABASE_URL": settings.simulator_database_url,
            "ORACLE_DATABASE_URL": settings.oracle_database_url,
            "SIMULATOR_BASE_URL": settings.simulator_base_url,
            "SIMULATOR_TOKEN": settings.simulator_token,
            "MODEL_PROVIDER": "fake",
            "MODEL_NAME": "",
            "LLM_BUDGET_USD": "0",
        }
    )
    env.update(extra)
    return env


def test_withdrawal_before_claim_allows_a_fresh_generation(client, settings, manifest) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    approver = approver_headers(client)
    assert (
        client.post(
            f"/proposals/{proposal['id']}/approve",
            headers=approver,
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        ).status_code
        == 200
    )
    queued = _execute(client, proposal, approver)
    assert queued.status_code == 200, queued.text
    assert queued.json()["data"]["recovery_jobs"][-1]["state"] == "queued"
    withdrawn = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=support_headers(client),
        json={"body": "The customer withdrew the refund request."},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert _job(settings)["state"] == "cancelled"
    drain(settings)
    assert _refund_count(settings, proposal["target_payment_id"]) == 0
    fresh = _investigate(client, case["ticket_id"])["proposals"][-1]
    approver = approver_headers(client)
    assert (
        client.post(
            f"/proposals/{fresh['id']}/approve",
            headers=approver,
            json={"revision": fresh["revision"], "payload_hash": fresh["payload_hash"]},
        ).status_code
        == 200
    )
    assert _execute(client, fresh, approver).status_code == 200
    settle_recovery(settings)
    assert _refund_count(settings, fresh["target_payment_id"]) == 1
    assert _job(settings)["command_generation"] == 2
    assert _job(settings)["state"] == "settled"


def test_kill_after_admission_queries_without_a_second_create(
    client, settings, manifest, tmp_path: Path
) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    approver = approver_headers(client)
    assert (
        client.post(
            f"/proposals/{proposal['id']}/approve",
            headers=approver,
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        ).status_code
        == 200
    )
    assert _execute(client, proposal, approver).status_code == 200
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    env = _worker_env(
        settings,
        RESOLVEAI_RECOVERY_HOLD="before_post",
        RESOLVEAI_RECOVERY_READY=str(ready),
        RESOLVEAI_RECOVERY_RELEASE=str(release),
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=env,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    for _ in range(100):
        if ready.exists() or process.poll() is not None:
            break
        time_sleep()
    assert ready.exists()
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=5)
    assert _refund_count(settings, proposal["target_payment_id"]) == 0
    assert _job(settings)["admitted"] is True
    _expire_lease(settings)
    recovery.BACKOFF_SECONDS = (0, 0, 0, 0, 0)
    try:
        drain(settings)
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
        assert _job(settings)["state"] == "blocked"
        assert _job(settings)["block_reason"] == "delivery_uncertain"
        requeued = client.post(
            f"/recovery-jobs/{_job_id(settings)}/requeue",
            headers=approver,
        )
        assert requeued.status_code == 200, requeued.text
        drain(settings)
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        recovery.BACKOFF_SECONDS = (1, 2, 4, 8, 16)


def test_settled_job_with_unconfirmed_ledger_is_not_success(client, settings, manifest) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    approver = approver_headers(client)
    assert (
        client.post(
            f"/proposals/{proposal['id']}/approve",
            headers=approver,
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        ).status_code
        == 200
    )
    assert _execute(client, proposal, approver).status_code == 200
    settle_recovery(settings)
    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE app.action_ledger SET status = 'pending'"))
    engine.dispose()
    report = client.get(f"/recovery-jobs/{_job_id(settings)}/consistency", headers=approver)
    assert report.status_code == 409
    assert report.json()["error_code"] == "inconsistent_recovery"
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def _job_id(settings) -> str:
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT id FROM app.recovery_job ORDER BY created_at DESC LIMIT 1")
        ).scalar_one()
    engine.dispose()
    return str(value)


def _expire_lease(settings) -> None:
    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app.recovery_job "
                "SET leased_until = clock_timestamp() - interval '1 second'"
            )
        )
    engine.dispose()


def test_lease_expiry_during_admission_rolls_back(client, settings, manifest, monkeypatch) -> None:
    import time

    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    _proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    original = recovery._proposal_matches_live_state

    def slow_check(*args, **kwargs):
        time.sleep(0.6)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery, "_proposal_matches_live_state", slow_check)
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            jid, token, generation = claimed
            with engine.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": "11111111-1111-4111-8111-111111111111"},
                )
                connection.execute(
                    text(
                        "UPDATE app.recovery_job SET leased_until = "
                        "clock_timestamp() + interval '300 milliseconds' WHERE id = :jid"
                    ),
                    {"jid": jid},
                )
            try:
                admitted = recovery._admit(db, tools, jid, token, generation)
            except recovery.OwnershipLost:
                db.rollback()
                admitted = False
        assert admitted is False
        row = _job(settings)
        assert row["admitted"] is False
        assert _dispatch_state(settings, job_id) == "reserved"
    finally:
        tools.close()
        engine.dispose()


def test_worker_role_cannot_read_the_simulator(settings) -> None:
    from sqlalchemy.exc import DBAPIError

    from resolveai.db.session import make_engine

    for url, expected in (
        (settings.api_database_url, "resolveai_api"),
        (settings.worker_database_url, "resolveai_worker"),
    ):
        engine = make_engine(url)
        with engine.connect() as connection:
            role, superuser, bypass = connection.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).one()
            assert role == expected
            assert superuser is False
            assert bypass is False
            with pytest.raises(DBAPIError):
                connection.execute(text("SELECT count(*) FROM simulator.refund"))
                connection.commit()
            connection.rollback()
            with pytest.raises(DBAPIError):
                connection.execute(text("CREATE TABLE app.recovery_not_allowed (id int)"))
                connection.commit()
            connection.rollback()
        engine.dispose()


def _queue(client, manifest):
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert client.post(
        f"/proposals/{proposal['id']}/approve", headers=approver, json=body
    ).status_code == 200
    queued = _execute(client, proposal, approver)
    assert queued.status_code == 200, queued.text
    return proposal, queued.json()["data"]["recovery_jobs"][-1]["id"]


def _dispatch_state(settings, job_id: str) -> str:
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT l.dispatch_state
                FROM app.recovery_job j
                JOIN app.action_ledger l ON l.id = j.ledger_id
                WHERE j.id = :job_id
                """
            ),
            {"job_id": job_id},
        ).scalar_one()
    engine.dispose()
    return str(value)


def time_sleep() -> None:
    time.sleep(0.05)


def test_restarted_worker_confirms_a_committed_simulator_refund(
    client, settings, manifest, tmp_path: Path
) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert client.post(
        f"/proposals/{proposal['id']}/approve", headers=approver, json=body
    ).status_code == 200
    assert _execute(client, proposal, approver).status_code == 200
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
        time_sleep()
    assert ready.exists()
    os.kill(held.pid, signal.SIGKILL)
    held.wait(timeout=5)
    assert _refund_count(settings, proposal["target_payment_id"]) == 1
    assert _job(settings)["state"] != "settled"
    _expire_lease(settings)
    restarted = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(settings),
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        for _ in range(200):
            if _job(settings)["state"] == "settled":
                break
            time_sleep()
        assert _job(settings)["state"] == "settled"
        assert _refund_count(settings, proposal["target_payment_id"]) == 1
    finally:
        os.kill(restarted.pid, signal.SIGTERM)
        restarted.wait(timeout=5)


def test_pre_admission_outage_does_not_kill_the_worker(client, settings, manifest) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert client.post(
        f"/proposals/{proposal['id']}/approve", headers=approver, json=body
    ).status_code == 200
    assert _execute(client, proposal, approver).status_code == 200
    stalled = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(settings, SIMULATOR_BASE_URL="http://127.0.0.1:9"),
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    try:
        time.sleep(1.2)
        assert stalled.poll() is None
        assert _job(settings)["admitted"] is False
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        os.kill(stalled.pid, signal.SIGTERM)
        stalled.wait(timeout=5)


def test_job_cannot_adopt_another_tenants_ledger(client, settings, manifest) -> None:
    from uuid import uuid4

    from sqlalchemy.exc import IntegrityError

    from resolveai.db.session import make_engine

    _queue(client, manifest)
    other = str(uuid4())
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO app.tenant (id, slug, name) VALUES (:id, :slug, 'Other')"
            ),
            {"id": other, "slug": f"other-{other[:8]}"},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE app.recovery_job SET tenant_id = :tenant_id"),
                {"tenant_id": other},
            )
    engine.dispose()


def _ledger_status(settings, job_id: str) -> str:
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT l.status
                FROM app.recovery_job j
                JOIN app.action_ledger l ON l.id = j.ledger_id
                WHERE j.id = :job_id
                """
            ),
            {"job_id": job_id},
        ).scalar_one()
    engine.dispose()
    return str(value)


def _operation_attempts(settings, action_id: str) -> int:
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        value = connection.execute(
            text(
                """
                SELECT write_attempts
                FROM simulator.operation
                WHERE action_id = :action_id
                """
            ),
            {"action_id": action_id},
        ).scalar()
    engine.dispose()
    return int(value or 0)


def _attempts(settings, job_id: str) -> tuple[int, float]:
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        count, delay = connection.execute(
            text(
                """
                SELECT attempt_count,
                       EXTRACT(EPOCH FROM (next_attempt_at - clock_timestamp()))
                FROM app.recovery_job
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        ).one()
    engine.dispose()
    return int(count), float(delay)


def test_lease_expiry_during_settlement_rolls_back(
    client, settings, manifest, monkeypatch
) -> None:
    import time

    from resolveai.db.session import make_engine
    from resolveai.services import execution
    from resolveai.tools.client import SimulatorClient

    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)

    def slow_receipt(*_args, **_kwargs):
        time.sleep(0.6)
        return "unavailable", None

    monkeypatch.setattr(execution, "_verified_receipt", slow_receipt)
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            jid, token, generation = claimed
            assert recovery._admit(db, tools, jid, token, generation) is True
            with engine.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": "11111111-1111-4111-8111-111111111111"},
                )
                connection.execute(
                    text(
                        "UPDATE app.recovery_job SET leased_until = "
                        "clock_timestamp() + interval '300 milliseconds' WHERE id = :jid"
                    ),
                    {"jid": jid},
                )
            try:
                recovery._settle_or_retry(db, tools, jid, token, generation)
            except recovery.OwnershipLost:
                db.rollback()
        row = _job(settings)
        assert row["state"] == "leased"
        assert row["admitted"] is True
        assert _ledger_status(settings, job_id) == "pending"
        assert _attempts(settings, job_id)[0] == 0
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        tools.close()
        engine.dispose()


def test_expired_owner_cannot_write_without_reassignment(
    client, settings, manifest
) -> None:
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    _proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            jid, token, generation = claimed
        _expire_lease(settings)
        with factory() as db:
            try:
                recovery._admit(db, tools, jid, token, generation)
                admitted = True
            except recovery.OwnershipLost:
                db.rollback()
                admitted = False
        assert admitted is False
        row = _job(settings)
        assert row["state"] == "leased"
        assert row["admitted"] is False
        assert _dispatch_state(settings, job_id) == "reserved"
    finally:
        tools.close()
        engine.dispose()


def test_old_owner_loses_after_reassignment(client, settings, manifest) -> None:
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    _proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    try:
        with factory() as db:
            first = recovery._claim(db)
            assert first is not None
        _expire_lease(settings)
        with factory() as db:
            second = recovery._claim(db)
            assert second is not None
        assert second[2] == first[2] + 1
        with factory() as db:
            try:
                recovery._admit(db, tools, first[0], first[1], first[2])
                admitted = True
            except recovery.OwnershipLost:
                db.rollback()
                admitted = False
        assert admitted is False
        assert _job(settings)["admitted"] is False
        assert _dispatch_state(settings, job_id) == "reserved"
        with factory() as db:
            owner = db.execute(
                text("SELECT owner_token, owner_generation FROM app.recovery_job WHERE id = :id"),
                {"id": second[0]},
            ).one()
        assert str(owner.owner_token) == str(second[1])
        assert int(owner.owner_generation) == second[2]
    finally:
        tools.close()
        engine.dispose()


def test_one_receipt_settles_when_a_later_read_would_fail(
    client, settings, manifest, monkeypatch
) -> None:
    from resolveai.db.session import make_engine
    from resolveai.services import execution
    from resolveai.tools.client import SimulatorClient

    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    calls = {"count": 0}
    original = execution._verified_receipt

    def once(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            return "unavailable", None
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "_verified_receipt", once)
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            jid, token, generation = claimed
            assert recovery._admit(db, tools, jid, token, generation) is True
            from resolveai.db.models import RecoveryJob

            job = db.get(RecoveryJob, jid)
            assert job is not None
            command = dict(job.command)
            from uuid import UUID

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
            recovery._settle_or_retry(db, tools, jid, token, generation)
        assert calls["count"] == 1
        assert _job(settings)["state"] == "settled"
        assert _ledger_status(settings, job_id) == "confirmed"
        assert _refund_count(settings, proposal["target_payment_id"]) == 1
        assert _operation_attempts(settings, proposal["action_id"]) == 1
    finally:
        tools.close()
        engine.dispose()


def test_failed_verification_retries_then_blocks(client, settings, manifest, monkeypatch) -> None:
    from resolveai.db.session import make_engine
    from resolveai.services import execution
    from resolveai.tools.client import SimulatorClient

    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    monkeypatch.setattr(execution, "_verified_receipt", lambda *_a, **_k: ("unavailable", None))
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            jid, token, generation = claimed
            assert recovery._admit(db, tools, jid, token, generation) is True
            recovery._settle_or_retry(db, tools, jid, token, generation)
        assert _job(settings)["state"] == "queued"
        attempts, delay = _attempts(settings, job_id)
        assert attempts == 1
        assert 0 < delay <= 1.5
        assert _ledger_status(settings, job_id) == "pending"
        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": "11111111-1111-4111-8111-111111111111"},
            )
            connection.execute(
                text(
                    """
                    UPDATE app.recovery_job
                    SET attempt_count = max_attempts - 1,
                        next_attempt_at = clock_timestamp()
                    WHERE id = :jid
                    """
                ),
                {"jid": jid},
            )
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            recovery._settle_or_retry(db, tools, *claimed)
        row = _job(settings)
        assert row["state"] == "blocked"
        assert row["block_reason"] == "delivery_uncertain"
        assert _ledger_status(settings, job_id) == "unknown"
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
        assert _operation_attempts(settings, proposal["action_id"]) == 0
    finally:
        tools.close()
        engine.dispose()


def test_conflicting_receipt_blocks_without_settling(
    client, settings, manifest, monkeypatch
) -> None:
    from resolveai.db.session import make_engine
    from resolveai.services import execution
    from resolveai.tools.client import SimulatorClient

    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    monkeypatch.setattr(execution, "_verified_receipt", lambda *_a, **_k: ("conflict", None))
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
            assert recovery._admit(db, tools, *claimed) is True
            recovery._settle_or_retry(db, tools, *claimed)
        row = _job(settings)
        assert row["state"] == "blocked"
        assert row["block_reason"] == "conflict"
        assert _ledger_status(settings, job_id) == "unknown"
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
    finally:
        tools.close()
        engine.dispose()


def test_pre_admission_cancellation_covers_expired_and_blocked_jobs(
    client, settings, manifest, monkeypatch
) -> None:
    from resolveai.db.session import make_engine

    case = case_by_key(manifest, "duplicate-approve")
    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(engine)
    with factory() as db:
        assert recovery._claim(db) is not None
    _expire_lease(settings)
    withdrawn = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=support_headers(client),
        json={"body": "Withdraw the queued refund before dispatch."},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert _job(settings)["state"] == "cancelled"
    assert _job(settings)["admitted"] is False
    drain(settings)
    assert _refund_count(settings, proposal["target_payment_id"]) == 0
    engine.dispose()

    fresh, blocked_id = _queue(client, manifest)
    monkeypatch.setattr(recovery, "_proposal_matches_live_state", lambda *_a, **_k: False)
    drain(settings)
    assert _job(settings)["state"] == "blocked"
    assert _job(settings)["block_reason"] == "authorization_stale"
    assert _job(settings)["admitted"] is False
    withdrawn = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=support_headers(client),
        json={"body": "Withdraw the blocked refund before dispatch."},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert _job(settings)["state"] == "cancelled"
    assert blocked_id
    drain(settings)
    assert _refund_count(settings, fresh["target_payment_id"]) == 0


def test_cancellation_and_admission_share_one_winner(client, settings, manifest) -> None:
    import threading
    from uuid import UUID

    from sqlalchemy import select

    from resolveai.db.models import Ticket
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    case = case_by_key(manifest, "duplicate-approve")
    _proposal, _job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    watcher = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    ticket_id = UUID(case["ticket_id"])
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
        held = factory()
        ticket = held.scalar(
            select(Ticket).where(Ticket.id == ticket_id).with_for_update()
        )
        assert ticket is not None
        recovery.cancel_pre_admission(held, ticket)
        outcome: dict[str, bool] = {}

        def admit() -> None:
            with factory() as db:
                try:
                    outcome["admitted"] = recovery._admit(db, tools, *claimed)
                except recovery.OwnershipLost:
                    db.rollback()
                    outcome["admitted"] = False

        worker = threading.Thread(target=admit)
        worker.start()
        _wait_for_lock(watcher)
        held.commit()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert outcome["admitted"] is False
        assert _job(settings)["state"] == "cancelled"
        assert _job(settings)["admitted"] is False
    finally:
        held.close()
        tools.close()
        engine.dispose()
        watcher.dispose()


def test_admission_wins_when_it_commits_before_cancellation(
    client, settings, manifest
) -> None:
    import threading
    from uuid import UUID

    from sqlalchemy import select

    from resolveai.db.models import Ticket
    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    case = case_by_key(manifest, "duplicate-approve")
    proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    watcher = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    ticket_id = UUID(case["ticket_id"])
    release = threading.Event()
    inside = threading.Event()

    def slow_check(*_args, **_kwargs):
        inside.set()
        assert release.wait(timeout=5)
        return True

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(recovery, "_proposal_matches_live_state", slow_check)
    try:
        with factory() as db:
            claimed = recovery._claim(db)
            assert claimed is not None
        outcome = {}

        def admit() -> None:
            with factory() as db:
                outcome["admitted"] = recovery._admit(db, tools, *claimed)

        worker = threading.Thread(target=admit)
        worker.start()
        assert inside.wait(timeout=5)
        cancelled = threading.Event()

        def cancel() -> None:
            with factory() as db:
                ticket = db.scalar(
                    select(Ticket).where(Ticket.id == ticket_id).with_for_update()
                )
                assert ticket is not None
                recovery.cancel_pre_admission(db, ticket)
                db.commit()
            cancelled.set()

        canceller = threading.Thread(target=cancel)
        canceller.start()
        _wait_for_lock(watcher)
        release.set()
        worker.join(timeout=5)
        canceller.join(timeout=5)
        assert outcome["admitted"] is True
        assert cancelled.is_set()
        row = _job(settings)
        assert row["admitted"] is True
        assert row["state"] == "leased"
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
        assert job_id
    finally:
        release.set()
        monkeypatch.undo()
        tools.close()
        engine.dispose()
        watcher.dispose()


def _start_worker(settings) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(settings),
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def _stop_worker(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.kill(process.pid, signal.SIGTERM)
        process.wait(timeout=5)


def _wait_for_job(settings, state: str) -> bool:
    for _ in range(200):
        if _job(settings)["state"] == state:
            return True
        time_sleep()
    return False


def _wait_for_lock(engine) -> None:
    import time

    with engine.connect() as connection:
        for _ in range(50):
            waiting = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                      AND wait_event_type = 'Lock'
                    """
                )
            ).scalar_one()
            if int(waiting) > 0:
                return
            time.sleep(0.05)
    raise AssertionError("the overlapping transaction did not wait on the row lock")


def test_requeue_and_a_new_worker_do_not_post_again(
    client, settings, manifest, tmp_path: Path
) -> None:
    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    held = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(
            settings,
            RESOLVEAI_RECOVERY_HOLD="before_post",
            RESOLVEAI_RECOVERY_READY=str(ready),
            RESOLVEAI_RECOVERY_RELEASE=str(release),
        ),
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    for _ in range(200):
        if ready.exists() or held.poll() is not None:
            break
        time_sleep()
    assert ready.exists()
    os.kill(held.pid, signal.SIGKILL)
    held.wait(timeout=5)
    assert _job(settings)["admitted"] is True
    assert _operation_attempts(settings, proposal["action_id"]) == 0
    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        command = connection.execute(
            text("SELECT command::text FROM app.recovery_job WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                UPDATE app.recovery_job
                SET leased_until = clock_timestamp() - interval '1 second',
                    max_attempts = 1
                WHERE id = :id
                """
            ),
            {"id": job_id},
        )
    engine.dispose()
    first = _start_worker(settings)
    try:
        assert _wait_for_job(settings, "blocked")
    finally:
        _stop_worker(first)
    assert _job(settings)["block_reason"] == "delivery_uncertain"
    assert _operation_attempts(settings, proposal["action_id"]) == 0
    requeued = client.post(f"/recovery-jobs/{job_id}/requeue", headers=approver)
    assert requeued.status_code == 200, requeued.text
    assert _job(settings)["state"] == "queued"
    engine = seeing_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        again = connection.execute(
            text("SELECT command::text FROM app.recovery_job WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
    engine.dispose()
    assert again == command
    restarted = _start_worker(settings)
    try:
        assert _wait_for_job(settings, "blocked")
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
        assert _operation_attempts(settings, proposal["action_id"]) == 0
    finally:
        _stop_worker(restarted)


def _prepare(client, manifest):
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    approved = client.post(
        f"/proposals/{proposal['id']}/approve", headers=approver, json=body
    )
    assert approved.status_code == 200, approved.text
    return case, proposal, approver


def _mark_latest_job(settings, state: str) -> None:
    from resolveai.db.session import make_engine

    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE app.recovery_job SET state = :state"),
            {"state": state},
        )
    engine.dispose()


def test_expiry_during_audit_flush_does_not_admit(client, settings, manifest) -> None:
    import threading

    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    _proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    admin = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    tools = SimulatorClient(settings)
    with factory() as db:
        claimed = recovery._claim(db)
    with admin.begin() as connection:
        connection.execute(
            text(
                "UPDATE app.recovery_job SET leased_until = "
                "clock_timestamp() + interval '2 seconds' WHERE id = :id"
            ),
            {"id": job_id},
        )
    blocker = admin.connect()
    blocker.execute(text("LOCK TABLE app.audit_event IN SHARE MODE"))
    outcome: dict[str, object] = {}

    def admit() -> None:
        with factory() as db:
            try:
                outcome["returned"] = recovery._admit(db, tools, *claimed)
            except recovery.OwnershipLost:
                db.rollback()
                outcome["returned"] = False

    thread = threading.Thread(target=admit)
    try:
        thread.start()
        waited = False
        expired = False
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as watch:
            for _ in range(250):
                pid = watch.execute(
                    text(
                        """
                        SELECT pid FROM pg_stat_activity
                        WHERE datname = current_database()
                          AND wait_event_type = 'Lock'
                          AND query ILIKE '%audit_event%'
                        """
                    )
                ).scalar()
                expired = bool(
                    watch.execute(
                        text(
                            "SELECT leased_until <= clock_timestamp() "
                            "FROM app.recovery_job WHERE id = :id"
                        ),
                        {"id": job_id},
                    ).scalar()
                )
                if pid is not None and expired:
                    waited = True
                    break
                time_sleep()
        assert waited and expired
        blocker.rollback()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert outcome["returned"] is False
        assert _job(settings)["admitted"] is False
        assert _dispatch_state(settings, job_id) == "reserved"
    finally:
        blocker.rollback()
        blocker.close()
        thread.join(timeout=5)
        tools.close()
        engine.dispose()
        admin.dispose()


def test_reconcile_and_repeat_execute_keep_a_confirmed_receipt(
    client, settings, manifest, monkeypatch
) -> None:
    from resolveai.services import execution

    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
    drain(settings, limit=1)
    monkeypatch.setattr(execution, "_verified_receipt", lambda *_a, **_k: ("unavailable", None))
    reconciled = client.post(f"/actions/{proposal['action_id']}/reconcile", headers=approver)
    assert reconciled.status_code == 200, reconciled.text
    assert _job(settings)["state"] == "settled"
    assert _ledger_status(settings, job_id) == "confirmed"
    detail = reconciled.json()["data"]
    reply = detail["replies"][-1]
    sent = client.post(
        f"/reply-drafts/{reply['id']}/approve-and-send",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert sent.status_code == 200, sent.text
    sent_reply = sent.json()["data"]["replies"][0]
    repeated = _execute(client, proposal, approver)
    assert repeated.status_code == 200, repeated.text
    again = client.post(f"/actions/{proposal['action_id']}/reconcile", headers=approver)
    assert again.status_code == 200, again.text
    payload = again.json()["data"]
    assert payload["ticket"]["status"] == "reply_sent"
    assert payload["replies"][0]["status"] == "sent"
    assert payload["replies"][0]["body"] == sent_reply["body"]
    assert payload["replies"][0]["receipt"]["receipt_id"] == sent_reply["receipt"]["receipt_id"]
    assert payload["actions"][0]["status"] == "confirmed"
    assert _job(settings)["state"] == "settled"
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def test_reconcile_does_not_take_a_worker_owned_refund(client, settings, manifest) -> None:
    proposal, job_id = _queue(client, manifest)
    refused = client.post(
        f"/actions/{proposal['action_id']}/reconcile",
        headers=approver_headers(client),
    )
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "stale_proposal"
    assert _job(settings)["state"] == "queued"
    assert _job(settings)["admitted"] is False
    assert _ledger_status(settings, job_id) == "pending"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def test_fast_worker_completion_is_returned(client, settings, manifest, monkeypatch) -> None:
    from resolveai.services import execution

    _case, proposal, approver = _prepare(client, manifest)
    monkeypatch.setattr(execution, "_before_dispatch", lambda: drain(settings, limit=1))
    result = _execute(client, proposal, approver)
    assert result.status_code == 200, result.text
    payload = result.json()["data"]
    assert payload["recovery_jobs"][-1]["state"] == "settled"
    assert payload["actions"][0]["status"] == "confirmed"
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


@pytest.mark.parametrize(
    ("state", "status_code"),
    [("leased", 200), ("blocked", 200), ("cancelled", 409)],
)
def test_enqueue_response_reports_the_worker_state(
    client, settings, manifest, monkeypatch, state: str, status_code: int
) -> None:
    from resolveai.services import execution

    _case, proposal, approver = _prepare(client, manifest)
    monkeypatch.setattr(
        execution, "_before_dispatch", lambda: _mark_latest_job(settings, state)
    )
    result = _execute(client, proposal, approver)
    assert result.status_code == status_code, result.text
    if status_code == 200:
        assert result.json()["data"]["recovery_jobs"][-1]["state"] == state
    else:
        assert result.json()["error_code"] == "stale_proposal"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def test_requeue_locks_the_ticket_before_the_job(client, settings, manifest) -> None:
    import threading
    from types import SimpleNamespace
    from uuid import UUID

    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from resolveai.db.models import RecoveryJob, Ticket
    from resolveai.db.session import make_engine
    from resolveai.errors import APIError

    _proposal, job_id = _queue(client, manifest)
    engine = make_engine(settings.worker_database_url)
    watcher = make_engine(settings.database_url)
    factory = bound_sessions(engine)
    facts: dict[str, object] = {}
    ready = threading.Event()
    with factory() as db:
        claimed = recovery._claim(db)
        job = db.get(RecoveryJob, UUID(job_id))
        assert job is not None
        tenant_id, authorizer = job.tenant_id, job.authorizer_user_id
    held = factory()
    thread = None
    try:
        held.execute(text("SET LOCAL lock_timeout = '5s'"))
        ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
        held.scalar(select(Ticket).where(Ticket.id == UUID(ticket_id)).with_for_update())

        def requeue() -> None:
            with factory() as db:
                try:
                    db.execute(text("SET LOCAL lock_timeout = '5s'"))
                    ready.set()
                    recovery.requeue_queries(
                        db,
                        actor=SimpleNamespace(
                            role="approver", tenant_id=tenant_id, user_id=authorizer
                        ),
                        job_id=UUID(job_id),
                    )
                    db.commit()
                    facts["requeue"] = "returned"
                except APIError as exc:
                    db.rollback()
                    facts["requeue"] = exc.error_code
                except OperationalError as exc:
                    db.rollback()
                    facts["requeue"] = getattr(exc.orig, "sqlstate", "db")

        thread = threading.Thread(target=requeue)
        thread.start()
        assert ready.wait(3)
        _wait_for_lock(watcher)
        recovery._fence(held, *claimed)
        held.rollback()
        thread.join(timeout=7)
        assert facts["requeue"] == "stale_proposal"
    finally:
        held.rollback()
        held.close()
        if thread is not None:
            thread.join(timeout=7)
        watcher.dispose()
        engine.dispose()


def test_in_flight_post_is_queried_by_the_next_owner(
    client, settings, manifest, monkeypatch
) -> None:
    import threading

    from resolveai.db.session import make_engine
    from resolveai.tools.client import SimulatorClient

    proposal, _job_id = _queue(client, manifest)
    started = threading.Event()
    release = threading.Event()
    calls = {"n": 0}
    original = SimulatorClient.create_refund

    def slow(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            assert release.wait(timeout=5)
        return original(self, **kwargs)

    monkeypatch.setattr(SimulatorClient, "create_refund", slow)
    outcome: dict[str, object] = {}

    def first() -> None:
        try:
            drain(settings, limit=1)
            outcome["done"] = True
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert started.wait(timeout=5)
        _expire_lease(settings)
        drain(settings, limit=1)
        assert calls["n"] == 1
        assert _job(settings)["state"] == "queued"
        assert _refund_count(settings, proposal["target_payment_id"]) == 0
        release.set()
        thread.join(timeout=5)
        assert outcome.get("done") is True
        assert _refund_count(settings, proposal["target_payment_id"]) == 1
        engine = make_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE app.recovery_job SET next_attempt_at = clock_timestamp()")
            )
        engine.dispose()
        drain(settings, limit=1)
        assert _job(settings)["state"] == "settled"
        assert calls["n"] == 1
        assert _operation_attempts(settings, proposal["action_id"]) == 1
        assert _refund_count(settings, proposal["target_payment_id"]) == 1
    finally:
        release.set()
        thread.join(timeout=5)


def test_confirmed_ledger_on_a_leased_job_settles_without_another_post(
    client, settings, manifest
) -> None:
    from resolveai.db.session import make_engine

    proposal, job_id = _queue(client, manifest)
    drain(settings, limit=1)
    assert _job(settings)["state"] == "settled"
    assert _ledger_status(settings, job_id) == "confirmed"
    posts = _operation_attempts(settings, proposal["action_id"])
    refunds = _refund_count(settings, proposal["target_payment_id"])
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.recovery_job
                SET state = 'leased',
                    leased_until = clock_timestamp() - interval '1 second'
                WHERE id = :id
                """
            ),
            {"id": job_id},
        )
    engine.dispose()
    drain(settings, limit=1)
    assert _job(settings)["state"] == "settled"
    assert _ledger_status(settings, job_id) == "confirmed"
    assert _operation_attempts(settings, proposal["action_id"]) == posts
    assert _refund_count(settings, proposal["target_payment_id"]) == refunds


def test_stale_requeue_request_cannot_revive_a_settled_job(
    client, settings, manifest, monkeypatch
) -> None:
    import threading
    from uuid import UUID

    from resolveai.db.session import make_engine
    from resolveai.errors import APIError
    from resolveai.services import execution

    proposal, job_id = _queue(client, manifest)
    approver = approver_headers(client)
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
    assert _job(settings)["block_reason"] == "delivery_uncertain"
    refunds = _refund_count(settings, proposal["target_payment_id"])
    session_engine = make_engine(settings.worker_database_url)
    factory = bound_sessions(session_engine)
    from types import SimpleNamespace

    from resolveai.db.models import RecoveryJob

    with factory() as db:
        preview = db.get(RecoveryJob, UUID(job_id))
        assert preview is not None
        actor = SimpleNamespace(
            role="approver",
            tenant_id=preview.tenant_id,
            user_id=preview.authorizer_user_id,
        )
    paused = threading.Event()
    release = threading.Event()
    outcome: dict[str, str] = {}

    def older_request() -> None:
        with factory() as db:
            db.get(RecoveryJob, UUID(job_id))
            scalar = db.scalar
            stopped = False

            def pause_before_ticket_lock(statement, *args, **kwargs):
                nonlocal stopped
                sql = str(statement)
                if not stopped and "FROM app.ticket" in sql and "FOR UPDATE" in sql:
                    stopped = True
                    paused.set()
                    assert release.wait(8)
                return scalar(statement, *args, **kwargs)

            db.scalar = pause_before_ticket_lock
            try:
                recovery.requeue_queries(db, actor=actor, job_id=UUID(job_id))
                db.commit()
                outcome["older_request"] = "requeued"
            except APIError as exc:
                db.rollback()
                outcome["older_request"] = exc.error_code

    thread = threading.Thread(target=older_request)
    try:
        thread.start()
        assert paused.wait(4)
        requeued = client.post(f"/recovery-jobs/{job_id}/requeue", headers=approver)
        assert requeued.status_code == 200, requeued.text
        drain(settings, limit=1)
        assert _job(settings)["state"] == "settled"
        assert _ledger_status(settings, job_id) == "confirmed"
        release.set()
        thread.join(timeout=5)
        assert outcome["older_request"] == "stale_proposal"
        assert _job(settings)["state"] == "settled"
        assert _ledger_status(settings, job_id) == "confirmed"
        assert _refund_count(settings, proposal["target_payment_id"]) == refunds
    finally:
        release.set()
        thread.join(timeout=5)
        session_engine.dispose()
