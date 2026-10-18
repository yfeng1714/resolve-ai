"""Regressions for the M1 review findings."""

from __future__ import annotations

import threading
from dataclasses import replace
from uuid import UUID, uuid4

from sqlalchemy import select, text
from starlette.testclient import TestClient
from tests.support import approver_headers, case_by_key, support_headers
from tests.test_auth_and_workflow import _investigate, _refund_count

from resolveai.api.security import Actor
from resolveai.db.models import Membership, Ticket, UserAccount
from resolveai.db.session import make_engine, make_session_factory
from resolveai.services.execution import send_reply
from resolveai.services.replies import append_message
from resolveai.tools.client import SimulatorClient, ToolResult


def _approve(client, proposal: dict) -> dict[str, str]:
    headers = approver_headers(client)
    response = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=headers,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert response.status_code == 200, response.text
    return headers


def _execute(client, proposal: dict, headers: dict[str, str]):
    return client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=headers,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )


def _forged_receipt(**kwargs: object) -> ToolResult:
    return ToolResult(
        status="ok",
        data={
            "operation_status": "confirmed",
            "operation_id": str(uuid4()),
            "amount_cents": kwargs["amount_cents"],
            "currency": "EUR",
            "payment_id": str(uuid4()),
            "event_id": kwargs["event_id"],
            "action_id": kwargs["action_id"],
        },
        error_code=None,
        retryable=False,
        operation_id=str(uuid4()),
        resource_version=None,
        observed_at="2026-10-12T00:00:00+00:00",
        request_id="forged-write",
    )


class ForgedWrite(SimulatorClient):
    def create_refund(self, **kwargs: object) -> ToolResult:
        return _forged_receipt(**kwargs)


class MismatchedRead(SimulatorClient):
    def get_operation(self, account_id: UUID, action_id: str) -> ToolResult:
        result = super().get_operation(account_id, action_id)
        if result.status != "ok":
            return result
        data = dict(result.data)
        payload = dict(data.get("result") or {})
        payload["currency"] = "EUR"
        data["result"] = payload
        return replace(result, data=data)


class UnavailableRead(SimulatorClient):
    def get_operation(self, account_id: UUID, action_id: str) -> ToolResult:
        del account_id, action_id
        return ToolResult(
            status="error",
            data={},
            error_code="unavailable",
            retryable=True,
            operation_id=None,
            resource_version=None,
            observed_at="2026-10-12T00:00:00+00:00",
            request_id="unavailable-read",
        )


def _install(api_app, client_type: type[SimulatorClient]) -> None:
    api_app.state.tool_client_factory = client_type


def test_success_requires_an_independent_refund(
    client, api_app, settings, manifest, restore_runtime
) -> None:
    del restore_runtime
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    headers = _approve(client, proposal)
    _install(api_app, ForgedWrite)
    executed = _execute(client, proposal, headers)
    assert executed.status_code == 200, executed.text
    body = executed.json()["data"]
    assert body["actions"][0]["status"] == "unknown"
    assert "We confirmed a refund" not in body["replies"][0]["body"]
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def test_mismatched_receipt_stays_unknown(
    client, api_app, settings, manifest, restore_runtime
) -> None:
    del restore_runtime
    case = case_by_key(manifest, "duplicate-reject")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    headers = _approve(client, proposal)
    _install(api_app, MismatchedRead)
    executed = _execute(client, proposal, headers)
    assert executed.status_code == 200, executed.text
    body = executed.json()["data"]
    assert body["actions"][0]["status"] == "unknown"
    assert "We confirmed a refund" not in body["replies"][0]["body"]
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def test_unavailable_verification_stays_unknown(
    client, api_app, settings, manifest, restore_runtime
) -> None:
    del restore_runtime
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    headers = _approve(client, proposal)
    _install(api_app, UnavailableRead)
    executed = _execute(client, proposal, headers)
    assert executed.status_code == 200, executed.text
    payload = executed.json()["data"]
    assert payload["actions"][0]["status"] == "unknown"
    assert "We confirmed a refund" not in payload["replies"][0]["body"]


