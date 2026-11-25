"""Maintained regressions for the M3.4 review findings. No paid model calls."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.support import approver_headers, seeing_engine, support_headers
from tests.test_auth_and_workflow import _investigate as refund_investigate
from tests.test_cancellation import (
    IMMEDIATE,
    ONE,
    STANDARD,
    _approve_execute,
    _investigate,
    _operations,
    _subscription,
)

from resolveai.agent.provider import FakeProvider, ProviderAction
from resolveai.db.session import make_engine
from resolveai.domain.cancellation import (
    cancellation_action_id,
    effect_hash,
    effect_payload,
)
from resolveai.services.cancellation import load_subscription_policy_version
from resolveai.tools.client import SimulatorClient
from resolveai.worker.__main__ import drain


def _queue(client) -> dict:
    detail = _investigate(client, ONE, support_headers(client))
    return _approve_execute(client, detail, approver_headers(client))


def _jobs(settings, action_id: str) -> list[dict]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT j.id::text, j.state, j.command::text AS command,
                       j.owner_token::text AS owner_token,
                       l.status AS ledger_status
                FROM app.recovery_job AS j
                JOIN app.action_ledger AS l ON l.id = j.ledger_id
                WHERE l.action_id = :action_id
                ORDER BY j.created_at, j.id
                """
            ),
            {"action_id": action_id},
        ).mappings().all()
    engine.dispose()
    return [dict(row) for row in rows]


@pytest.mark.parametrize("column", ["amount_cents", "currency"])
def test_refund_shape_rejects_null_required_fields(client, settings, column: str) -> None:
    detail = refund_investigate(client, "55555555-5555-4555-8555-555555555501")
    proposal_id = detail["proposals"][-1]["id"]
    engine = seeing_engine(settings.api_database_url)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE app.proposal SET {column} = NULL WHERE id = :id"),
                {"id": proposal_id},
            )
    engine.dispose()


def test_changed_command_version_cannot_use_an_old_grant(client, settings) -> None:
    proposal = _queue(client)
    admin = make_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(
            text("UPDATE simulator.subscription SET resource_version = 2 WHERE id = :id"),
            {"id": STANDARD},
        )
    api = seeing_engine(settings.api_database_url)
    with pytest.raises(DBAPIError):
        with api.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE app.recovery_job
                    SET command = jsonb_set(command, '{expected_resource_version}', '2'::jsonb)
                    WHERE proposal_id = :id
                    """
                ),
                {"id": proposal["id"]},
            )
    admin.dispose()
    api.dispose()
    drain(settings, limit=2)
    assert _subscription(settings, STANDARD)["flag"] is False
    assert _operations(settings, proposal["action_id"]) == 0


def test_incomplete_receipt_does_not_confirm(client, settings, monkeypatch) -> None:
    proposal = _queue(client)
    original = SimulatorClient.get_operation

    def masked(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if result.status != "ok":
            return result
        receipt = dict(result.data.get("result") or {})
        for key in ("operation_id", "resource_version", "cancel_at_period_end"):
            receipt.pop(key, None)
        return replace(result, data={**result.data, "result": receipt})

    monkeypatch.setattr(SimulatorClient, "get_operation", masked)
    drain(settings, limit=1)
    ledgers = [row["ledger_status"] for row in _jobs(settings, proposal["action_id"])]
    assert ledgers
    assert "confirmed" not in ledgers


@pytest.mark.parametrize("ticket_id", [IMMEDIATE])
def test_untrusted_model_cannot_bypass_the_intent_gate(
    client, api_app, ticket_id: str, monkeypatch
) -> None:
    class ForcedProposal(FakeProvider):
        def __init__(self) -> None:
            self.step = 0

        def next_action(self, snapshot: object) -> ProviderAction:
            del snapshot
            self.step += 1
            if self.step == 1:
                return ProviderAction(
                    route="tool", tool_name="list_subscriptions", reason="Read owned rows"
                )
            if self.step == 2:
                return ProviderAction(
                    route="tool",
                    tool_name="propose_cancellation",
                    reason="Attempt to bypass the intent gate",
                )
            return ProviderAction(route="decide", reason="Stop")

    monkeypatch.setattr(api_app.state, "provider_factory", ForcedProposal)
    response = client.post(f"/tickets/{ticket_id}/runs", headers=support_headers(client))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["proposals"] == []


def test_repeat_execute_reuses_the_unsent_job(client, settings) -> None:
    proposal = _queue(client)
    before = _jobs(settings, proposal["action_id"])
    repeated = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver_headers(client),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert repeated.status_code == 200, repeated.text
    after = _jobs(settings, proposal["action_id"])
    assert len(after) == 1
    assert after[0]["id"] == before[0]["id"]
    assert after[0]["state"] == "queued"
    assert after[0]["command"] == before[0]["command"]


def test_repeat_execute_keeps_a_leased_owner(client, settings) -> None:
    proposal = _queue(client)
    owner = uuid4()
    admin = make_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.recovery_job
                SET state = 'leased',
                    owner_token = :owner,
                    owner_generation = 1,
                    leased_until = clock_timestamp() + interval '30 seconds'
                WHERE proposal_id = :id
                """
            ),
            {"owner": owner, "id": proposal["id"]},
        )
    admin.dispose()
    repeated = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver_headers(client),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert repeated.status_code == 200, repeated.text
    rows = _jobs(settings, proposal["action_id"])
    assert len(rows) == 1
    assert rows[0]["state"] == "leased"
    assert rows[0]["owner_token"] == str(owner)


