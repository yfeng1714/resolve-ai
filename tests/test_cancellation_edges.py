"""Cancellation races, receipts, restart, and rollback. No paid model calls."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from tests.support import approver_headers, login, support_headers
from tests.test_cancellation import (
    COMBINED,
    HARBOR,
    HARBOR_SUB,
    IMMEDIATE,
    ONE,
    PLUS,
    STANDARD,
    TWO,
    _approve_execute,
    _investigate,
    _operations,
    _subscription,
)

from resolveai.db.session import make_engine
from resolveai.domain.cancellation import (
    SubscriptionView,
    canonical_instant,
    classify_subscriptions,
)
from resolveai.services import recovery
from resolveai.services.cancellation import receipt_matches
from resolveai.settings import repo_root
from resolveai.tools.client import ToolResult
from resolveai.worker.__main__ import drain

OTHER = "99999999-9999-4999-8999-999999999902"
IMMEDIATE_SUB = "99999999-9999-4999-8999-999999999909"
COMBINED_SUB = "99999999-9999-4999-8999-999999999910"
ROOT = Path(__file__).resolve().parents[1]


def test_decision_table_does_not_guess_a_mixed_or_stale_row() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)
    future = "2026-11-11T00:00:00.000000Z"
    past = "2026-10-01T00:00:00.000000Z"
    only = SubscriptionView("u", "standard", "active", False, future, 1)
    elapsed = SubscriptionView("e", "old", "active", False, past, 1)
    single = classify_subscriptions([only], choice_id=None, now=now)
    mixed = classify_subscriptions([only, elapsed], choice_id=None, now=now)
    stale = classify_subscriptions([only], choice_id="missing", now=now)
    assert single.kind == "propose" and single.subscription_id == "u"
    assert mixed.kind == "wait" and mixed.subscription_id is None
    assert stale.kind == "wait" and stale.reason == "stale_choice"


def test_receipt_match_ignores_other_operations() -> None:
    command = {
        "effect_hash": "effect",
        "action_id": "cancel_at_period_end:sub:2026-11-11T00:00:00.000000Z",
        "account_id": "account",
        "subscription_id": "sub",
        "effective_at": "2026-11-11T00:00:00.000000Z",
    }
    command["expected_resource_version"] = 1
    result = {
        "operation_status": "confirmed",
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "action_id": command["action_id"],
        "account_id": "account",
        "subscription_id": "sub",
        "effective_at": command["effective_at"],
        "resource_version": 2,
        "cancel_at_period_end": True,
    }
    operation = {
        "status": "confirmed",
        "action_id": command["action_id"],
        "operation_id": result["operation_id"],
        "payload_hash": "effect",
        "result": result,
    }
    subscription = {
        "id": "sub",
        "cancel_at_period_end": True,
        "current_period_end": command["effective_at"],
    }
    assert receipt_matches(command, operation, subscription)
    assert not receipt_matches(command, {**operation, "status": "pending"}, subscription)
    assert not receipt_matches(
        command,
        {**operation, "result": {**result, "operation_status": "rejected"}},
        subscription,
    )
    assert not receipt_matches(command, {**operation, "payload_hash": "other"}, subscription)
    assert not receipt_matches(
        command,
        operation,
        {**subscription, "id": "other-sub"},
    )
    incomplete = {key: value for key, value in result.items() if key != "operation_id"}
    assert not receipt_matches(command, {**operation, "result": incomplete}, subscription)
    assert not receipt_matches(
        command,
        {**operation, "result": {**result, "resource_version": True}},
        subscription,
    )
    assert not receipt_matches(
        command,
        {**operation, "result": {**result, "cancel_at_period_end": False}},
        subscription,
    )
    assert not receipt_matches(
        command,
        {**operation, "action_id": "cancel_at_period_end:other:2030-01-01T00:00:00.000000Z"},
        subscription,
    )
    assert not receipt_matches(
        command,
        {
            **operation,
            "result": {**result, "operation_id": "22222222-2222-4222-8222-222222222222"},
        },
        subscription,
    )


def test_grants_and_cross_tenant_ids_do_not_schedule(client, settings) -> None:
    support = support_headers(client)
    rejected = _investigate(client, ONE, support)
    proposal = rejected["proposals"][-1]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    mismatch = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": "0" * 64},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error_code"] == "stale_proposal"
    refused = client.post(
        f"/proposals/{proposal['id']}/reject",
        headers=approver,
        json=body,
    )
    assert refused.status_code == 200, refused.text
    assert "No cancellation was scheduled" in refused.text
    assert _subscription(settings, STANDARD)["flag"] is False
    assert _operations(settings, proposal["action_id"]) == 0

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    hidden = client.post(
        f"/tickets/{ONE}/subscription-choice",
        headers=harbor_support,
        json={"subscription_id": STANDARD},
    )
    assert hidden.status_code == 404
    harbor = _investigate(client, HARBOR, harbor_support)
    harbor_proposal = harbor["proposals"][-1]
    foreign = client.post(
        f"/proposals/{harbor_proposal['id']}/approve",
        headers=approver_headers(client),
        json={
            "revision": harbor_proposal["revision"],
            "payload_hash": harbor_proposal["payload_hash"],
        },
    )
    assert foreign.status_code == 404
    harbor_approver = login(
        client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass"
    )
    approved = client.post(
        f"/proposals/{harbor_proposal['id']}/approve",
        headers=harbor_approver,
        json={
            "revision": harbor_proposal["revision"],
            "payload_hash": harbor_proposal["payload_hash"],
        },
    )
    assert approved.status_code == 200, approved.text
    grant_id = approved.json()["data"]["proposals"][-1]["approval"]["id"]
    revoked = client.post(
        f"/approvals/{grant_id}/revoke",
        headers=harbor_approver,
        json={"reason": "The customer will keep the plan."},
    )
    assert revoked.status_code == 200, revoked.text
    executed = client.post(
        f"/proposals/{harbor_proposal['id']}/execute",
        headers=harbor_approver,
        json={
            "revision": harbor_proposal["revision"],
            "payload_hash": harbor_proposal["payload_hash"],
        },
    )
    assert executed.status_code == 409
    assert _subscription(settings, HARBOR_SUB)["flag"] is False

    waiting = _investigate(client, TWO, support_headers(client))
    assert waiting["proposals"] == []
    chosen = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support_headers(client),
        json={"subscription_id": PLUS},
    )
    assert chosen.status_code == 200, chosen.text
    resumed = _investigate(client, TWO, support_headers(client))
    first = resumed["proposals"][-1]
    approver = approver_headers(client)
    approved = client.post(
        f"/proposals/{first['id']}/approve",
        headers=approver,
        json={"revision": first["revision"], "payload_hash": first["payload_hash"]},
    )
    assert approved.status_code == 200, approved.text
    old_grant = approved.json()["data"]["proposals"][-1]["approval"]["id"]
    _expire_grant(settings, old_grant)
    stale = client.post(
        f"/proposals/{first['id']}/execute",
        headers=approver,
        json={"revision": first["revision"], "payload_hash": first["payload_hash"]},
    )
    assert stale.status_code == 409
    assert _subscription(settings, PLUS)["flag"] is False
    renewed = client.post(
        f"/proposals/{first['id']}/approve",
        headers=approver_headers(client),
        json={"revision": first["revision"], "payload_hash": first["payload_hash"]},
    )
    assert renewed.status_code == 200, renewed.text
    new_grant = renewed.json()["data"]["proposals"][-1]["approval"]["id"]
    assert new_grant != old_grant
    executed = client.post(
        f"/proposals/{first['id']}/execute",
        headers=approver_headers(client),
        json={"revision": first["revision"], "payload_hash": first["payload_hash"]},
    )
    assert executed.status_code == 200, executed.text
    assert _jobs(settings)[-1]["approval_id"] == new_grant
    drain(settings, limit=3)
    assert _subscription(settings, PLUS)["flag"] is True
    assert _subscription(settings, OTHER)["flag"] is False
    assert _operations(settings, first["action_id"]) == 1


def test_version_changes_block_before_a_write(client, settings) -> None:
    support = support_headers(client)
    north = _investigate(client, ONE, support)
    proposal = north["proposals"][-1]
    _bump_version(settings, STANDARD)
    approver = approver_headers(client)
    approved = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert approved.status_code == 200, approved.text
    blocked = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "version_conflict"
    assert _subscription(settings, STANDARD)["flag"] is False
    assert _operations(settings, proposal["action_id"]) == 0

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate(client, HARBOR, harbor_support)
    harbor_approver = login(
        client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass"
    )
    queued = _approve_execute(client, harbor, harbor_approver)
    _bump_version(settings, HARBOR_SUB)
    drain(settings, limit=3)
    state = _subscription(settings, HARBOR_SUB)
    assert state["flag"] is False
    assert state["version"] == 2
    assert _operations(settings, queued["action_id"]) == 0
    assert _jobs(settings)[-1]["block_reason"] == "version_conflict"


def test_period_end_during_approval_is_not_scheduled(client, settings) -> None:
    _shift_period(settings, STANDARD, 20)
    detail = _investigate(client, ONE, support_headers(client))
    proposal = detail["proposals"][-1]
    instant = proposal["action_id"].split(f"{STANDARD}:", 1)[1]
    assert len(instant) == 27 and instant.endswith("Z")
    approver = approver_headers(client)
    approved = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert approved.status_code == 200, approved.text
    _wait_until_period_passes(settings, STANDARD)
    executed = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert executed.status_code == 409
    assert executed.json()["error_code"] == "period_elapsed"
    assert _subscription(settings, STANDARD)["flag"] is False
    assert _operations(settings, proposal["action_id"]) == 0


def test_new_input_replaces_only_an_unsent_attempt(client, settings) -> None:
    detail = _investigate(client, ONE, support_headers(client))
    proposal = _approve_execute(client, detail, approver_headers(client))
    original = _jobs(settings)[-1]
    assert original["admitted"] is False
    posted = client.post(
        f"/tickets/{ONE}/messages",
        headers=support_headers(client),
        json={"body": "Please schedule cancellation at period end."},
    )
    assert posted.status_code == 200, posted.text
    cancelled = _jobs(settings)[-1]
    assert cancelled["state"] == "cancelled"
    assert cancelled["command"] == original["command"]
    assert "payment_id" not in cancelled["command"]
    resumed = _investigate(client, ONE, support_headers(client))
    fresh = resumed["proposals"][-1]
    assert fresh["id"] != proposal["id"]
    assert fresh["payload_hash"] == proposal["payload_hash"]
    rows = _proposals(settings)
    assert rows[0]["status"] == "superseded"
    assert rows[0]["input_revision"] != rows[-1]["input_revision"]
    _approve_execute(client, resumed, approver_headers(client))
    replacement = _jobs(settings)[-1]
    assert replacement["approval_id"] != original["approval_id"]
    assert replacement["command"] != original["command"] or replacement["id"] != original["id"]
    drain(settings, limit=3)
    assert _operations(settings, proposal["action_id"]) == 1
    assert _subscription(settings, STANDARD)["flag"] is True
    assert _jobs(settings)[0]["command"] == original["command"]


def test_admitted_attempt_is_not_posted_again(client, settings, tmp_path: Path) -> None:
    detail = _investigate(client, ONE, support_headers(client))
    proposal = _approve_execute(client, detail, approver_headers(client))
    process, ready, release = _start_worker(settings, tmp_path, "before_post")
    try:
        assert _wait_ready(process, ready)
        again = client.post(
            f"/proposals/{proposal['id']}/execute",
            headers=approver_headers(client),
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        )
        assert again.status_code == 409
        assert again.json()["error_code"] == "period_already_dispatched"
        assert _operations(settings, proposal["action_id"]) == 0
        release.write_text("go", encoding="utf-8")
        assert _wait_job(settings, "settled")
    finally:
        _stop(process)
    assert _operations(settings, proposal["action_id"]) == 1
    assert _subscription(settings, STANDARD)["version"] == 2
    settled = _jobs(settings)[-1]
    assert settled["state"] == "settled"
    assert settled["admitted"] is True
    assert _operations(settings, proposal["action_id"]) == 1


def test_kill_before_post_never_gains_a_second_dispatch(client, settings, tmp_path: Path) -> None:
    detail = _investigate(client, HARBOR, _harbor_support(client))
    proposal = _approve_execute(client, detail, _harbor_approver(client))
    process, ready, _release = _start_worker(settings, tmp_path, "before_post")
    try:
        assert _wait_ready(process, ready)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            _stop(process)
    assert _subscription(settings, HARBOR_SUB)["flag"] is False
    assert _jobs(settings)[-1]["admitted"] is True
    assert _operations(settings, proposal["action_id"]) == 0
    _expire_lease(settings)
    recovery.BACKOFF_SECONDS = (0, 0, 0, 0, 0)
    try:
        drain(settings)
        assert _jobs(settings)[-1]["state"] == "blocked"
        assert _jobs(settings)[-1]["block_reason"] == "delivery_uncertain"
        requeued = client.post(
            f"/recovery-jobs/{_jobs(settings)[-1]['id']}/requeue",
            headers=_harbor_approver(client),
        )
        assert requeued.status_code == 200, requeued.text
        drain(settings)
        assert _operations(settings, proposal["action_id"]) == 0
        assert _subscription(settings, HARBOR_SUB)["flag"] is False
    finally:
        recovery.BACKOFF_SECONDS = (1, 2, 4, 8, 16)


def test_restart_after_the_write_confirms_the_same_operation(
    client, settings, tmp_path: Path
) -> None:
    _shift_period(settings, STANDARD, 25)
    detail = _investigate(client, ONE, support_headers(client))
    proposal = _approve_execute(client, detail, approver_headers(client))
    process, ready, _release = _start_worker(settings, tmp_path, "before_local_confirm")
    try:
        assert _wait_ready(process, ready)
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            _stop(process)
    assert _subscription(settings, STANDARD)["flag"] is True
    assert _operations(settings, proposal["action_id"]) == 1
    _wait_until_period_passes(settings, STANDARD)
    _expire_lease(settings)
    recovery.BACKOFF_SECONDS = (0, 0, 0, 0, 0)
    try:
        drain(settings, limit=3)
    finally:
        recovery.BACKOFF_SECONDS = (1, 2, 4, 8, 16)
    state = _subscription(settings, STANDARD)
    assert state["flag"] is True
    assert state["status"] == "active"
    assert state["version"] == 2
    assert _operations(settings, proposal["action_id"]) == 1
    assert "does not end access by itself" in _latest_reply(settings)
    assert _receipt(settings, proposal["action_id"], STANDARD)


def test_choice_and_intent_gates_do_not_select_another_row(client, settings) -> None:
    support = support_headers(client)
    _investigate(client, TWO, support)
    chosen = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support,
        json={"subscription_id": PLUS},
    )
    assert chosen.status_code == 200, chosen.text
    _delete_subscription(settings, PLUS)
    stale = _investigate(client, TWO, support)
    assert stale["proposals"] == []
    assert stale["runs"][-1]["state"] == "awaiting_customer"
    assert _subscription(settings, OTHER)["flag"] is False

    immediate = _investigate(client, IMMEDIATE, support_headers(client))
    assert immediate["proposals"] == []
    recorded = client.post(
        f"/tickets/{IMMEDIATE}/subscription-choice",
        headers=support_headers(client),
        json={"subscription_id": IMMEDIATE_SUB},
    )
    assert recorded.status_code == 200, recorded.text
    resumed = _investigate(client, IMMEDIATE, support_headers(client))
    assert resumed["proposals"][-1]["target_subscription_id"] == IMMEDIATE_SUB

    combined = _investigate(client, COMBINED, support_headers(client))
    assert {item["action_type"] for item in combined["proposals"]} == {
        "refund",
        "cancel_at_period_end",
    }
    assert combined["runs"][-1]["state"] == "awaiting_approval"


def test_b1_reaches_the_same_cancellation_outcomes(
    client, api_app, restore_runtime
) -> None:
    del restore_runtime
    from resolveai.evaluation.catalog import use_baseline

    use_baseline(api_app)
    support = support_headers(client)
    single = _investigate(client, ONE, support)
    assert single["proposals"][-1]["action_type"] == "cancel_at_period_end"
    waiting = _investigate(client, TWO, support)
    assert waiting["proposals"] == []
    assert waiting["runs"][-1]["state"] == "awaiting_customer"
    chosen = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support,
        json={"subscription_id": PLUS},
    )
    assert chosen.status_code == 200, chosen.text
    resumed = _investigate(client, TWO, support)
    assert resumed["proposals"][-1]["target_subscription_id"] == PLUS
    immediate = _investigate(client, IMMEDIATE, support)
    assert immediate["proposals"] == []
    combined = _investigate(client, COMBINED, support)
    assert {item["action_type"] for item in combined["proposals"]} == {
        "refund",
        "cancel_at_period_end",
    }


def test_confirmed_cancellation_survives_a_later_bad_read(
    client, settings, api_app, restore_runtime
) -> None:
    del restore_runtime
    detail = _investigate(client, ONE, support_headers(client))
    proposal = _approve_execute(client, detail, approver_headers(client))
    drain(settings, limit=3)
    assert _ledger_status(settings, proposal["action_id"]) == "confirmed"
    reply = _latest_reply(settings)
    _set_operation_status(settings, proposal["action_id"], "pending")
    conflict = client.post(
        f"/actions/{proposal['action_id']}/reconcile",
        headers=approver_headers(client),
    )
    assert conflict.status_code == 409
    assert _ledger_status(settings, proposal["action_id"]) == "confirmed"
    assert _latest_reply(settings) == reply
    _set_operation_status(settings, proposal["action_id"], "confirmed")
    original = api_app.state.tool_client_factory

    def unavailable(bound):
        return _UnavailableReads(original(bound))

    api_app.state.tool_client_factory = unavailable
    kept = client.post(
        f"/actions/{proposal['action_id']}/reconcile",
        headers=approver_headers(client),
    )
    assert kept.status_code == 200, kept.text
    assert _ledger_status(settings, proposal["action_id"]) == "confirmed"
    assert _latest_reply(settings) == reply
    api_app.state.tool_client_factory = original
    _delete_operation(settings, proposal["action_id"])
    missing = client.post(
        f"/actions/{proposal['action_id']}/reconcile",
        headers=approver_headers(client),
    )
    assert missing.status_code == 409
    assert _ledger_status(settings, proposal["action_id"]) == "confirmed"
    assert _refund_count(settings) == 2


def test_runtime_roles_cannot_rewrite_choice_or_command_shape(client, settings) -> None:
    support = support_headers(client)
    _investigate(client, TWO, support)
    chosen = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support,
        json={"subscription_id": PLUS},
    )
    assert chosen.status_code == 200, chosen.text
    api = make_engine(settings.api_database_url)
    auth = make_engine(settings.auth_database_url)
    worker = make_engine(settings.worker_database_url)
    try:
        with pytest.raises(DBAPIError):
            with api.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": "11111111-1111-4111-8111-111111111111"},
                )
                connection.execute(
                    text("UPDATE app.subscription_choice SET input_revision = input_revision")
                )
        with pytest.raises(ProgrammingError):
            with auth.connect() as connection:
                connection.execute(text("SELECT count(*) FROM app.subscription_choice"))
        with pytest.raises(ProgrammingError):
            with worker.connect() as connection:
                connection.execute(text("SELECT count(*) FROM app.subscription_choice"))
    finally:
        api.dispose()
        auth.dispose()
        worker.dispose()
    detail = _investigate(client, ONE, support_headers(client))
    proposal = _approve_execute(client, detail, approver_headers(client))
    admin = make_engine(settings.database_url)
    try:
        with pytest.raises(DBAPIError):
            with admin.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE app.recovery_job
                        SET command = command || '{"payment_id": "not-a-payment"}'::jsonb
                        WHERE payload_hash = :payload_hash
                        """
                    ),
                    {"payload_hash": proposal["payload_hash"]},
                )
    finally:
        admin.dispose()
    assert "payment_id" not in _jobs(settings)[-1]["command"]