def test_new_input_invalidates_an_approved_refund(client, settings, manifest) -> None:
    case = case_by_key(manifest, "duplicate-reject")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    _approve(client, proposal)
    engine = make_engine(settings.api_database_url)
    factory = make_session_factory(engine)
    with factory() as session:
        ticket = session.get(Ticket, UUID(case["ticket_id"]))
        assert ticket is not None
        append_message(session, ticket=ticket, author_kind="internal", body="trace only")
        session.commit()
    engine.dispose()
    support = support_headers(client)
    current = client.get(f"/tickets/{case['ticket_id']}", headers=support)
    assert current.status_code == 200
    assert current.json()["data"]["proposals"][0]["status"] == "approved"
    posted = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=support,
        json={"body": "The customer withdrew the refund request."},
    )
    assert posted.status_code == 200, posted.text
    withdrawn = posted.json()["data"]
    assert withdrawn["proposals"][0]["status"] == "superseded"
    assert withdrawn["runs"][0]["state"] == "cancelled"
    refused = _execute(client, proposal, approver_headers(client))
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "stale_proposal"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0
    fresh = _investigate(client, case["ticket_id"])["proposals"][-1]
    assert fresh["id"] != proposal["id"]
    assert fresh["status"] == "open"
    unapproved = _execute(client, fresh, approver_headers(client))
    assert unapproved.status_code == 409
    assert unapproved.json()["error_code"] == "not_approved"


def test_pending_ticket_can_resume_after_operator_input(client, manifest) -> None:
    case = case_by_key(manifest, "pending-payment")
    detail = _investigate(client, case["ticket_id"])
    assert detail["runs"][-1]["state"] == "awaiting_customer"
    headers = support_headers(client)
    blocked = client.post(f"/tickets/{case['ticket_id']}/runs", headers=headers)
    assert blocked.status_code == 409
    assert blocked.json()["error_code"] == "run_in_progress"
    posted = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=headers,
        json={"body": "The customer confirmed this charge is still pending."},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["data"]["runs"][-1]["state"] == "cancelled"
    resumed = _investigate(client, case["ticket_id"])
    assert resumed["runs"][-1]["state"] == "awaiting_customer"
    assert len(resumed["runs"]) == 2


def _approver(factory) -> Actor:
    with factory() as session:
        user = session.scalar(
            select(UserAccount).where(UserAccount.email == "approver@demo.resolveai.test")
        )
        assert user is not None
        membership = session.scalar(select(Membership).where(Membership.user_id == user.id))
        assert membership is not None
        return Actor(
            user_id=user.id,
            tenant_id=membership.tenant_id,
            role=membership.role,
            display_name=user.display_name,
            email=user.email,
            session_id=UUID(int=0),
            csrf_hash="",
        )


def test_concurrent_reply_send_returns_one_receipt(
    client, settings, manifest, monkeypatch
) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    headers = _approve(client, proposal)
    executed = _execute(client, proposal, headers)
    assert executed.status_code == 200, executed.text
    reply = executed.json()["data"]["replies"][0]
    engine = make_engine(settings.api_database_url)
    factory = make_session_factory(engine)
    approver = _approver(factory)
    entered: list[int] = []
    gate = threading.Barrier(2)

    def pause() -> None:
        entered.append(threading.get_ident())
        try:
            gate.wait(timeout=1)
        except threading.BrokenBarrierError:
            return

    monkeypatch.setattr("resolveai.services.execution._before_send_receipt", pause)
    errors: list[BaseException] = []
    receipts: list[str] = []
    started = threading.Barrier(2)

    def worker() -> None:
        session = factory()
        try:
            started.wait(timeout=5)
            draft = send_reply(
                session,
                actor=approver,
                reply_id=UUID(reply["id"]),
                revision=reply["revision"],
                body_hash_value=reply["body_hash"],
            )
            receipts.append(str(draft.receipt["receipt_id"]))
        except BaseException as exc:  # noqa: BLE001 - report every worker failure
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    engine.dispose()
    assert errors == []
    assert receipts[0] == receipts[1]
    assert len(entered) == 1
    oracle = make_engine(settings.oracle_database_url)
    with oracle.connect() as connection:
        messages = connection.execute(
            text(
                "SELECT count(*) FROM app.message "
                "WHERE ticket_id = :ticket_id AND author_kind = 'system'"
            ),
            {"ticket_id": case["ticket_id"]},
        ).scalar_one()
    oracle.dispose()
    assert messages == 1


class CountingRefunds(SimulatorClient):
    calls = 0

    def create_refund(self, **kwargs: object) -> ToolResult:
        type(self).calls += 1
        return super().create_refund(**kwargs)


