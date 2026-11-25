"""Same-ticket refund and period-end cancellation. No paid model calls."""

from __future__ import annotations

from sqlalchemy import text
from tests.support import approver_headers, support_headers
from tests.test_cancellation import ONE, _investigate

from resolveai.db.session import make_engine

COMBINED = "55555555-5555-4555-8555-555555555527"


def test_one_ticket_proposes_both_actions(client) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    kinds = {item["action_type"] for item in detail["proposals"]}
    assert kinds == {"refund", "cancel_at_period_end"}
    codes = {item["half"]: item["outcome_code"] for item in detail["combined"]["halves"]}
    assert codes == {"refund": "open", "cancel_at_period_end": "open"}
    assert detail["replies"] == [] or all(item["status"] != "draft" for item in detail["replies"])


def test_confirming_one_action_leaves_the_other_open(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    approver = approver_headers(client)
    body = {"revision": refund["revision"], "payload_hash": refund["payload_hash"]}
    assert (
        client.post(f"/proposals/{refund['id']}/approve", headers=approver, json=body).status_code
        == 200
    )
    assert (
        client.post(f"/proposals/{refund['id']}/execute", headers=approver, json=body).status_code
        == 200
    )
    from resolveai.worker.__main__ import drain

    drain(settings, limit=5)
    current = client.get(f"/tickets/{COMBINED}", headers=support_headers(client))
    assert current.status_code == 200, current.text
    data = current.json()["data"]
    codes = {item["half"]: item["outcome_code"] for item in data["combined"]["halves"]}
    assert codes["refund"] == "confirmed"
    assert codes["cancel_at_period_end"] == "open"
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        scheduled = connection.execute(
            text(
                """
                SELECT cancel_at_period_end FROM simulator.subscription
                WHERE id = '99999999-9999-4999-8999-999999999910'
                """
            )
        ).scalar_one()
    engine.dispose()
    assert scheduled is False


def test_both_confirmed_replies_name_both_receipts(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
        assert (
            client.post(
                f"/proposals/{proposal['id']}/approve", headers=approver, json=body
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/proposals/{proposal['id']}/execute", headers=approver, json=body
            ).status_code
            == 200
        )
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver)
    data = current.json()["data"]
    codes = {item["half"]: item["outcome_code"] for item in data["combined"]["halves"]}
    assert codes == {"refund": "confirmed", "cancel_at_period_end": "confirmed"}
    draft = next(item for item in data["replies"] if item["status"] == "draft")
    refund_receipt = next(
        item["operation_id"] for item in data["actions"] if item["action_type"] == "refund"
    )
    cancel_receipt = next(
        item["operation_id"]
        for item in data["actions"]
        if item["action_type"] == "cancel_at_period_end"
    )
    assert refund_receipt in draft["body"]
    assert cancel_receipt in draft["body"]
    assert "$19.00" in draft["body"]
    assert "Cancellation scheduled for" in draft["body"]


CHOICE = "55555555-5555-4555-8555-555555555528"
INELIGIBLE = "55555555-5555-4555-8555-555555555529"
HARBOR = "55555555-5555-4555-8555-555555555532"
KNOWN = "55555555-5555-4555-8555-555555555541"
SUBSCRIPTION = "99999999-9999-4999-8999-999999999910"
HARBOR_SUB = "99999999-9999-4999-8999-999999999915"
NORTHWIND = "11111111-1111-4111-8111-111111111111"


def test_choice_ineligible_and_the_other_tenant_stay_independent(client) -> None:
    from tests.support import login

    choice = _investigate(client, CHOICE, support_headers(client))
    codes = _codes(choice)
    assert codes["refund"] == "open"
    assert codes["cancel_at_period_end"] == "waiting"
    assert choice["ticket"]["status"] == "awaiting_approval"
    assert choice["proposals"][0]["action_type"] == "refund"
    assert all(item["status"] != "draft" for item in choice["replies"])
    waiting = next(item for item in choice["combined"]["halves"] if item["half"] != "refund")
    assert waiting["reason_code"] == "choice_required"
    assert waiting["outcome_id"]

    missed = _investigate(client, INELIGIBLE, support_headers(client))
    assert missed["proposals"] == []
    assert _codes(missed) == {
        "refund": "not_executed",
        "cancel_at_period_end": "not_executed",
    }
    reasons = {item["half"]: item["reason_code"] for item in missed["combined"]["halves"]}
    assert reasons == {"refund": "no_duplicate", "cancel_at_period_end": "not_eligible"}
    assert missed["ticket"]["status"] == "open"
    assert missed["runs"][-1]["state"] == "completed"
    assert all(item["action_id"] is None for item in missed["combined"]["halves"])

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate(client, HARBOR, harbor_support)
    assert {item["action_type"] for item in harbor["proposals"]} == {
        "refund",
        "cancel_at_period_end",
    }
    assert client.get(f"/tickets/{HARBOR}", headers=support_headers(client)).status_code == 404
    incident = client.post(
        f"/tickets/{KNOWN}/incident-report",
        headers=support_headers(client),
        json={
            "service_code": "api",
            "region_code": "us-east",
            "reported_started_at": "2026-10-15T00:00:00.000000Z",
            "request_id": "REQ-1",
        },
    )
    assert incident.status_code == 200, incident.text
    known = _investigate(client, KNOWN, support_headers(client))
    assert known["combined"]["active"] is False
    assert known["combined"]["halves"] == []
    single = _investigate(client, ONE, support_headers(client))
    assert single["combined"]["active"] is False


def test_b1_proposes_both_halves(client, api_app, restore_runtime) -> None:
    del restore_runtime
    from resolveai.evaluation.catalog import use_baseline

    use_baseline(api_app)
    detail = _investigate(client, COMBINED, support_headers(client))
    assert {item["action_type"] for item in detail["proposals"]} == {
        "refund",
        "cancel_at_period_end",
    }
    assert _codes(detail) == {"refund": "open", "cancel_at_period_end": "open"}


def test_queued_attempts_stay_in_flight_until_a_worker_finishes(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        assert _execute(client, proposal, approver).status_code == 200
    queued = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(queued) == {"refund": "in_flight", "cancel_at_period_end": "in_flight"}
    assert all(item["status"] != "draft" for item in queued["replies"])
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    before = _versions(settings, COMBINED, "refund")
    again = client.post(
        f"/proposals/{refund['id']}/execute",
        headers=approver,
        json={"revision": refund["revision"], "payload_hash": refund["payload_hash"]},
    )
    assert again.status_code == 200, again.text
    assert _versions(settings, COMBINED, "refund") == before
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    done = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(done) == {"refund": "confirmed", "cancel_at_period_end": "confirmed"}


def test_cancellation_can_confirm_before_the_refund(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    _execute(client, cancel, approver)
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    midway = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(midway)["cancel_at_period_end"] == "confirmed"
    assert _codes(midway)["refund"] == "open"
    assert _scheduled(settings, SUBSCRIPTION) is True
    _execute(client, refund, approver)
    drain(settings, limit=4)
    done = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(done) == {"refund": "confirmed", "cancel_at_period_end": "confirmed"}
    from tests.test_auth_and_workflow import _refund_count

    assert _refund_count(settings, refund["target_payment_id"]) == 1


def test_rejecting_one_proposal_keeps_the_confirmed_sibling(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    _execute(client, refund, approver)
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    rejected = client.post(
        f"/proposals/{cancel['id']}/reject",
        headers=approver,
        json={"revision": cancel["revision"], "payload_hash": cancel["payload_hash"]},
    )
    assert rejected.status_code == 200, rejected.text
    data = rejected.json()["data"]
    assert _codes(data) == {"refund": "confirmed", "cancel_at_period_end": "not_executed"}
    reasons = {item["half"]: item["reason_code"] for item in data["combined"]["halves"]}
    assert reasons["cancel_at_period_end"] == "rejected"
    draft = next(item for item in data["replies"] if item["status"] == "draft")
    assert "Refund reference:" in draft["body"]
    assert "not executed (rejected)" in draft["body"]
    assert _scheduled(settings, SUBSCRIPTION) is False


def test_expiry_and_revocation_stop_only_that_half(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    approved = client.post(
        f"/proposals/{refund['id']}/approve",
        headers=approver,
        json={"revision": refund["revision"], "payload_hash": refund["payload_hash"]},
    )
    assert approved.status_code == 200, approved.text
    grant = next(
        item["approval"]["id"]
        for item in approved.json()["data"]["proposals"]
        if item["id"] == refund["id"]
    )
    _expire(settings, grant)
    expired = client.post(
        f"/proposals/{refund['id']}/execute",
        headers=approver,
        json={"revision": refund["revision"], "payload_hash": refund["payload_hash"]},
    )
    assert expired.status_code == 409
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(current)["refund"] == "not_executed"
    assert _codes(current)["cancel_at_period_end"] == "open"
    approved_cancel = client.post(
        f"/proposals/{cancel['id']}/approve",
        headers=approver,
        json={"revision": cancel["revision"], "payload_hash": cancel["payload_hash"]},
    )
    assert approved_cancel.status_code == 200, approved_cancel.text
    cancel_grant = next(
        item["approval"]["id"]
        for item in approved_cancel.json()["data"]["proposals"]
        if item["id"] == cancel["id"]
    )
    revoked = client.post(
        f"/approvals/{cancel_grant}/revoke",
        headers=approver,
        json={"reason": "The customer asked to keep the plan"},
    )
    assert revoked.status_code == 200, revoked.text
    data = revoked.json()["data"]
    assert _codes(data)["cancel_at_period_end"] == "not_executed"
    reasons = {item["half"]: item["reason_code"] for item in data["combined"]["halves"]}
    assert reasons["cancel_at_period_end"] == "approval_revoked"
    assert _scheduled(settings, SUBSCRIPTION) is False
    from tests.test_auth_and_workflow import _refund_count

    assert _refund_count(settings, refund["target_payment_id"]) == 0


def test_stale_version_and_elapsed_period_are_not_unavailable(client, settings) -> None:
    from tests.support import login

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    _execute(client, refund, approver)
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    _execute(client, cancel, approver)
    _bump(settings, SUBSCRIPTION)
    drain(settings, limit=4)
    data = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(data)["cancel_at_period_end"] == "not_executed"
    reasons = {item["half"]: item["reason_code"] for item in data["combined"]["halves"]}
    assert reasons["cancel_at_period_end"] == "version_conflict"
    assert "unavailable" not in data["replies"][-1]["body"]
    assert _scheduled(settings, SUBSCRIPTION) is False

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate(client, HARBOR, harbor_support)
    harbor_approver = login(client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass")
    harbor_refund = next(item for item in harbor["proposals"] if item["action_type"] == "refund")
    harbor_cancel = next(
        item for item in harbor["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    _execute(client, harbor_refund, harbor_approver)
    drain(settings, limit=4)
    _execute(client, harbor_cancel, harbor_approver)
    _shift(settings, HARBOR_SUB)
    drain(settings, limit=4)
    harbor_detail = client.get(f"/tickets/{HARBOR}", headers=harbor_approver).json()["data"]
    assert _codes(harbor_detail)["cancel_at_period_end"] == "not_executed"
    harbor_reasons = {
        item["half"]: item["reason_code"] for item in harbor_detail["combined"]["halves"]
    }
    assert harbor_reasons["cancel_at_period_end"] == "period_elapsed"
    assert _scheduled(settings, HARBOR_SUB) is False


def test_a_blocked_read_can_be_enqueued_again(client, settings, monkeypatch) -> None:
    detail = _prepare_blocked_read(client, settings, monkeypatch)
    approver = approver_headers(client)
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    monkeypatch.undo()
    again = client.post(
        f"/proposals/{cancel['id']}/execute",
        headers=approver,
        json={"revision": cancel["revision"], "payload_hash": cancel["payload_hash"]},
    )
    assert again.status_code == 200, again.text
    jobs = _jobs(settings, COMBINED)
    assert [item["state"] for item in jobs if item["action"] == "cancel_at_period_end"] == [
        "blocked",
        "queued",
    ]
    assert jobs[-1]["block_reason"] is None
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(current)["cancel_at_period_end"] == "in_flight"
    versions = _versions(settings, COMBINED, "cancel_at_period_end")
    assert "not_dispatched" in versions
    assert versions[-1] == "in_flight"


def test_new_input_while_blocked_does_not_reuse_the_old_approval(
    client, settings, monkeypatch
) -> None:
    detail = _prepare_blocked_read(client, settings, monkeypatch)
    refund_action = next(
        item["action_id"] for item in detail["proposals"] if item["action_type"] == "refund"
    )
    posted = client.post(
        f"/tickets/{COMBINED}/messages",
        headers=support_headers(client),
        json={"body": "The customer added a later note."},
    )
    assert posted.status_code == 200, posted.text
    data = posted.json()["data"]
    assert _codes(data)["refund"] == "confirmed"
    assert _codes(data)["cancel_at_period_end"] == "not_executed"
    reasons = {item["half"]: item["reason_code"] for item in data["combined"]["halves"]}
    assert reasons["cancel_at_period_end"] == "input_changed"
    assert next(
        item["action_id"] for item in data["combined"]["halves"] if item["half"] == "refund"
    ) == (refund_action)
    assert all(item["status"] != "draft" for item in data["replies"]) or any(
        item["status"] == "superseded" for item in data["replies"]
    )
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    stale = client.post(
        f"/proposals/{cancel['id']}/execute",
        headers=approver_headers(client),
        json={"revision": cancel["revision"], "payload_hash": cancel["payload_hash"]},
    )
    assert stale.status_code == 409
    assert _scheduled(settings, SUBSCRIPTION) is False


def test_lost_responses_stay_uncertain_on_the_original_action(
    client, settings, monkeypatch
) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    _execute(client, cancel, approver)
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    approved = client.post(
        f"/proposals/{refund['id']}/approve",
        headers=approver,
        json={"revision": refund["revision"], "payload_hash": refund["payload_hash"]},
    )
    assert approved.status_code == 200, approved.text
    lost = client.post(
        f"/proposals/{refund['id']}/execute",
        headers={**approver, "X-ResolveAI-Test-Fault": "commit_then_fail"},
        json={"revision": refund["revision"], "payload_hash": refund["payload_hash"]},
    )
    assert lost.status_code == 200, lost.text
    _set_max_attempts(settings, COMBINED, 1)
    _fail_operation_reads(monkeypatch)
    drain(settings, limit=3)
    data = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(data)["refund"] == "uncertain"
    assert _codes(data)["cancel_at_period_end"] == "confirmed"
    draft = next(item for item in data["replies"] if item["status"] == "draft")
    assert f"The refund {refund['action_id']} is not confirmed." in draft["body"]
    assert "Cancellation scheduled for" in draft["body"]
    from tests.test_auth_and_workflow import _refund_count

    assert _refund_count(settings, refund["target_payment_id"]) == 1
    monkeypatch.undo()

    from tests.support import login

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate(client, HARBOR, harbor_support)
    harbor_approver = login(client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass")
    harbor_refund = next(item for item in harbor["proposals"] if item["action_type"] == "refund")
    harbor_cancel = next(
        item for item in harbor["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    _execute(client, harbor_refund, harbor_approver)
    drain(settings, limit=4)
    _execute(client, harbor_cancel, harbor_approver)
    _set_max_attempts(settings, HARBOR, 1)
    _fail_after_schedule(monkeypatch)
    drain(settings, limit=3)
    harbor_detail = client.get(f"/tickets/{HARBOR}", headers=harbor_approver).json()["data"]
    assert _codes(harbor_detail)["cancel_at_period_end"] == "uncertain"
    assert _scheduled(settings, HARBOR_SUB) is True
    harbor_draft = next(item for item in harbor_detail["replies"] if item["status"] == "draft")
    assert "is not confirmed" in harbor_draft["body"]
    assert "not executed" not in harbor_draft["body"]


def test_two_workers_confirm_each_action_once(client, settings) -> None:
    import threading

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        _execute(client, proposal, approver)
    from resolveai.worker.__main__ import drain

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _run() -> None:
        try:
            barrier.wait(timeout=5)
            drain(settings, limit=6)
        except BaseException as exc:  # noqa: BLE001 - the join reports the worker error
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert errors == []
    data = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(data) == {"refund": "confirmed", "cancel_at_period_end": "confirmed"}
    drafts = [item for item in data["replies"] if item["status"] == "draft"]
    assert len(drafts) == 1
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    from tests.test_auth_and_workflow import _refund_count

    assert _refund_count(settings, refund["target_payment_id"]) == 1
    assert _scheduled(settings, SUBSCRIPTION) is True
    assert _write_attempts(settings, refund["action_id"]) == 1
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    assert _write_attempts(settings, cancel["action_id"]) == 1


def test_send_and_reject_follow_the_current_binding(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        _execute(client, proposal, approver)
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    first = next(item for item in current["replies"] if item["status"] == "draft")
    sent = _send(client, first, approver)
    assert sent.status_code == 200, sent.text
    receipt = sent.json()["data"]["replies"][-1]["receipt"]["receipt_id"]
    repeated = _send(client, first, approver)
    assert repeated.json()["data"]["replies"][-1]["receipt"]["receipt_id"] == receipt
    posted = client.post(
        f"/tickets/{COMBINED}/messages",
        headers=support_headers(client),
        json={"body": "A later operator note."},
    )
    assert posted.status_code == 200, posted.text
    after = posted.json()["data"]
    repeated_after = _send(client, first, approver_headers(client))
    assert repeated_after.status_code == 200, repeated_after.text
    sent_again = next(
        item for item in repeated_after.json()["data"]["replies"] if item["id"] == first["id"]
    )
    assert sent_again["receipt"]["receipt_id"] == receipt
    newer = next(item for item in after["replies"] if item["status"] == "draft")
    assert newer["id"] != first["id"]
    historical = client.post(
        f"/reply-drafts/{first['id']}/reject",
        headers=approver_headers(client),
        json={"revision": first["revision"], "body_hash": first["body_hash"]},
    )
    assert historical.status_code == 409
    kept = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert next(item["id"] for item in kept["replies"] if item["status"] == "draft") == newer["id"]
    assert kept["ticket"]["status"] == "awaiting_reply"


def test_rejecting_the_current_draft_writes_the_next_revision(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        _execute(client, proposal, approver)
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    first = next(item for item in current["replies"] if item["status"] == "draft")
    outcome_ids = {item["half"]: item["outcome_id"] for item in current["combined"]["halves"]}
    rejected = client.post(
        f"/reply-drafts/{first['id']}/reject",
        headers=approver,
        json={"revision": first["revision"], "body_hash": first["body_hash"]},
    )
    assert rejected.status_code == 200, rejected.text
    data = rejected.json()["data"]
    nxt = next(item for item in data["replies"] if item["status"] == "draft")
    assert nxt["id"] != first["id"]
    assert {item["half"]: item["outcome_id"] for item in data["combined"]["halves"]} == outcome_ids
    assert data["ticket"]["status"] == "awaiting_reply"
    again = client.post(
        f"/reply-drafts/{first['id']}/reject",
        headers=approver,
        json={"revision": first["revision"], "body_hash": first["body_hash"]},
    )
    assert again.status_code == 200, again.text
    kept = again.json()["data"]
    assert next(item["id"] for item in kept["replies"] if item["status"] == "draft") == nxt["id"]


def test_runtime_roles_cannot_rewrite_combined_history(client, settings) -> None:
    import pytest
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    _investigate(client, COMBINED, support_headers(client))
    api = make_engine(settings.api_database_url)
    worker = make_engine(settings.worker_database_url)
    auth = make_engine(settings.auth_database_url)
    try:
        with pytest.raises(DBAPIError):
            with api.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": NORTHWIND},
                )
                connection.execute(text("UPDATE app.combined_half SET reason_code = reason_code"))
        with pytest.raises(DBAPIError):
            with worker.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": NORTHWIND},
                )
                connection.execute(text("DELETE FROM app.combined_half"))
        with worker.connect() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": NORTHWIND},
            )
            visible = connection.execute(
                text("SELECT count(*) FROM app.combined_half")
            ).scalar_one()
        assert int(visible) >= 2
        with pytest.raises(ProgrammingError):
            with auth.connect() as connection:
                connection.execute(text("SELECT count(*) FROM app.combined_half"))
    finally:
        api.dispose()
        worker.dispose()
        auth.dispose()


def test_ordinary_expiry_does_not_write_combined_history(client, settings, manifest) -> None:
    from tests.support import case_by_key

    refund_ticket = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    _assert_ordinary_expiry_is_not_combined(client, settings, refund_ticket, "refund")
    _assert_ordinary_expiry_is_not_combined(client, settings, ONE, "cancel_at_period_end")


def test_ordinary_financial_history_can_drop_and_reapply_combined_storage(
    client, settings, manifest
) -> None:
    import os
    import subprocess
    import sys

    from tests.support import case_by_key

    from resolveai.settings import repo_root

    ticket_id = case_by_key(manifest, "duplicate-approve")["ticket_id"]
    detail = _investigate(client, ticket_id, support_headers(client))
    proposal = detail["proposals"][0]
    approver = approver_headers(client)
    assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    confirmed = client.get(f"/tickets/{ticket_id}", headers=approver).json()["data"]
    assert confirmed["combined"]["active"] is False
    assert _combined_table_counts(settings) == (0, 0)
    action_id = proposal["action_id"]
    env = os.environ.copy()
    env.update({"DATABASE_URL": settings.database_url, "PYTHONPATH": str(repo_root() / "src")})
    try:
        removed = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "20261106_0011"],
            cwd=repo_root(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert removed.returncode == 0, removed.stderr
        engine = make_engine(settings.database_url)
        with engine.connect() as connection:
            kept = connection.execute(
                text(
                    """
                    SELECT l.status, l.operation_id
                    FROM app.action_ledger AS l
                    WHERE l.action_id = :action_id
                    """
                ),
                {"action_id": action_id},
            ).one()
        engine.dispose()
        assert kept.status == "confirmed"
        assert kept.operation_id
    finally:
        restored = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=repo_root(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    assert restored.returncode == 0, restored.stderr
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        ledger = connection.execute(
            text("SELECT status FROM app.action_ledger WHERE action_id = :action_id"),
            {"action_id": action_id},
        ).scalar_one()
    engine.dispose()
    assert version == "20261112_0014"
    assert ledger == "confirmed"
    assert _combined_table_counts(settings) == (0, 0)


def test_later_combined_input_can_record_outcomes(client, settings) -> None:
    detail = _investigate(client, ONE, support_headers(client))
    proposal = detail["proposals"][-1]
    approver = approver_headers(client)
    assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    current = client.get(f"/tickets/{ONE}", headers=approver).json()["data"]
    draft = next(item for item in current["replies"] if item["status"] == "draft")
    sent = _send(client, draft, approver)
    assert sent.status_code == 200, sent.text
    posted = client.post(
        f"/tickets/{ONE}/messages",
        headers=support_headers(client),
        json={"body": "Please refund the duplicate and schedule cancellation at period end."},
    )
    assert posted.status_code == 200, posted.text
    again = _investigate(client, ONE, support_headers(client))
    assert again["combined"]["active"] is True
    assert _codes(again)["cancel_at_period_end"] == "confirmed"
    assert _combined_table_counts(settings)[0] >= 2
    assert _scheduled(settings, "99999999-9999-4999-8999-999999999901") is True


def _assert_ordinary_expiry_is_not_combined(
    client, settings, ticket_id: str, action_type: str
) -> None:
    detail = _investigate(client, ticket_id, support_headers(client))
    proposal = next(item for item in detail["proposals"] if item["action_type"] == action_type)
    approver = approver_headers(client)
    assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    before = client.get(f"/tickets/{ticket_id}", headers=approver).json()["data"]
    assert before["combined"]["active"] is False
    assert _combined_table_counts(settings) == (0, 0)
    operation_id = next(
        item["operation_id"]
        for item in before["actions"]
        if item["action_id"] == proposal["action_id"]
    )
    assert operation_id
    snapshot = (
        before["ticket"]["status"],
        before["runs"][-1]["state"],
        [(item["id"], item["status"]) for item in before["replies"]],
    )
    stored = next(item for item in before["proposals"] if item["id"] == proposal["id"])
    _expire(settings, stored["approval"]["id"])
    repeated = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver_headers(client),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert repeated.status_code == 200, repeated.text
    after = client.get(f"/tickets/{ticket_id}", headers=approver_headers(client)).json()["data"]
    assert after["combined"]["active"] is False
    assert after["combined"]["halves"] == []
    assert _combined_table_counts(settings) == (0, 0)
    assert (
        next(
            item["operation_id"]
            for item in after["actions"]
            if item["action_id"] == proposal["action_id"]
        )
        == operation_id
    )
    assert (
        after["ticket"]["status"],
        after["runs"][-1]["state"],
        [(item["id"], item["status"]) for item in after["replies"]],
    ) == snapshot


def _combined_table_counts(settings) -> tuple[int, int]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        halves = connection.execute(text("SELECT count(*) FROM app.combined_half")).scalar_one()
        replies = connection.execute(text("SELECT count(*) FROM app.combined_reply")).scalar_one()
    engine.dispose()
    return int(halves), int(replies)


def test_populated_combined_history_refuses_downgrade(client, settings) -> None:
    import os
    import subprocess
    import sys

    from resolveai.settings import repo_root

    _investigate(client, COMBINED, support_headers(client))
    env = os.environ.copy()
    env.update({"DATABASE_URL": settings.database_url, "PYTHONPATH": str(repo_root() / "src")})
    refused = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20261106_0011"],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        rows = connection.execute(text("SELECT count(*) FROM app.combined_half")).scalar_one()
    engine.dispose()
    assert version == "20261112_0014"
    assert int(rows) >= 2


def test_reinvestigation_keeps_the_confirmed_effect(client, settings) -> None:
    from tests.support import login
    from tests.test_auth_and_workflow import _refund_count

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    assert _execute(client, refund, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    posted = client.post(
        f"/tickets/{COMBINED}/messages",
        headers=support_headers(client),
        json={"body": "Please look at the request again."},
    )
    assert posted.status_code == 200, posted.text
    again = _investigate(client, COMBINED, support_headers(client))
    assert _codes(again)["refund"] == "confirmed"
    assert any(
        item["action_type"] == "cancel_at_period_end" and item["status"] != "superseded"
        for item in again["proposals"]
    )
    assert _refund_count(settings, refund["target_payment_id"]) == 1

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate(client, HARBOR, harbor_support)
    harbor_approver = login(client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass")
    harbor_cancel = next(
        item for item in harbor["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    assert _execute(client, harbor_cancel, harbor_approver).status_code == 200
    drain(settings, limit=4)
    harbor_note = client.post(
        f"/tickets/{HARBOR}/messages",
        headers=login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass"),
        json={"body": "Please look at the request again."},
    )
    assert harbor_note.status_code == 200, harbor_note.text
    harbor_again = _investigate(
        client,
        HARBOR,
        login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass"),
    )
    assert _codes(harbor_again)["cancel_at_period_end"] == "confirmed"
    assert any(
        item["action_type"] == "refund" and item["status"] != "superseded"
        for item in harbor_again["proposals"]
    )
    assert _scheduled(settings, HARBOR_SUB) is True


def test_input_after_admission_keeps_the_command(client, settings) -> None:
    import threading

    from resolveai.services import recovery

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    assert _execute(client, refund, approver).status_code == 200
    support = support_headers(client)
    started = threading.Event()
    release = threading.Event()

    def pause() -> None:
        started.set()
        assert release.wait(timeout=8)

    recovery.holds["after_admission"] = pause
    errors: list[BaseException] = []

    def work() -> None:
        try:
            from resolveai.worker.__main__ import drain

            drain(settings, limit=4)
        except BaseException as exc:  # noqa: BLE001 - the join reports it
            errors.append(exc)

    worker = threading.Thread(target=work)
    try:
        worker.start()
        assert started.wait(timeout=8)
        response = client.post(
            f"/tickets/{COMBINED}/messages",
            headers=support,
            json={"body": "A note arrived after the command was admitted."},
        )
        assert response.status_code == 200, response.text
        assert _codes(response.json()["data"])["refund"] == "uncertain"
        release.set()
        worker.join(timeout=8)
    finally:
        release.set()
        recovery.holds.pop("after_admission", None)
    assert errors == []
    assert not worker.is_alive()
    finished = client.get(f"/tickets/{COMBINED}", headers=approver_headers(client)).json()["data"]
    assert _codes(finished)["refund"] == "confirmed"
    assert _write_attempts(settings, refund["action_id"]) == 1


def test_an_expired_grant_does_not_erase_a_confirmed_effect(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    draft = next(item for item in current["replies"] if item["status"] == "draft")
    sent = _send(client, draft, approver)
    assert sent.status_code == 200, sent.text
    receipt = next(
        item["receipt"]["receipt_id"]
        for item in sent.json()["data"]["replies"]
        if item["id"] == draft["id"]
    )
    versions_before = _half_count(settings, COMBINED)
    for proposal in current["proposals"]:
        if proposal["status"] == "superseded" or proposal["approval"] is None:
            continue
        _expire(settings, proposal["approval"]["id"])
        repeated = client.post(
            f"/proposals/{proposal['id']}/execute",
            headers=approver_headers(client),
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        )
        assert repeated.status_code in {200, 409}
    after = client.get(f"/tickets/{COMBINED}", headers=approver_headers(client)).json()["data"]
    assert _codes(after) == {"refund": "confirmed", "cancel_at_period_end": "confirmed"}
    assert after["ticket"]["status"] == "reply_sent"
    assert after["runs"][-1]["state"] == "completed"
    assert [item["status"] for item in after["replies"]] == ["sent"]
    assert _half_count(settings, COMBINED) == versions_before
    resent = _send(client, draft, approver_headers(client))
    assert resent.status_code == 200, resent.text
    assert (
        next(
            item["receipt"]["receipt_id"]
            for item in resent.json()["data"]["replies"]
            if item["id"] == draft["id"]
        )
        == receipt
    )
    followed = client.post(
        f"/tickets/{COMBINED}/messages",
        headers=support_headers(client),
        json={"body": "A later note after the sent reply."},
    )
    assert followed.status_code == 200, followed.text
    history = followed.json()["data"]["replies"]
    assert [item["status"] for item in history] == ["sent", "draft"]
    assert history[0]["receipt"]["receipt_id"] == receipt


def test_combined_reply_locks_the_ticket_first(client, api_app, settings, monkeypatch) -> None:
    import threading

    from starlette.testclient import TestClient
    from tests.support import login

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    draft = next(item for item in current["replies"] if item["status"] == "draft")
    approver = approver_headers(client)
    support_client = TestClient(api_app)
    support = login(support_client, "support@demo.resolveai.test", "support-demo-pass")
    held = threading.Event()
    release = threading.Event()

    def pause() -> None:
        held.set()
        assert release.wait(timeout=8)

    monkeypatch.setattr("resolveai.services.execution._after_combined_ticket_lock", pause)
    outcome: dict[str, object] = {}

    def send() -> None:
        try:
            outcome["send"] = _send(client, draft, approver)
        except BaseException as exc:  # noqa: BLE001
            outcome["send_error"] = exc

    def post() -> None:
        assert held.wait(timeout=8)
        outcome["input"] = support_client.post(
            f"/tickets/{COMBINED}/messages",
            headers=support,
            json={"body": "Input while the reply is being sent."},
        )

    sender = threading.Thread(target=send)
    writer = threading.Thread(target=post)
    sender.start()
    writer.start()
    try:
        assert held.wait(timeout=8)
        assert _wait_for_ticket_lock(settings)
    finally:
        release.set()
        sender.join(timeout=8)
        writer.join(timeout=8)
    assert "send_error" not in outcome
    assert outcome["send"].status_code == 200, outcome["send"].text
    assert outcome["input"].status_code == 200, outcome["input"].text


def test_send_and_reject_wait_on_the_same_ticket(client, api_app, settings, monkeypatch) -> None:
    import threading

    from starlette.testclient import TestClient
    from tests.support import login

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    draft = next(item for item in current["replies"] if item["status"] == "draft")
    approver = approver_headers(client)
    reject_client = TestClient(api_app)
    rejector = login(reject_client, "approver@demo.resolveai.test", "approver-demo-pass")
    calls = {"n": 0}
    held = threading.Event()
    release = threading.Event()

    def pause() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            held.set()
            assert release.wait(timeout=8)

    monkeypatch.setattr("resolveai.services.execution._after_combined_ticket_lock", pause)
    outcome: dict[str, object] = {}

    def reject() -> None:
        outcome["reject"] = reject_client.post(
            f"/reply-drafts/{draft['id']}/reject",
            headers=rejector,
            json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
        )

    def send() -> None:
        assert held.wait(timeout=8)
        outcome["send"] = _send(client, draft, approver)

    first = threading.Thread(target=reject)
    second = threading.Thread(target=send)
    first.start()
    try:
        assert held.wait(timeout=8)
        second.start()
        assert _wait_for_ticket_lock(settings)
    finally:
        release.set()
        first.join(timeout=8)
        if second.ident is not None:
            second.join(timeout=8)
    assert outcome["reject"].status_code == 200, outcome["reject"].text
    assert outcome["send"].status_code == 409
    stored = _reply_rows(settings, COMBINED)
    assert [item["status"] for item in stored] == ["rejected", "draft"]
    assert _event_count(settings, COMBINED, "reply_rejected") == 1


def test_send_first_keeps_the_sent_reply(client, api_app, settings, monkeypatch) -> None:
    import threading

    from starlette.testclient import TestClient
    from tests.support import login
    from tests.test_auth_and_workflow import _refund_count

    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    draft = next(item for item in current["replies"] if item["status"] == "draft")
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    approver = approver_headers(client)
    reject_client = TestClient(api_app)
    rejector = login(reject_client, "approver@demo.resolveai.test", "approver-demo-pass")
    held = threading.Event()
    release = threading.Event()

    def pause() -> None:
        held.set()
        assert release.wait(timeout=8)

    monkeypatch.setattr("resolveai.services.execution._after_combined_ticket_lock", pause)
    outcome: dict[str, object] = {}

    def send() -> None:
        outcome["send"] = _send(client, draft, approver)

    def reject() -> None:
        assert held.wait(timeout=8)
        outcome["reject"] = reject_client.post(
            f"/reply-drafts/{draft['id']}/reject",
            headers=rejector,
            json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
        )

    sender = threading.Thread(target=send)
    rejector_thread = threading.Thread(target=reject)
    sender.start()
    try:
        assert held.wait(timeout=8)
        rejector_thread.start()
        assert _wait_for_ticket_lock(settings)
    finally:
        release.set()
        sender.join(timeout=8)
        if rejector_thread.ident is not None:
            rejector_thread.join(timeout=8)
    assert outcome["send"].status_code == 200, outcome["send"].text
    assert outcome["reject"].status_code == 409, outcome["reject"].text
    sent = outcome["send"].json()["data"]
    receipt = next(
        item["receipt"]["receipt_id"] for item in sent["replies"] if item["id"] == draft["id"]
    )
    stored = _reply_rows(settings, COMBINED)
    assert stored == [{"id": draft["id"], "status": "sent", "receipt_id": receipt}]
    assert _event_count(settings, COMBINED, "reply_rejected") == 0
    assert (
        client.get(f"/tickets/{COMBINED}", headers=approver_headers(client)).json()["data"][
            "ticket"
        ]["status"]
        == "reply_sent"
    )
    assert _refund_count(settings, refund["target_payment_id"]) == 1
    assert _scheduled(settings, SUBSCRIPTION) is True


def test_immediate_language_waits_for_a_later_choice(client, api_app, monkeypatch) -> None:
    from resolveai.agent.provider import FakeProvider, ProviderAction

    same = client.post(
        f"/tickets/{COMBINED}/messages",
        headers=support_headers(client),
        json={"body": "Please refund the duplicate charge and terminate immediately."},
    )
    assert same.status_code == 200, same.text
    detail = _investigate(client, COMBINED, support_headers(client))
    assert any(item["action_type"] == "refund" for item in detail["proposals"])
    assert all(item["action_type"] != "cancel_at_period_end" for item in detail["proposals"])
    assert _codes(detail)["cancel_at_period_end"] == "waiting"
    assert _codes(detail)["refund"] == "open"

    from tests.support import login

    harbor_support = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    split = client.post(
        f"/tickets/{HARBOR}/messages",
        headers=harbor_support,
        json={"body": "Please terminate immediately."},
    )
    assert split.status_code == 200, split.text
    harbor = _investigate(client, HARBOR, harbor_support)
    assert _codes(harbor)["cancel_at_period_end"] == "waiting"
    chosen = client.post(
        f"/tickets/{HARBOR}/subscription-choice",
        headers=login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass"),
        json={"subscription_id": HARBOR_SUB},
    )
    assert chosen.status_code == 200, chosen.text
    resumed = _investigate(
        client,
        HARBOR,
        login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass"),
    )
    assert any(item["action_type"] == "cancel_at_period_end" for item in resumed["proposals"])

    class ForcedCancellation(FakeProvider):
        def __init__(self) -> None:
            self.step = 0

        def next_action(self, snapshot: object) -> ProviderAction:
            del snapshot
            self.step += 1
            if self.step == 1:
                return ProviderAction(
                    route="tool", tool_name="list_subscriptions", reason="Read subscriptions"
                )
            if self.step == 2:
                return ProviderAction(
                    route="tool",
                    tool_name="propose_cancellation",
                    reason="Try to skip the choice",
                )
            return ProviderAction(route="decide", reason="Stop")

    posted = client.post(
        f"/tickets/{CHOICE}/messages",
        headers=support_headers(client),
        json={"body": "Please refund the duplicate charge and terminate immediately."},
    )
    assert posted.status_code == 200, posted.text
    monkeypatch.setattr(api_app.state, "provider_factory", ForcedCancellation)
    forced = client.post(f"/tickets/{CHOICE}/runs", headers=support_headers(client))
    assert forced.status_code == 200, forced.text
    assert all(
        item["action_type"] != "cancel_at_period_end" for item in forced.json()["data"]["proposals"]
    )


def test_a_later_choice_allows_the_period_end_half(client) -> None:
    support = support_headers(client)
    older = client.post(
        f"/tickets/{COMBINED}/subscription-choice",
        headers=support,
        json={"subscription_id": SUBSCRIPTION},
    )
    assert older.status_code == 200, older.text
    posted = client.post(
        f"/tickets/{COMBINED}/messages",
        headers=support_headers(client),
        json={"body": "Please refund the duplicate charge and terminate immediately."},
    )
    assert posted.status_code == 200, posted.text
    waiting = _investigate(client, COMBINED, support_headers(client))
    assert _codes(waiting)["cancel_at_period_end"] == "waiting"
    later = client.post(
        f"/tickets/{COMBINED}/subscription-choice",
        headers=support_headers(client),
        json={"subscription_id": SUBSCRIPTION},
    )
    assert later.status_code == 200, later.text
    resumed = _investigate(client, COMBINED, support_headers(client))
    assert any(item["action_type"] == "cancel_at_period_end" for item in resumed["proposals"])
    assert _codes(resumed)["refund"] == "open"


def test_rejecting_a_combined_reply_stays_rejected(client, settings) -> None:
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    for proposal in detail["proposals"]:
        assert _execute(client, proposal, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=8)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    first = next(item for item in current["replies"] if item["status"] == "draft")
    rejected = client.post(
        f"/reply-drafts/{first['id']}/reject",
        headers=approver,
        json={"revision": first["revision"], "body_hash": first["body_hash"]},
    )
    assert rejected.status_code == 200, rejected.text
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM app.reply_draft WHERE id = :id"),
            {"id": first["id"]},
        ).scalar_one()
        audits = connection.execute(
            text(
                """
                SELECT count(*) FROM app.audit_event
                WHERE event_type = 'reply_rejected' AND ticket_id = :ticket_id
                """
            ),
            {"ticket_id": COMBINED},
        ).scalar_one()
    engine.dispose()
    assert status == "rejected"
    assert int(audits) == 1
    again = client.post(
        f"/reply-drafts/{first['id']}/reject",
        headers=approver_headers(client),
        json={"revision": first["revision"], "body_hash": first["body_hash"]},
    )
    assert again.status_code == 200, again.text
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        audits = connection.execute(
            text(
                """
                SELECT count(*) FROM app.audit_event
                WHERE event_type = 'reply_rejected' AND ticket_id = :ticket_id
                """
            ),
            {"ticket_id": COMBINED},
        ).scalar_one()
        kept = connection.execute(
            text("SELECT status FROM app.reply_draft WHERE id = :id"),
            {"id": first["id"]},
        ).scalar_one()
    engine.dispose()
    assert kept == "rejected"
    assert int(audits) == 1


def _half_count(settings, ticket_id: str) -> int:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM app.combined_half WHERE ticket_id = :ticket_id"),
            {"ticket_id": ticket_id},
        ).scalar_one()
    engine.dispose()
    return int(count)


def _reply_rows(settings, ticket_id: str) -> list[dict[str, str | None]]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT id::text, status, receipt->>'receipt_id' AS receipt_id
                FROM app.reply_draft
                WHERE ticket_id = :ticket_id
                ORDER BY revision
                """
            ),
            {"ticket_id": ticket_id},
        ).mappings()
        found = [dict(row) for row in rows]
    engine.dispose()
    return found


def _event_count(settings, ticket_id: str, event_type: str) -> int:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        count = connection.execute(
            text(
                """
                SELECT count(*) FROM app.audit_event
                WHERE ticket_id = :ticket_id AND event_type = :event_type
                """
            ),
            {"ticket_id": ticket_id, "event_type": event_type},
        ).scalar_one()
    engine.dispose()
    return int(count)


def _wait_for_ticket_lock(settings) -> bool:
    import time

    engine = make_engine(settings.database_url)
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            with engine.connect() as connection:
                waiting = connection.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM pg_locks AS granted_lock
                        WHERE NOT granted_lock.granted
                        """
                    )
                ).scalar_one()
            if int(waiting) >= 1:
                return True
            time.sleep(0.05)
    finally:
        engine.dispose()
    return False


def _codes(detail: dict) -> dict[str, str]:
    return {item["half"]: item["outcome_code"] for item in detail["combined"]["halves"]}


def _execute(client, proposal: dict, approver: dict[str, str]):
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    approved = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
    assert approved.status_code == 200, approved.text
    return client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)


def _send(client, draft: dict, approver: dict[str, str]):
    return client.post(
        f"/reply-drafts/{draft['id']}/approve-and-send",
        headers=approver,
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )


def _versions(settings, ticket_id: str, half: str) -> list[str]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT outcome_code FROM app.combined_half
                WHERE ticket_id = :ticket_id AND half = :half
                ORDER BY outcome_version
                """
            ),
            {"ticket_id": ticket_id, "half": half},
        ).scalars()
        found = list(rows)
    engine.dispose()
    return found


def _jobs(settings, ticket_id: str) -> list[dict]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT j.state, j.block_reason, j.command->>'action' AS action
                FROM app.recovery_job AS j
                JOIN app.proposal AS p ON p.id = j.proposal_id
                WHERE p.ticket_id = :ticket_id
                ORDER BY j.created_at, j.id
                """
            ),
            {"ticket_id": ticket_id},
        ).mappings()
        found = [dict(row) for row in rows]
    engine.dispose()
    return found


def _scheduled(settings, subscription_id: str) -> bool:
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        flag = connection.execute(
            text(
                """
                SELECT cancel_at_period_end FROM simulator.subscription
                WHERE id = :id
                """
            ),
            {"id": subscription_id},
        ).scalar_one()
    engine.dispose()
    return bool(flag)


def _write_attempts(settings, action_id: str) -> int:
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT write_attempts FROM simulator.operation WHERE action_id = :action_id"),
            {"action_id": action_id},
        ).scalar()
    engine.dispose()
    return int(value or 0)


def _expire(settings, approval_id: str) -> None:
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.approval
                SET expires_at = clock_timestamp() - interval '1 second'
                WHERE id = :id
                """
            ),
            {"id": approval_id},
        )
    engine.dispose()


def _bump(settings, subscription_id: str) -> None:
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


def _shift(settings, subscription_id: str) -> None:
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE simulator.subscription
                SET current_period_end = clock_timestamp() - interval '5 seconds'
                WHERE id = :id
                """
            ),
            {"id": subscription_id},
        )
    engine.dispose()


def _set_max_attempts(settings, ticket_id: str, maximum: int) -> None:
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.recovery_job AS j
                SET max_attempts = :maximum
                FROM app.proposal AS p
                WHERE p.id = j.proposal_id
                  AND p.ticket_id = :ticket_id
                  AND j.state = 'queued'
                """
            ),
            {"maximum": maximum, "ticket_id": ticket_id},
        )
    engine.dispose()


def _prepare_blocked_read(client, settings, monkeypatch):
    detail = _investigate(client, COMBINED, support_headers(client))
    approver = approver_headers(client)
    refund = next(item for item in detail["proposals"] if item["action_type"] == "refund")
    cancel = next(
        item for item in detail["proposals"] if item["action_type"] == "cancel_at_period_end"
    )
    assert _execute(client, refund, approver).status_code == 200
    from resolveai.worker.__main__ import drain

    drain(settings, limit=4)
    assert _execute(client, cancel, approver).status_code == 200
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.recovery_job AS j
                SET attempt_count = 4
                FROM app.proposal AS p
                WHERE p.id = j.proposal_id
                  AND p.ticket_id = :ticket_id
                  AND j.state = 'queued'
                """
            ),
            {"ticket_id": COMBINED},
        )
    engine.dispose()
    _fail_subscription_reads(monkeypatch)
    drain(settings, limit=3)
    current = client.get(f"/tickets/{COMBINED}", headers=approver).json()["data"]
    assert _codes(current)["refund"] == "confirmed"
    assert _codes(current)["cancel_at_period_end"] == "not_dispatched"
    assert "not a decision that the request is ineligible" in next(
        item["body"] for item in current["replies"] if item["status"] == "draft"
    )
    return detail


def _unavailable():
    from resolveai.tools.client import ToolResult

    return ToolResult(
        status="error",
        data={},
        error_code="unavailable",
        retryable=True,
        operation_id=None,
        resource_version=None,
        observed_at="",
        request_id="combined-read",
    )


def _fail_subscription_reads(monkeypatch) -> None:
    from resolveai.tools.client import SimulatorClient

    monkeypatch.setattr(
        SimulatorClient, "list_subscriptions", lambda self, account_id: _unavailable()
    )


def _fail_operation_reads(monkeypatch) -> None:
    from resolveai.tools.client import SimulatorClient

    monkeypatch.setattr(
        SimulatorClient, "get_operation", lambda self, account_id, action_id: _unavailable()
    )


def _fail_after_schedule(monkeypatch) -> None:
    from resolveai.tools.client import SimulatorClient

    original = SimulatorClient.list_subscriptions
    calls = {"n": 0}

    def _list(self, account_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(self, account_id)
        return _unavailable()

    monkeypatch.setattr(SimulatorClient, "list_subscriptions", _list)
    monkeypatch.setattr(
        SimulatorClient, "get_operation", lambda self, account_id, action_id: _unavailable()
    )
