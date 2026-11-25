"""Incident correlation. These tests do not call a paid model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.support import approver_headers, login, seeing_engine, support_headers

from resolveai.agent.provider import FakeProvider, ProviderAction
from resolveai.db.session import make_engine
from resolveai.domain.incident import (
    classify_incidents,
    parse_snapshot,
    reply_body,
    snapshot_digest,
)
from resolveai.settings import repo_root
from resolveai.tools.client import ToolResult

KNOWN = "55555555-5555-4555-8555-555555555541"
TWO = "55555555-5555-4555-8555-555555555542"
NONE = "55555555-5555-4555-8555-555555555543"
COMPENSATION = "55555555-5555-4555-8555-555555555544"
RESOLVED = "55555555-5555-4555-8555-555555555545"
HARBOR = "55555555-5555-4555-8555-555555555551"
KNOWN_INCIDENT = "88888888-8888-4888-8888-888888888801"
HARBOR_INCIDENT = "88888888-8888-4888-8888-888888888811"
RESOLVED_ACCOUNT = "44444444-4444-4444-8444-444444444445"
NORTHWIND = "11111111-1111-4111-8111-111111111111"
REPORT = {
    "service_code": "api",
    "region_code": "us-east",
    "reported_started_at": "2026-10-15T00:00:00.000000Z",
}


def _row(**overrides: object) -> dict:
    base = {
        "id": "88888888-8888-4888-8888-888888888805",
        "account_id": RESOLVED_ACCOUNT,
        "service_code": "api",
        "region_code": "us-east",
        "status": "investigating",
        "started_at": "2026-10-01T00:00:00.000000Z",
        "resolved_at": None,
        "summary": "Synthetic",
        "reference_ids": [],
        "resource_version": 1,
    }
    base.update(overrides)
    return base


def _report(client, ticket_id: str, headers: dict[str, str], **extra: object) -> dict:
    response = client.post(
        f"/tickets/{ticket_id}/incident-report",
        headers=headers,
        json={**REPORT, **extra},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _investigate(client, ticket_id: str, headers: dict[str, str]) -> dict:
    response = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _handoffs(settings, ticket_id: str) -> list[dict]:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT kind, reason_code, incident_id::text AS incident_id,
                       snapshot_digest, evidence, run_id::text AS run_id
                FROM app.incident_handoff
                WHERE ticket_id = :ticket_id
                ORDER BY created_at, id
                """
            ),
            {"ticket_id": ticket_id},
        ).mappings()
        found = [dict(row) for row in rows]
    engine.dispose()
    return found


def _reads(settings, run_id: str) -> int:
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        count = connection.execute(
            text(
                """
                SELECT count(*) FROM app.tool_invocation
                WHERE run_id = :run_id AND tool_name = 'list_incidents'
                """
            ),
            {"run_id": run_id},
        ).scalar_one()
    engine.dispose()
    return int(count)


def _refunds(settings) -> int:
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        count = connection.execute(text("SELECT count(*) FROM simulator.refund")).scalar_one()
    engine.dispose()
    return int(count)