def test_populated_cancellation_downgrade_keeps_the_proposal_hash(client, settings) -> None:
    detail = _investigate(client, ONE, support_headers(client))
    proposal = detail["proposals"][-1]
    before = _proposals(settings)[-1]["payload_hash"]
    assert before == proposal["payload_hash"]
    refused = _alembic(settings, "downgrade", "20261027_0007")
    assert refused.returncode != 0
    admin = make_engine(settings.database_url)
    with admin.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        after = connection.execute(
            text(
                """
                SELECT payload_hash FROM app.proposal
                WHERE id = :id
                """
            ),
            {"id": proposal["id"]},
        ).scalar_one()
    admin.dispose()
    assert version == "20261112_0014"
    assert after == before


class _UnavailableReads:
    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def list_subscriptions(self, account_id: object) -> ToolResult:
        del account_id
        return _error("unavailable")

    def get_operation(self, account_id: object, action_id: object) -> ToolResult:
        del account_id, action_id
        return _error("unavailable")


def _error(code: str) -> ToolResult:
    return ToolResult(
        status="error",
        data={},
        error_code=code,
        retryable=True,
        operation_id=None,
        resource_version=None,
        observed_at="",
        request_id="cancellation-read",
    )


def _jobs(settings) -> list[dict]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id::text, state, block_reason,
                       admitted_at IS NOT NULL AS admitted,
                       command::text AS command,
                       approval_id::text,
                       payload_hash
                FROM app.recovery_job
                ORDER BY created_at
                """
            )
        ).mappings().all()
    engine.dispose()
    return [dict(row) for row in rows]


def _proposals(settings) -> list[dict]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id::text, status, payload_hash,
                       preconditions->>'input_revision' AS input_revision
                FROM app.proposal
                WHERE action_type = 'cancel_at_period_end'
                ORDER BY created_at
                """
            )
        ).mappings().all()
    engine.dispose()
    return [dict(row) for row in rows]


