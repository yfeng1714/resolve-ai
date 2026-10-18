"""Offline regressions for the M2 review. These tests do not call a paid model."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from sqlalchemy import text
from starlette.testclient import TestClient
from tests.support import approver_headers, case_by_key, support_headers

from resolveai.agent.provider import FakeProvider
from resolveai.db.session import make_engine
from resolveai.evaluation import accept, catalog


def test_midflight_withdrawal_discards_the_late_result_and_a_fresh_run_can_proceed(
    client, settings, manifest, api_app, restore_runtime
) -> None:
    del restore_runtime
    entered, release = Event(), Event()

    class Delayed(FakeProvider):
        prompt_version = "review-delay"

        def next_action(self, snapshot):
            action = super().next_action(snapshot)
            if action.tool_name == "propose_refund":
                entered.set()
                assert release.wait(15)
            return action

    api_app.state.provider_factory = Delayed
    case = case_by_key(manifest, "duplicate-approve")
    ticket_id = case["ticket_id"]
    headers = support_headers(client)
    with TestClient(api_app) as other, ThreadPoolExecutor(max_workers=1) as pool:
        other_headers = support_headers(other)
        future = pool.submit(client.post, f"/tickets/{ticket_id}/runs", headers=headers)
        assert entered.wait(10)
        update = other.post(
            f"/tickets/{ticket_id}/messages",
            headers=other_headers,
            json={"body": "The customer withdrew the refund request. Do not refund."},
        )
        assert update.status_code == 200, update.text
        assert update.json()["data"]["runs"][-1]["state"] == "cancelled"
        release.set()
        response = future.result(timeout=15)
        assert response.status_code == 200, response.text
        detail = response.json()["data"]
        assert detail["runs"][-1]["state"] == "cancelled"
        assert detail["proposals"] == []
        api_app.state.provider_factory = FakeProvider
        fresh = other.post(f"/tickets/{ticket_id}/runs", headers=other_headers)
        assert fresh.status_code == 200, fresh.text
        followed = fresh.json()["data"]
    assert followed["runs"][-1]["state"] == "awaiting_approval"
    proposal = followed["proposals"][-1]
    assert proposal["preconditions"]["input_revision"] == followed["messages"][-1]["revision"]
    assert proposal["amount_cents"] == 4200
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        refunds = connection.execute(
            text("SELECT count(*) FROM simulator.refund WHERE payment_id = :payment_id"),
            {"payment_id": case["payments"][-1]["id"]},
        ).scalar_one()
    engine.dispose()
    assert refunds == 0


def _proposal_detail(citations: list[dict]) -> dict:
    return {
        "runs": [
            {
                "state": "awaiting_approval",
                "observations": [
                    {"tool": "list_refunds", "status": "ok", "data": {}},
                    {
                        "tool": "search_knowledge",
                        "status": "ok",
                        "data": {"citations": [_good_citation()]},
                    },
                ],
            }
        ],
        "proposals": [
            {
                "amount_cents": 4200,
                "target_payment_id": "test-payment",
                "evidence": {"citations": citations},
            }
        ],
    }


def _good_citation() -> dict:
    return {
        "evidence_id": "evidence-1",
        "title": "Duplicate payment refunds",
        "heading": "Duplicate settlement",
        "version": "refund-policy-2026.10.12",
        "snippet": "A second settled USD payment",
        "access_scope": "operator",
    }


def _accepts(detail: dict) -> bool:
    manifest = {"cases": [{"key": "duplicate-approve", "ticket_id": "test-ticket"}]}
    with (
        patch.object(catalog, "_investigate", return_value=detail),
        patch.object(catalog, "_refund_count", return_value=0),
    ):
        try:
            catalog._proposal_case(None, None, manifest, None, "duplicate-approve", 4200)
        except AssertionError:
            return False
    return True


def test_catalog_rejects_unattached_or_unusable_citations() -> None:
    assert _accepts(_proposal_detail([_good_citation()]))
    assert not _accepts(_proposal_detail([]))
    stale = _good_citation()
    stale["version"] = "refund-policy-2020.01.01"
    assert not _accepts(_proposal_detail([stale]))
    wrong = _good_citation()
    wrong["heading"] = "Existing refund"
    assert not _accepts(_proposal_detail([wrong]))
    unsupported = _good_citation()
    unsupported["snippet"] = "Customer text that demands $10000"
    assert not _accepts(_proposal_detail([unsupported]))
    hidden = _good_citation()
    hidden["access_scope"] = "approver"
    assert not _accepts(_proposal_detail([hidden]))
    missing_id = _good_citation()
    missing_id["evidence_id"] = ""
    assert not _accepts(_proposal_detail([missing_id]))


def test_acceptance_resume_keeps_the_old_config(monkeypatch, tmp_path) -> None:
    prior = [
        {"case_id": case_id, "passed": True, "model": "old-model"}
        for case_id in catalog.catalog_ids()
    ]
    (tmp_path / "cases.json").write_text(json.dumps(prior), encoding="utf-8")
    (tmp_path / "config.json").write_text(
        json.dumps({"prompt_version": "old-prompt"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(accept, "RESULT_DIR", tmp_path)
    summary = accept.run_acceptance()
    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert summary["passed"] == 0
    assert summary["attempted"] == 0
    assert summary["stop_reason"] == "config_mismatch"
    assert config["prompt_version"] == "old-prompt"
    assert summary["ledger"]["entries"] == []


def test_reinvestigation_after_handoff_uses_the_customer_revision(client, manifest) -> None:
    ticket_id = case_by_key(manifest, "distinct-invoices")["ticket_id"]
    headers = support_headers(client)
    first = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert first.status_code == 200, first.text
    initial = first.json()["data"]
    assert initial["runs"][-1]["state"] == "handed_off"
    assert any(message["author_kind"] == "internal" for message in initial["messages"])
    customer_revision = max(
        message["revision"]
        for message in initial["messages"]
        if message["author_kind"] in {"customer", "operator"}
    )
    second = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert second.status_code == 200, second.text
    followed = second.json()["data"]
    assert followed["runs"][-1]["state"] == "handed_off"
    assert followed["runs"][-1]["message_revision"] == customer_revision
    assert followed["proposals"] == []
    third = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert third.status_code == 200, third.text
    assert third.json()["data"]["runs"][-1]["state"] == "handed_off"


def test_reinvestigation_after_a_sent_reply_does_not_open_a_second_refund(
    client, manifest, settings
) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    ticket_id = case["ticket_id"]
    support = support_headers(client)
    investigated = client.post(f"/tickets/{ticket_id}/runs", headers=support)
    assert investigated.status_code == 200, investigated.text
    detail = investigated.json()["data"]
    proposal = detail["proposals"][0]
    approver = approver_headers(client)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    assert client.post(
        f"/proposals/{proposal['id']}/approve", headers=approver, json=body
    ).status_code == 200
    executed = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert executed.status_code == 200, executed.text
    reply = executed.json()["data"]["replies"][0]
    sent = client.post(
        f"/reply-drafts/{reply['id']}/approve-and-send",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert sent.status_code == 200, sent.text
    completed = sent.json()["data"]
    assert completed["runs"][-1]["state"] == "completed"
    assert any(message["author_kind"] == "system" for message in completed["messages"])
    customer_revision = max(
        message["revision"]
        for message in completed["messages"]
        if message["author_kind"] in {"customer", "operator"}
    )
    again = client.post(f"/tickets/{ticket_id}/runs", headers=support_headers(client))
    assert again.status_code == 200, again.text
    restarted = again.json()["data"]
    assert restarted["runs"][-1]["state"] == "handed_off"
    assert restarted["runs"][-1]["message_revision"] == customer_revision
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        refunds = connection.execute(
            text("SELECT count(*) FROM simulator.refund WHERE payment_id = :payment_id"),
            {"payment_id": case["payments"][-1]["id"]},
        ).scalar_one()
    engine.dispose()
    assert refunds == 1