def _send(client, detail: dict, approver: dict[str, str], status: int = 200):
    reply = next(item for item in reversed(detail["replies"]) if item["status"] == "draft")
    response = client.post(
        f"/reply-drafts/{reply['id']}/approve-and-send",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert response.status_code == status, response.text
    return response


def _reject(client, detail: dict, approver: dict[str, str]) -> None:
    reply = next(item for item in reversed(detail["replies"]) if item["status"] == "draft")
    response = client.post(
        f"/reply-drafts/{reply['id']}/reject",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert response.status_code == 200, response.text


def _script(api_app, payloads: list[object]) -> None:
    factory = api_app.state.tool_client_factory
    cursor = {"n": 0}

    def wrapped(settings):
        tool = factory(settings)

        def scripted(_account_id: object) -> ToolResult:
            item = payloads[min(cursor["n"], len(payloads) - 1)]
            cursor["n"] += 1
            if item is None:
                return ToolResult(
                    status="error",
                    data={},
                    error_code="unavailable",
                    retryable=False,
                    operation_id=None,
                    resource_version=None,
                    observed_at="2026-10-15T00:00:00.000000Z",
                    request_id=f"script-{cursor['n']}",
                )
            if isinstance(item, list):
                data = {"incidents": item}
            else:
                data = {"incidents": [{"status": "boom"}]}
            return ToolResult(
                status="ok",
                data=data,
                error_code=None,
                retryable=False,
                operation_id=None,
                resource_version=None,
                observed_at="2026-10-15T00:00:00.000000Z",
                request_id=f"script-{cursor['n']}",
            )

        tool.list_incidents = scripted
        return tool

    api_app.state.tool_client_factory = wrapped


def test_decision_table_is_ordered_and_does_not_invent_a_digest() -> None:
    reported = "2026-10-15T00:00:00.000000Z"
    full = _row()
    other = _row(id="88888888-8888-4888-8888-888888888899", summary="Second")
    shared = {
        "service_code": "api",
        "region_code": "us-east",
        "reported_started_at": reported,
        "request_id": None,
    }
    assert classify_incidents([full], compensation=True, **shared)["kind"] == "compensation"
    confirmed = classify_incidents([full], compensation=False, **shared)
    assert confirmed["kind"] == "confirmed"
    assert confirmed["incident_id"] == full["id"]
    assert classify_incidents([full, other], compensation=False, **shared)["kind"] == "several"
    mismatched = classify_incidents([full], compensation=False, **{**shared, "request_id": "OTHER"})
    assert mismatched["kind"] == "possible"
    assert mismatched["reason_code"] == "request_id_not_listed"
    resolved = _row(
        status="resolved",
        resolved_at=reported,
    )
    outside = classify_incidents([resolved], compensation=False, **shared)
    assert outside["kind"] == "possible"
    assert outside["reason_code"] == "resolved_before_report"
    partial = classify_incidents(
        [_row(region_code="eu-west")],
        compensation=False,
        **shared,
    )
    assert partial["reason_code"] == "partial_overlap"
    assert classify_incidents([], compensation=False, **shared)["kind"] == "none"
    assert parse_snapshot(RESOLVED_ACCOUNT, {"incidents": [{"status": "boom"}]}) is None
    assert parse_snapshot(RESOLVED_ACCOUNT, {"incidents": [_row(id="not-a-uuid")]}) is None
    assert parse_snapshot(RESOLVED_ACCOUNT, {"incidents": [_row(id="")]}) is None
    assert parse_snapshot(RESOLVED_ACCOUNT, None) is None
    partial = _row(id="88888888-8888-4888-8888-888888888877", region_code="eu-west")
    referenced = _row(reference_ids=["OTHER"])
    mismatched = _row(id="88888888-8888-4888-8888-888888888866", reference_ids=[])
    outside = _row(
        id="88888888-8888-4888-8888-888888888855",
        status="resolved",
        resolved_at=reported,
    )
    mixes = [
        ("partial_overlap", [full, partial]),
        ("request_id_not_listed", [referenced, mismatched]),
        ("resolved_before_report", [full, outside]),
    ]
    for reason, rows in mixes:
        outcome = classify_incidents(
            rows,
            compensation=False,
            **{**shared, "request_id": None if reason != "request_id_not_listed" else "OTHER"},
        )
        assert outcome["kind"] == "confirmed"
        assert outcome["incident_id"] == rows[0]["id"]
        text_body = reply_body(outcome, customer_name="Ira")
        assert rows[0]["id"] in text_body
        assert rows[1]["id"] not in text_body
        classes = {item["match_class"] for item in outcome["evidence"]}
        assert "full" in classes and len(classes) == 2
    several = classify_incidents([full, other, partial], compensation=False, **shared)
    assert several["kind"] == "several"
    several_text = reply_body(several, customer_name="Ira")
    assert full["id"] in several_text and other["id"] in several_text
    assert partial["id"] not in several_text
    assert snapshot_digest([]) != ""


def test_each_tenant_confirms_only_its_own_incident(client, settings) -> None:
    support = support_headers(client)
    _report(client, KNOWN, support, request_id="REQ-1")
    detail = _investigate(client, KNOWN, support)
    assert detail["incident"]["handoff"]["kind"] == "confirmed"
    assert detail["incident"]["handoff"]["incident_id"] == KNOWN_INCIDENT
    assert detail["proposals"] == []
    stored = _handoffs(settings, KNOWN)[-1]
    assert stored["snapshot_digest"]
    assert _reads(settings, stored["run_id"]) == 2

    harbor = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    assert client.get(f"/tickets/{KNOWN}", headers=harbor).status_code == 404
    _report(
        client,
        HARBOR,
        harbor,
        region_code="eu-west",
    )
    harbor_detail = _investigate(client, HARBOR, harbor)
    assert harbor_detail["incident"]["handoff"]["incident_id"] == HARBOR_INCIDENT
    support = support_headers(client)
    assert client.get(f"/tickets/{HARBOR}", headers=support).status_code == 404
    engine = make_engine(settings.api_database_url)
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": NORTHWIND},
            )
            connection.execute(text("UPDATE app.incident_handoff SET reason_code = 'tampered'"))
    engine.dispose()
    oracle = make_engine(settings.oracle_database_url)
    with oracle.connect() as connection:
        visible = connection.execute(
            text("SELECT count(*) FROM simulator.incident WHERE id = :id"),
            {"id": HARBOR_INCIDENT},
        ).scalar_one()
    oracle.dispose()
    assert int(visible) == 1
    denied = make_engine(settings.api_database_url)
    with pytest.raises(DBAPIError):
        with denied.connect() as connection:
            connection.execute(text("SELECT count(*) FROM simulator.incident"))
    denied.dispose()


def test_missing_report_waits_even_when_the_read_fails(
    client, api_app, settings, restore_runtime
) -> None:
    del restore_runtime
    _script(api_app, [None])

    class Forced(FakeProvider):
        def __init__(self) -> None:
            self.step = 0

        def next_action(self, snapshot: object) -> ProviderAction:
            del snapshot
            self.step += 1
            if self.step == 1:
                return ProviderAction(
                    route="tool", tool_name="list_incidents", reason="Force a failed read"
                )
            if self.step == 2:
                return ProviderAction(
                    route="tool", tool_name="handoff_ticket", reason="Force a handoff"
                )
            return ProviderAction(route="decide", reason="Stop")

    api_app.state.provider_factory = Forced
    detail = _investigate(client, NONE, support_headers(client))
    assert detail["incident"]["handoff"] is None
    assert detail["incident"]["report"] is None
    assert _handoffs(settings, NONE) == []


def test_compensation_is_a_handoff_without_a_financial_effect(
    client, api_app, settings, restore_runtime
) -> None:
    del restore_runtime
    before = _refunds(settings)
    support = support_headers(client)
    _report(client, COMPENSATION, support)
    detail = _investigate(client, COMPENSATION, support)
    assert detail["incident"]["handoff"]["kind"] == "compensation"
    assert detail["incident"]["handoff"]["incident_id"] is None
    assert detail["proposals"] == []
    assert _refunds(settings) == before

    class Forced(FakeProvider):
        def __init__(self) -> None:
            self.step = 0

        def next_action(self, snapshot: object) -> ProviderAction:
            del snapshot
            self.step += 1
            if self.step == 1:
                return ProviderAction(
                    route="tool", tool_name="propose_refund", reason="Force a refund"
                )
            if self.step == 2:
                return ProviderAction(
                    route="tool",
                    tool_name="propose_cancellation",
                    reason="Force a cancellation",
                )
            return ProviderAction(route="decide", reason="Stop")

    api_app.state.provider_factory = Forced
    posted = client.post(
        f"/tickets/{COMPENSATION}/messages",
        headers=support,
        json={"body": "There is an API failure and I want compensation."},
    )
    assert posted.status_code == 200, posted.text
    forced = _investigate(client, COMPENSATION, support)
    assert forced["proposals"] == []
    assert _refunds(settings) == before
    assert _handoffs(settings, COMPENSATION)[-1]["kind"] == "compensation"


def test_ordinary_refund_and_cancellation_still_proceed(client, manifest) -> None:
    support = support_headers(client)
    refund = _investigate(client, manifest["cases"][0]["ticket_id"], support)
    assert refund["proposals"][0]["action_type"] == "refund"
    cancellation = _investigate(client, "55555555-5555-4555-8555-555555555521", support)
    assert cancellation["proposals"][0]["action_type"] == "cancel_at_period_end"


def test_snapshot_protocol_keeps_provenance(client, api_app, settings, restore_runtime) -> None:
    del restore_runtime
    _report(client, RESOLVED, support_headers(client))
    stable = [_row()]
    changed = [_row(status="identified", resource_version=2)]
    added = [stable[0], _row(id="88888888-8888-4888-8888-888888888899")]
    phases = [
        ([stable, stable], "confirmed", True),
        ([stable, changed, changed], "confirmed", True),
        ([stable, added, added], "several", True),
        ([stable, changed, [_row(resource_version=3)]], "conflict", False),
        ([None], "unavailable", False),
        (["malformed"], "unavailable", False),
        ([stable, None], "unavailable", False),
        ([stable, "malformed"], "unavailable", False),
        ([stable, changed, None], "unavailable", False),
        ([stable, changed, "malformed"], "unavailable", False),
    ]
    for payloads, kind, verified in phases:
        _script(api_app, payloads)
        detail = _investigate(client, RESOLVED, support_headers(client))
        handoff = detail["incident"]["handoff"]
        assert handoff["kind"] == kind, payloads
        stored = _handoffs(settings, RESOLVED)[-1]
        assert _reads(settings, stored["run_id"]) <= 3
        if verified:
            assert stored["snapshot_digest"]
        else:
            assert stored["snapshot_digest"] is None
            assert stored["incident_id"] is None
        if kind == "conflict":
            digests = [item["digest"] for item in stored["evidence"]["context"]]
            assert len(digests) == 3
            assert len(set(digests)) == 3
        _reject(client, detail, approver_headers(client))


def test_send_and_input_races_keep_one_current_draft(client, settings) -> None:
    support = support_headers(client)
    _report(client, TWO, support)
    first = _investigate(client, TWO, support)
    assert first["incident"]["handoff"]["kind"] == "several"
    first_reply = next(item for item in first["replies"] if item["status"] == "draft")
    _reject(client, first, approver_headers(client))
    support = support_headers(client)
    second = _investigate(client, TWO, support)
    second_reply = next(item for item in second["replies"] if item["status"] == "draft")
    assert second_reply["id"] != first_reply["id"]
    assert second_reply["body"] == first_reply["body"]
    assert second["incident"]["handoff"]["run_id"] != first["incident"]["handoff"]["run_id"]
    raced = client.post(
        f"/tickets/{TWO}/messages",
        headers=support,
        json={"body": "There is an API failure."},
    )
    assert raced.status_code == 200, raced.text
    hidden = raced.json()["data"]
    assert hidden["incident"]["handoff"] is None
    assert hidden["incident"]["history"]
    _send(client, second, approver_headers(client), status=409)
    support = support_headers(client)
    third = _investigate(client, TWO, support)
    draft = next(item for item in third["replies"] if item["status"] == "draft")
    approver = approver_headers(client)
    sent = client.post(
        f"/reply-drafts/{draft['id']}/approve-and-send",
        headers=approver,
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )
    assert sent.status_code == 200, sent.text
    receipt = sent.json()["data"]["replies"][-1]["receipt"]["receipt_id"]
    again = client.post(
        f"/reply-drafts/{draft['id']}/approve-and-send",
        headers=approver,
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["replies"][-1]["receipt"]["receipt_id"] == receipt
    client.post(
        f"/tickets/{TWO}/messages",
        headers=support_headers(client),
        json={"body": "There is an API failure."},
    )
    repeated = client.post(
        f"/reply-drafts/{draft['id']}/approve-and-send",
        headers=approver_headers(client),
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["replies"][-1]["receipt"]["receipt_id"] == receipt


def test_a_report_for_a_different_ticket_is_not_a_snapshot(client, settings) -> None:
    support = support_headers(client)
    _report(client, NONE, support)
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE app.incident_report
                SET ticket_id = :known
                WHERE ticket_id = :none
                """
            ),
            {"known": KNOWN, "none": NONE},
        )
    engine.dispose()
    detail = _investigate(client, KNOWN, support)
    assert detail["incident"]["handoff"] is None
    assert _handoffs(settings, KNOWN) == []


def test_b1_uses_the_same_confirmed_record(client, api_app, restore_runtime) -> None:
    del restore_runtime
    from resolveai.evaluation.catalog import use_baseline

    use_baseline(api_app)
    support = support_headers(client)
    _report(client, KNOWN, support, request_id="REQ-1")
    detail = _investigate(client, KNOWN, support)
    assert detail["incident"]["handoff"]["kind"] == "confirmed"
    assert detail["incident"]["handoff"]["incident_id"] == KNOWN_INCIDENT
    assert detail["proposals"] == []


def test_stale_rejection_leaves_the_current_handoff(client) -> None:
    support = support_headers(client)
    _report(client, KNOWN, support)
    initial = _investigate(client, KNOWN, support)
    old = next(item for item in initial["replies"] if item["status"] == "draft")
    support = support_headers(client)
    _report(client, KNOWN, support, request_id="REQ-1")
    current = _investigate(client, KNOWN, support)
    current_draft = next(item for item in current["replies"] if item["status"] == "draft")
    assert current_draft["id"] != old["id"]
    response = client.post(
        f"/reply-drafts/{old['id']}/reject",
        headers=approver_headers(client),
        json={"revision": old["revision"], "body_hash": old["body_hash"]},
    )
    assert response.status_code == 200, response.text
    after = response.json()["data"]
    assert after["ticket"]["status"] == "awaiting_reply"
    assert after["runs"][-1]["state"] == "awaiting_reply_review"
    assert after["incident"]["handoff"]["id"] == current["incident"]["handoff"]["id"]
    kept = next(item for item in after["replies"] if item["id"] == current_draft["id"])
    assert kept["status"] == "draft"


def test_current_rejection_can_be_repeated_and_a_sent_reply_stays_sent(client) -> None:
    support = support_headers(client)
    _report(client, NONE, support)
    detail = _investigate(client, NONE, support)
    _reject(client, detail, approver_headers(client))
    approver = approver_headers(client)
    draft = next(item for item in detail["replies"] if item["status"] == "draft")
    again = client.post(
        f"/reply-drafts/{draft['id']}/reject",
        headers=approver,
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )
    assert again.status_code == 200, again.text
    assert again.json()["data"]["ticket"]["status"] == "open"
    support = support_headers(client)
    _report(client, TWO, support)
    sent_detail = _investigate(client, TWO, support)
    sent = _send(client, sent_detail, approver_headers(client))
    receipt = sent.json()["data"]["replies"][-1]["receipt"]["receipt_id"]
    draft = next(item for item in sent_detail["replies"] if item["status"] == "draft")
    refused = client.post(
        f"/reply-drafts/{draft['id']}/reject",
        headers=approver_headers(client),
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )
    assert refused.status_code == 409, refused.text
    kept = client.get(f"/tickets/{TWO}", headers=support_headers(client))
    assert kept.json()["data"]["replies"][-1]["receipt"]["receipt_id"] == receipt


def test_incident_read_cap_blocks_a_noncompliant_provider(
    client, api_app, settings, restore_runtime
) -> None:
    del restore_runtime
    support = support_headers(client)
    _report(client, KNOWN, support, request_id="REQ-1")

    class RepeatedRead(FakeProvider):
        def __init__(self) -> None:
            self.step = 0

        def next_action(self, snapshot: object) -> ProviderAction:
            del snapshot
            self.step += 1
            if self.step <= 4:
                return ProviderAction(
                    route="tool",
                    tool_name="list_incidents",
                    reason="Repeat read",
                )
            if self.step == 5:
                return ProviderAction(route="tool", tool_name="handoff_ticket", reason="Handoff")
            return ProviderAction(route="decide", reason="Stop")

    api_app.state.provider_factory = RepeatedRead
    detail = _investigate(client, KNOWN, support)
    assert detail["incident"]["handoff"]["kind"] == "unavailable"
    assert detail["incident"]["handoff"]["incident_id"] is None
    assert _reads(settings, detail["runs"][-1]["id"]) <= 3
    assert _handoffs(settings, KNOWN)[-1]["snapshot_digest"] is None


@pytest.mark.parametrize("invalid_id", ["not-a-uuid", ""])
def test_malformed_incident_identity_is_unavailable(
    client, api_app, settings, restore_runtime, invalid_id: str
) -> None:
    del restore_runtime
    support = support_headers(client)
    _report(client, RESOLVED, support)
    _script(api_app, [[_row(id=invalid_id)], [_row(id=invalid_id)]])
    detail = _investigate(client, RESOLVED, support)
    assert detail["incident"]["handoff"]["kind"] == "unavailable"
    assert detail["incident"]["handoff"]["incident_id"] is None
    stored = _handoffs(settings, RESOLVED)[-1]
    assert stored["snapshot_digest"] is None
    assert detail["proposals"] == []


def test_several_reply_lists_only_full_matches(client, api_app, settings, restore_runtime) -> None:
    del restore_runtime
    support = support_headers(client)
    _report(client, RESOLVED, support)
    partial = "88888888-8888-4888-8888-888888888877"
    rows = [
        _row(),
        _row(id="88888888-8888-4888-8888-888888888899"),
        _row(id=partial, region_code="eu-west"),
    ]
    _script(api_app, [rows, rows])
    detail = _investigate(client, RESOLVED, support)
    body = next(item for item in detail["replies"] if item["status"] == "draft")["body"]
    assert partial not in body
    assert rows[0]["id"] in body and rows[1]["id"] in body
    stored = _handoffs(settings, RESOLVED)[-1]
    classes = {item["match_class"] for item in stored["evidence"]["candidates"]}
    assert classes == {"full", "partial"}


def test_a_corrected_report_replaces_the_unsent_handoff(client) -> None:
    support = support_headers(client)
    _report(client, KNOWN, support, request_id="REQ-1")
    first = _investigate(client, KNOWN, support)
    old_draft = next(item for item in first["replies"] if item["status"] == "draft")
    replaced = _report(client, KNOWN, support, request_id="OTHER-1")
    assert replaced["incident"]["handoff"] is None
    assert replaced["incident"]["history"][0]["kind"] == "confirmed"
    assert replaced["incident"]["report"]["request_id"] == "OTHER-1"
    superseded = next(item for item in replaced["replies"] if item["id"] == old_draft["id"])
    assert superseded["status"] == "superseded"
    second = _investigate(client, KNOWN, support_headers(client))
    assert second["incident"]["handoff"]["kind"] == "possible"
    assert second["incident"]["handoff"]["id"] != first["incident"]["handoff"]["id"]
    fresh = next(item for item in second["replies"] if item["status"] == "draft")
    assert fresh["id"] != old_draft["id"]


def test_confirmed_storage_rejects_a_fabricated_candidate(
    client, api_app, settings, restore_runtime
) -> None:
    del restore_runtime
    support = support_headers(client)
    _report(client, KNOWN, support, request_id="REQ-1")
    initial = _investigate(client, KNOWN, support)
    _reject(client, initial, approver_headers(client))

    class ReadStop(FakeProvider):
        def __init__(self) -> None:
            self.step = 0

        def next_action(self, snapshot: object) -> ProviderAction:
            del snapshot
            self.step += 1
            if self.step == 1:
                return ProviderAction(route="tool", tool_name="list_incidents", reason="Read")
            return ProviderAction(route="decide", reason="Stop")

    api_app.state.provider_factory = ReadStop
    detail = _investigate(client, KNOWN, support_headers(client))
    run_id = detail["runs"][-1]["id"]
    admin = make_engine(settings.database_url)
    with admin.connect() as connection:
        invocation = connection.execute(
            text(
                """
                SELECT id FROM app.tool_invocation
                WHERE run_id = :run_id AND tool_name = 'list_incidents'
                """
            ),
            {"run_id": run_id},
        ).scalar_one()
        template = connection.execute(
            text(
                """
                SELECT id FROM app.incident_handoff
                WHERE ticket_id = :ticket_id
                ORDER BY created_at
                LIMIT 1
                """
            ),
            {"ticket_id": KNOWN},
        ).scalar_one()
    admin.dispose()
    runtime = seeing_engine(settings.api_database_url)
    try:
        for evidence in (
            {"verified": True, "candidates": [], "context": []},
            None,
            {"verified": True},
            {"verified": True, "candidates": {}, "context": []},
        ):
            with pytest.raises(DBAPIError):
                _insert_confirmed(runtime, template, run_id, invocation, evidence)
        accepted = _insert_confirmed(
            runtime,
            template,
            run_id,
            invocation,
            {
                "verified": True,
                "candidates": [
                    {"id": str(uuid4()), "match_class": "full", "resource_version": 1},
                    {
                        "id": "88888888-8888-4888-8888-888888888877",
                        "match_class": "partial",
                        "resource_version": 4,
                    },
                ],
                "context": [{"historical": True}],
            },
        )
    finally:
        runtime.dispose()
    assert accepted == 1


def _insert_confirmed(engine, template, run_id, invocation, evidence) -> int:
    candidates = evidence.get("candidates") if isinstance(evidence, dict) else None
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        incident_id = candidates[0]["id"]
    else:
        incident_id = uuid4()
    with engine.begin() as connection:
        return connection.execute(
            text(
                """
                INSERT INTO app.incident_handoff (
                  id, tenant_id, ticket_id, account_id, customer_id, run_id, report_id,
                  actor_user_id, input_revision, reply_draft_id, tool_invocation_id,
                  observed_at, snapshot_digest, kind, incident_id, incident_resource_version,
                  evidence, compensation_requested, reason_code, next_step, created_at
                )
                SELECT :id, tenant_id, ticket_id, account_id, customer_id, :run_id, report_id,
                       actor_user_id, input_revision, NULL, :invocation, clock_timestamp(),
                       :digest, 'confirmed', :incident_id, 1, CAST(:evidence AS jsonb),
                       false, 'record_match', 'human_review', clock_timestamp()
                FROM app.incident_handoff
                WHERE id = :template
                """
            ),
            {
                "id": uuid4(),
                "run_id": run_id,
                "invocation": invocation,
                "template": template,
                "incident_id": incident_id,
                "digest": "verified-digest",
                "evidence": "null" if evidence is None else json.dumps(evidence),
            },
        ).rowcount


def test_downgrade_refuses_a_report_and_a_handoff(client, settings) -> None:
    support = support_headers(client)
    _report(client, NONE, support)
    refused = _alembic(settings, "downgrade", "20261029_0009")
    assert refused.returncode != 0
    _report(client, KNOWN, support, request_id="REQ-1")
    _investigate(client, KNOWN, support)
    refused_again = _alembic(settings, "downgrade", "20261029_0009")
    assert refused_again.returncode != 0
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    assert version == "20261112_0014"


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