def _ledger_status(settings, action_id: str) -> str:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM app.action_ledger WHERE action_id = :id"),
            {"id": action_id},
        ).scalar_one()
    engine.dispose()
    return str(status)


def _latest_reply(settings) -> str:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        body = connection.execute(
            text("SELECT body FROM app.reply_draft ORDER BY created_at DESC LIMIT 1")
        ).scalar_one()
    engine.dispose()
    return str(body)


def _refund_count(settings) -> int:
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM simulator.refund")).scalar_one()
    engine.dispose()
    return int(count)


def _bump_version(settings, subscription_id: str) -> None:
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE simulator.subscription
                SET resource_version = resource_version + 1
                WHERE id = :id
                """
            ),
            {"id": subscription_id},
        )
    engine.dispose()


def _shift_period(settings, subscription_id: str, seconds: int) -> datetime:
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        ends = connection.execute(
            text(
                """
                UPDATE simulator.subscription
                SET current_period_end = clock_timestamp()
                    + make_interval(secs => :seconds)
                WHERE id = :id
                RETURNING current_period_end
                """
            ),
            {"id": subscription_id, "seconds": seconds},
        ).scalar_one()
    engine.dispose()
    return ends


def _delete_subscription(settings, subscription_id: str) -> None:
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM simulator.subscription WHERE id = :id"),
            {"id": subscription_id},
        )
    engine.dispose()


def _set_operation_status(settings, action_id: str, status: str) -> None:
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE simulator.operation SET status = :status WHERE action_id = :id"),
            {"status": status, "id": action_id},
        )
    engine.dispose()


def _delete_operation(settings, action_id: str) -> None:
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM simulator.operation WHERE action_id = :id"),
            {"id": action_id},
        )
    engine.dispose()


def _expire_grant(settings, approval_id: str) -> None:
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        result = connection.execute(
            text(
                """
                UPDATE app.approval
                SET expires_at = clock_timestamp() - interval '1 second'
                WHERE id = :id
                """
            ),
            {"id": approval_id},
        )
        assert result.rowcount == 1
    engine.dispose()


def _expire_lease(settings) -> None:
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE app.recovery_job "
                "SET leased_until = clock_timestamp() - interval '1 second'"
            )
        )
    engine.dispose()


def _receipt(settings, action_id: str, subscription_id: str) -> bool:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        command = connection.execute(
            text(
                """
                SELECT command FROM app.recovery_job
                WHERE command->>'action_id' = :action_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"action_id": action_id},
        ).scalar_one()
    engine.dispose()
    simulator = make_engine(settings.simulator_database_url)
    with simulator.connect() as connection:
        operation = connection.execute(
            text(
                """
                SELECT status, payload_hash, result, operation_id::text AS operation_id
                FROM simulator.operation WHERE action_id = :id
                """
            ),
            {"id": action_id},
        ).mappings().one()
    simulator.dispose()
    state = _subscription(settings, subscription_id)
    return receipt_matches(
        dict(command),
        {
            "status": operation["status"],
            "action_id": action_id,
            "operation_id": operation["operation_id"],
            "payload_hash": operation["payload_hash"],
            "result": operation["result"],
        },
        {
            "id": subscription_id,
            "cancel_at_period_end": state["flag"],
            "current_period_end": canonical_instant(state["ends"]),
        },
    )