def test_withdrawal_before_dispatch_writes_nothing(
    client, api_app, settings, manifest, monkeypatch, restore_runtime
) -> None:
    del restore_runtime
    case = case_by_key(manifest, "duplicate-approve")
    proposal = _investigate(client, case["ticket_id"])["proposals"][0]
    _approve(client, proposal)
    entered = threading.Event()
    release = threading.Event()

    def pause() -> None:
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr("resolveai.services.execution._before_dispatch", pause)
    CountingRefunds.calls = 0
    api_app.state.tool_client_factory = CountingRefunds
    outcome: dict[str, object] = {}

    def execute() -> None:
        with TestClient(api_app) as worker:
            headers = approver_headers(worker)
            response = worker.post(
                f"/proposals/{proposal['id']}/execute",
                headers=headers,
                json={
                    "revision": proposal["revision"],
                    "payload_hash": proposal["payload_hash"],
                },
            )
            outcome["status"] = response.status_code
            outcome["body"] = response.json()

    thread = threading.Thread(target=execute)
    thread.start()
    assert entered.wait(timeout=10)
    posted = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=support_headers(client),
        json={"body": "The customer withdrew the refund request."},
    )
    assert posted.status_code == 200, posted.text
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["status"] == 409
    assert outcome["body"]["error_code"] == "stale_proposal"
    assert CountingRefunds.calls == 0
    assert _refund_count(settings, proposal["target_payment_id"]) == 0
    detail = client.get(
        f"/tickets/{case['ticket_id']}", headers=support_headers(client)
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()["data"]
    assert payload["proposals"][0]["status"] == "superseded"
    assert payload["runs"][0]["state"] == "cancelled"
    assert payload["actions"][0]["status"] == "pending"
    assert payload["actions"][0]["dispatch_state"] == "reserved"


def _ledger_proposal_id(settings, action_id: str) -> str:
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT proposal_id FROM app.action_ledger WHERE action_id = :action_id"),
            {"action_id": action_id},
        ).scalar_one()
    engine.dispose()
    return str(value)


def test_reused_reservation_stays_with_the_new_approval(
    client, api_app, settings, manifest, monkeypatch, restore_runtime
) -> None:
    del restore_runtime
    case = case_by_key(manifest, "duplicate-approve")
    original = _investigate(client, case["ticket_id"])["proposals"][0]
    _approve(client, original)
    entered = threading.Event()
    release = threading.Event()

    def pause() -> None:
        entered.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr("resolveai.services.execution._before_dispatch", pause)
    CountingRefunds.calls = 0
    api_app.state.tool_client_factory = CountingRefunds
    outcome: dict[str, object] = {}

    def execute_original() -> None:
        with TestClient(api_app) as worker:
            headers = approver_headers(worker)
            response = worker.post(
                f"/proposals/{original['id']}/execute",
                headers=headers,
                json={
                    "revision": original["revision"],
                    "payload_hash": original["payload_hash"],
                },
            )
            outcome["status"] = response.status_code

    thread = threading.Thread(target=execute_original)
    thread.start()
    assert entered.wait(timeout=10)
    withdrawn = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=support_headers(client),
        json={"body": "The customer withdrew the refund request."},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    release.set()
    thread.join(timeout=10)
    assert outcome["status"] == 409
    assert CountingRefunds.calls == 0
    monkeypatch.undo()
    api_app.state.tool_client_factory = SimulatorClient

    fresh = _investigate(client, case["ticket_id"])["proposals"][-1]
    assert fresh["id"] != original["id"]
    assert fresh["status"] == "open"
    headers = _approve(client, fresh)
    executed = _execute(client, fresh, headers)
    assert executed.status_code == 200, executed.text
    assert _refund_count(settings, fresh["target_payment_id"]) == 1
    assert _ledger_proposal_id(settings, fresh["action_id"]) == fresh["id"]
    reconciled = client.post(
        f"/actions/{fresh['action_id']}/reconcile",
        headers=approver_headers(client),
    )
    assert reconciled.status_code == 200, reconciled.text
    detail = reconciled.json()["data"]
    old = next(item for item in detail["proposals"] if item["id"] == original["id"])
    new = next(item for item in detail["proposals"] if item["id"] == fresh["id"])
    assert old["status"] == "superseded"
    assert new["status"] == "executed"
    assert detail["runs"][0]["state"] == "cancelled"
    assert detail["runs"][-1]["state"] == "awaiting_reply_review"
    assert _refund_count(settings, fresh["target_payment_id"]) == 1
    assert _ledger_proposal_id(settings, fresh["action_id"]) == fresh["id"]
