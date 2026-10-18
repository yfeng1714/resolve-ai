"""Offline M2 checks. These are not live-model measurements."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from tests.support import approver_headers, case_by_key, support_headers

from resolveai.agent.baseline import BaselineWorkflow
from resolveai.agent.budget import admission_decision
from resolveai.agent.live import LiveProvider
from resolveai.agent.provider import FakeProvider, RunSnapshot, classify_observations
from resolveai.db.session import make_engine
from resolveai.settings import LiveModelBlocked, repo_root


def _payment(event: str, invoice: str, status: str = "settled") -> dict[str, str]:
    return {"event_id": event, "invoice_id": invoice, "status": status, "amount_cents": 4200}


def _snapshot(*observations: dict) -> RunSnapshot:
    return RunSnapshot(
        observations=list(observations),
        messages=[{"revision": 1, "author_kind": "customer", "body": "I was charged twice."}],
        tenant_id="tenant",
        account_id="account",
        ticket_id="ticket",
    )


def _read(tool: str, **data: object) -> dict:
    return {"tool": tool, "status": "ok", "data": data}


def test_observations_change_the_next_tool() -> None:
    provider = FakeProvider()
    opening = [
        _read("get_customer_account", account_id="account"),
        _read("list_invoices", invoices=[]),
        _read(
            "list_payments",
            payments=[_payment("evt", "inv-a"), _payment("evt", "inv-a", "pending")],
        ),
    ]
    pending = _snapshot(*opening)
    assert classify_observations(pending) == "pending"
    assert provider.next_action(pending).tool_name == "add_internal_note"

    conflict = _snapshot(
        _read("get_customer_account"),
        _read("list_invoices"),
        _read("list_payments", payments=[_payment("evt", "inv-a"), _payment("evt", "inv-b")]),
    )
    assert classify_observations(conflict) == "conflict"
    assert provider.next_action(conflict).tool_name == "search_knowledge"

    duplicate = _snapshot(
        _read("get_customer_account"),
        _read("list_invoices"),
        _read("list_payments", payments=[_payment("evt", "inv-a"), _payment("evt", "inv-a")]),
    )
    assert provider.next_action(duplicate).tool_name == "list_refunds"
    baseline = BaselineWorkflow().next_action(duplicate)
    assert baseline.tool_name == "list_refunds"


def test_b1_finishes_billing_reads_before_the_branch() -> None:
    snapshot = _snapshot(_read("get_customer_account"), _read("list_invoices"))
    assert BaselineWorkflow().next_action(snapshot).tool_name == "list_payments"
    assert FakeProvider().next_action(snapshot).tool_name == "list_payments"


def test_conflict_path_looks_up_policy_and_does_not_propose(client, settings, manifest) -> None:
    case = case_by_key(manifest, "distinct-invoices")
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE simulator.payment SET event_id = 'evt-invoice-conflict' "
                "WHERE account_id = :account_id"
            ),
            {"account_id": case["account_id"]},
        )
    engine.dispose()
    headers = support_headers(client)
    response = client.post(f"/tickets/{case['ticket_id']}/runs", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    tools = [item["tool"] for item in detail["runs"][-1]["observations"]]
    assert "search_knowledge" in tools
    assert "propose_refund" not in tools
    assert detail["runs"][-1]["state"] == "handed_off"
    assert detail["proposals"] == []
    observations = detail["runs"][-1]["observations"]
    citation = next(item for item in observations if item["tool"] == "search_knowledge")
    titles = [item["title"] for item in citation["data"]["citations"]]
    assert "Conflicting invoice evidence" in titles


def test_duplicate_investigation_cites_policy_and_keeps_the_amount(client, manifest) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    headers = support_headers(client)
    response = client.post(f"/tickets/{case['ticket_id']}/runs", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    proposal = detail["proposals"][0]
    assert proposal["amount_cents"] == 4200
    citations = proposal["evidence"]["citations"]
    assert citations[0]["title"] == "Duplicate payment refunds"
    evidence_id = citations[0]["evidence_id"]
    visible = client.get(f"/evidence/{evidence_id}", headers=headers)
    assert visible.status_code == 200, visible.text
    assert visible.json()["data"]["title"] == "Duplicate payment refunds"


def test_support_cannot_read_approver_only_evidence(client, settings, manifest) -> None:
    del manifest
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        evidence_id = connection.execute(
            text(
                """
                SELECT c.id
                FROM app.knowledge_chunk AS c
                JOIN app.knowledge_document AS d ON d.id = c.document_id
                WHERE d.access_scope = 'approver' AND d.is_current
                LIMIT 1
                """
            )
        ).scalar_one()
    engine.dispose()
    denied = client.get(f"/evidence/{evidence_id}", headers=support_headers(client))
    assert denied.status_code == 404
    allowed = client.get(f"/evidence/{evidence_id}", headers=approver_headers(client))
    assert allowed.status_code == 200, allowed.text


def test_retired_policy_is_not_retrieved(settings, seeded) -> None:
    del seeded
    from resolveai.clock import utcnow
    from resolveai.db.session import make_session_factory
    from resolveai.domain.embedding import embed_text, vector_literal
    from resolveai.retrieval.corpus import search_knowledge

    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    tenant_id = UUID("11111111-1111-4111-8111-111111111111")
    document_id = uuid4()
    chunk_id = uuid4()
    with factory() as session:
        session.execute(
            text(
                """
                INSERT INTO app.knowledge_document (
                    id, tenant_id, source_path, title, version, content_hash,
                    access_scope, effective_from, effective_to, is_current, created_at
                ) VALUES (
                    :id, :tenant_id, 'retired.md', 'Retired rule', 'old', 'hash',
                    'operator', '2020-01-01T00:00:00+00:00', '2020-02-01T00:00:00+00:00',
                    false, :created_at
                )
                """
            ),
            {"id": document_id, "tenant_id": tenant_id, "created_at": utcnow()},
        )
        session.execute(
            text(
                """
                INSERT INTO app.knowledge_chunk (
                    id, document_id, tenant_id, ordinal, heading, body,
                    start_offset, end_offset, embedding
                ) VALUES (
                    :id, :document_id, :tenant_id, 0, 'Retired',
                    'zephyr-retired-token should not be retrieved',
                    0, 10, CAST(:embedding AS vector)
                )
                """
            ),
            {
                "id": chunk_id,
                "document_id": document_id,
                "tenant_id": tenant_id,
                "embedding": vector_literal(embed_text("zephyr-retired-token")),
            },
        )
        session.commit()
        hits = search_knowledge(
            session,
            tenant_id=tenant_id,
            role="support",
            query="zephyr-retired-token",
        )
    engine.dispose()
    assert all(hit["evidence_id"] != str(chunk_id) for hit in hits)


def test_live_call_and_over_budget_are_refused() -> None:
    with pytest.raises(LiveModelBlocked):
        LiveProvider().next_action(RunSnapshot(observations=[]))
    admission_decision(
        provider="fake",
        cap_micros=0,
        spent_micros=0,
        estimate_micros=0,
        api_key="",
        model_name="",
    )
    with pytest.raises(LiveModelBlocked):
        admission_decision(
            provider="fake",
            cap_micros=0,
            spent_micros=0,
            estimate_micros=1,
            api_key="",
            model_name="",
        )
    with pytest.raises(LiveModelBlocked):
        admission_decision(
            provider="deepseek",
            cap_micros=1_000_000,
            spent_micros=0,
            estimate_micros=1,
            api_key="",
            model_name="",
        )


def test_smoke_catalog_hides_oracle_labels() -> None:
    path = repo_root() / "fixtures" / "smoke_cases.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    cases = catalog["cases"]
    assert len(cases) == 24
    families = {case["task_family"] for case in cases}
    assert families == {
        "invoice_consistency",
        "payment_progress",
        "policy_citation",
        "approval_identity",
        "safety_budget",
    }
    for family in ("invoice_consistency", "payment_progress"):
        assert sum(case["task_family"] == family for case in cases) >= 4
    for case in cases:
        context = json.dumps(case["model_context"])
        assert "expected_outcome" not in context
        assert "follow_up_tool" not in context
        assert "oracle" not in case["model_context"]