def _harbor_support(client) -> dict[str, str]:
    return login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")


def _harbor_approver(client) -> dict[str, str]:
    return login(client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass")


def _wait_until_period_passes(settings, subscription_id: str) -> None:
    engine = make_engine(settings.simulator_database_url)
    try:
        for _ in range(240):
            with engine.connect() as connection:
                passed = connection.execute(
                    text(
                        """
                        SELECT clock_timestamp() > current_period_end
                        FROM simulator.subscription
                        WHERE id = :id
                        """
                    ),
                    {"id": subscription_id},
                ).scalar_one()
            if passed:
                time.sleep(1)
                return
            time.sleep(0.25)
    finally:
        engine.dispose()
    raise AssertionError("period end did not pass")


def _worker_env(settings, hold: str, ready: Path, release: Path) -> dict[str, str]:
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
            "RESOLVEAI_RECOVERY_HOLD": hold,
            "RESOLVEAI_RECOVERY_READY": str(ready),
            "RESOLVEAI_RECOVERY_RELEASE": str(release),
        }
    )
    return env


def _start_worker(settings, tmp_path: Path, hold: str):
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    process = subprocess.Popen(
        [sys.executable, "-m", "resolveai.worker"],
        env=_worker_env(settings, hold, ready, release),
        cwd=str(ROOT),
    )
    return process, ready, release


def _wait_ready(process: subprocess.Popen, ready: Path) -> bool:
    for _ in range(100):
        if ready.exists() or process.poll() is not None:
            break
        time.sleep(0.05)
    return ready.exists()


def _wait_job(settings, state: str) -> bool:
    for _ in range(100):
        if _jobs(settings)[-1]["state"] == state:
            return True
        time.sleep(0.05)
    return False


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _alembic(settings, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": settings.database_url,
            "PYTHONPATH": str(repo_root() / "src"),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