def test_clock_guard_survives_a_blocked_business_flush(settings, seeded, simulator_app) -> None:
    del seeded, simulator_app
    admin = make_engine(settings.database_url)
    with admin.begin() as connection:
        end = connection.execute(
            text(
                """
                UPDATE simulator.subscription
                SET current_period_end = clock_timestamp() + interval '2 seconds'
                WHERE id = :id
                RETURNING current_period_end
                """
            ),
            {"id": STANDARD},
        ).scalar_one()
        account = str(
            connection.execute(
                text("SELECT account_id FROM simulator.subscription WHERE id = :id"),
                {"id": STANDARD},
            ).scalar_one()
        )
    from resolveai.domain.cancellation import canonical_instant

    effective = canonical_instant(end)
    action_id = cancellation_action_id(STANDARD, effective)
    payload = effect_payload(
        action_id=action_id,
        account_id=account,
        subscription_id=STANDARD,
        effective_at=effective,
        policy_version=load_subscription_policy_version(),
    )
    blocker = admin.connect()
    blocker.execute(text("LOCK TABLE simulator.operation IN SHARE MODE"))
    result: dict[str, str | None] = {}

    def call() -> None:
        tools = SimulatorClient(settings)
        try:
            response = tools.schedule_period_end(
                account_id=UUID(account),
                subscription_id=UUID(STANDARD),
                effective_at=effective,
                action_id=action_id,
                effect_hash=effect_hash(payload),
                expected_resource_version=1,
                policy_version=str(payload["policy_version"]),
            )
            result["status"] = response.status
            result["error_code"] = response.error_code
        finally:
            tools.close()

    worker = threading.Thread(target=call)
    worker.start()
    waited = False
    elapsed = False
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            for _ in range(180):
                waited = bool(
                    connection.execute(
                        text(
                            """
                            SELECT count(*)
                            FROM pg_stat_activity
                            WHERE usename = 'resolveai_simulator'
                              AND datname = current_database()
                              AND wait_event_type = 'Lock'
                            """
                        )
                    ).scalar_one()
                )
                elapsed = bool(
                    connection.execute(
                        text("SELECT clock_timestamp() >= :end"),
                        {"end": end},
                    ).scalar_one()
                )
                if waited and elapsed:
                    break
                time.sleep(0.02)
        assert waited and elapsed
    finally:
        blocker.rollback()
        blocker.close()
        worker.join(timeout=8)
    with admin.connect() as connection:
        flag = connection.execute(
            text("SELECT cancel_at_period_end FROM simulator.subscription WHERE id = :id"),
            {"id": STANDARD},
        ).scalar_one()
        operations = connection.execute(
            text("SELECT count(*) FROM simulator.operation WHERE action_id = :id"),
            {"id": action_id},
        ).scalar_one()
    admin.dispose()
    assert flag is False
    assert operations == 0
    assert result.get("status") == "error"
    assert result.get("error_code") == "period_elapsed"


def test_newer_immediate_request_does_not_reuse_period_end_wording(client, settings) -> None:
    support = support_headers(client)
    posted = client.post(
        f"/tickets/{ONE}/messages",
        headers=support,
        json={"body": "Customer update: terminate immediately. Please do not wait."},
    )
    assert posted.status_code == 200, posted.text
    detail = _investigate(client, ONE, support)
    assert detail["proposals"] == []
    assert _operations(settings, "cancel_at_period_end:%") == 0


def test_a_choice_must_follow_the_latest_immediate_request(client, settings) -> None:
    support = support_headers(client)
    chosen = client.post(
        f"/tickets/{IMMEDIATE}/subscription-choice",
        headers=support,
        json={"subscription_id": "99999999-9999-4999-8999-999999999909"},
    )
    assert chosen.status_code == 200, chosen.text
    renewed = client.post(
        f"/tickets/{IMMEDIATE}/messages",
        headers=support,
        json={"body": "Customer update: terminate immediately. Please do not wait."},
    )
    assert renewed.status_code == 200, renewed.text
    blocked = _investigate(client, IMMEDIATE, support)
    assert blocked["proposals"] == []
    assert _operations(settings, "cancel_at_period_end:%") == 0
    fresh = client.post(
        f"/tickets/{IMMEDIATE}/subscription-choice",
        headers=support,
        json={"subscription_id": "99999999-9999-4999-8999-999999999909"},
    )
    assert fresh.status_code == 200, fresh.text
    resumed = _investigate(client, IMMEDIATE, support)
    proposal = resumed["proposals"][-1]
    assert proposal["action_type"] == "cancel_at_period_end"
    assert proposal["target_subscription_id"] == "99999999-9999-4999-8999-999999999909"
    assert _operations(settings, proposal["action_id"]) == 0


@pytest.mark.parametrize("field", ["outer_action_id", "result_operation_id"])
def test_contradictory_receipt_identity_does_not_confirm(
    client, settings, monkeypatch, field: str
) -> None:
    proposal = _queue(client)
    fabricated = str(uuid4())
    original = SimulatorClient.get_operation

    def changed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if result.status != "ok":
            return result
        data = dict(result.data)
        if field == "outer_action_id":
            data["action_id"] = (
                "cancel_at_period_end:another-subscription:2030-01-01T00:00:00.000000Z"
            )
        else:
            data["result"] = {**data["result"], "operation_id": fabricated}
        return replace(result, data=data)

    monkeypatch.setattr(SimulatorClient, "get_operation", changed)
    drain(settings, limit=1)
    rows = _jobs(settings, proposal["action_id"])
    assert rows
    assert all(row["ledger_status"] != "confirmed" for row in rows)
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        recorded = connection.execute(
            text("SELECT operation_id FROM app.action_ledger WHERE action_id = :id"),
            {"id": proposal["action_id"]},
        ).scalar_one()
    engine.dispose()
    assert recorded in {None, ""}
    assert recorded != fabricated
    assert _operations(settings, proposal["action_id"]) == 1


IMMEDIATE_SUB = "99999999-9999-4999-8999-999999999909"
PERIOD_END_TEXT = "Please schedule cancellation at period end."
RENEWED_IMMEDIATE = "Customer update: terminate immediately."


def _use_provider(api_app, provider_name: str) -> None:
    from resolveai.evaluation.catalog import use_baseline

    api_app.state.provider_factory = FakeProvider
    if provider_name == "b1":
        use_baseline(api_app)


def _post_message(client, ticket_id: str, headers: dict[str, str], body: str) -> None:
    response = client.post(
        f"/tickets/{ticket_id}/messages",
        headers=headers,
        json={"body": body},
    )
    assert response.status_code == 200, response.text


def _choose_immediate(client, headers: dict[str, str]) -> None:
    response = client.post(
        f"/tickets/{IMMEDIATE}/subscription-choice",
        headers=headers,
        json={"subscription_id": IMMEDIATE_SUB},
    )
    assert response.status_code == 200, response.text


def _refund_rows(settings) -> int:
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM simulator.refund")).scalar_one()
    engine.dispose()
    return int(count)


@pytest.mark.parametrize("provider_name", ["fake", "b1"])
@pytest.mark.parametrize("old_choice", [False, True])
def test_period_end_text_does_not_replace_a_stored_choice(
    client, api_app, settings, restore_runtime, provider_name: str, old_choice: bool
) -> None:
    del restore_runtime
    _use_provider(api_app, provider_name)
    support = support_headers(client)
    if old_choice:
        _choose_immediate(client, support)
        _post_message(client, IMMEDIATE, support, RENEWED_IMMEDIATE)
    _post_message(client, IMMEDIATE, support, PERIOD_END_TEXT)
    detail = _investigate(client, IMMEDIATE, support)
    assert detail["proposals"] == []
    assert _subscription(settings, IMMEDIATE_SUB)["flag"] is False
    assert _operations(settings, "cancel_at_period_end:%") == 0


@pytest.mark.parametrize("provider_name", ["fake", "b1"])
def test_fresh_choice_after_the_latest_immediate_can_be_approved(
    client, api_app, settings, restore_runtime, provider_name: str
) -> None:
    del restore_runtime
    _use_provider(api_app, provider_name)
    support = support_headers(client)
    _choose_immediate(client, support)
    _post_message(client, IMMEDIATE, support, RENEWED_IMMEDIATE)
    blocked = _investigate(client, IMMEDIATE, support)
    assert blocked["proposals"] == []
    assert _subscription(settings, IMMEDIATE_SUB)["flag"] is False
    _choose_immediate(client, support)
    resumed = _investigate(client, IMMEDIATE, support)
    assert _operations(settings, "cancel_at_period_end:%") == 0
    assert _refund_rows(settings) == 2
    proposal = _approve_execute(client, resumed, approver_headers(client))
    assert drain(settings, limit=2) >= 1
    state = _subscription(settings, IMMEDIATE_SUB)
    assert state["flag"] is True
    assert state["version"] == 2
    assert _operations(settings, proposal["action_id"]) == 1
    assert _refund_rows(settings) == 2
