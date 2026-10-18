"""Executable checks for every development smoke-catalog label.

Oracle fields stay in this module. They are not copied into model context.
The fake provider is the caller. This module does not send a paid request.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import text

from resolveai.agent.baseline import BaselineWorkflow
from resolveai.clock import utcnow
from resolveai.db.session import make_engine, make_session_factory
from resolveai.domain.embedding import embed_text, vector_literal
from resolveai.retrieval.corpus import search_knowledge
from resolveai.settings import repo_root

SUPPORT = ("support@demo.resolveai.test", "support-demo-pass")
APPROVER = ("approver@demo.resolveai.test", "approver-demo-pass")


def catalog_ids() -> list[str]:
    path = repo_root() / "fixtures" / "smoke_cases.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    return [case["case_id"] for case in catalog["cases"]]


def execute_catalog_case(case_id: str, client, settings, manifest, api_app) -> None:
    """Run one catalog label against the local API and the oracle database."""
    original_calls = settings.run_max_model_calls
    original_faults = settings.simulator_allow_faults
    original_provider = api_app.state.provider_factory
    try:
        _CASES[case_id](client, settings, manifest, api_app)
    finally:
        settings.run_max_model_calls = original_calls
        settings.simulator_allow_faults = original_faults
        api_app.state.provider_factory = original_provider


def _case(manifest: dict, key: str) -> dict:
    return next(item for item in manifest["cases"] if item["key"] == key)


def _headers(client, settings, account: tuple[str, str]) -> dict[str, str]:
    response = client.post(
        "/sessions",
        json={"email": account[0], "password": account[1]},
        headers={"Origin": settings.web_origin},
    )
    assert response.status_code == 200, response.text
    token = client.cookies.get("resolveai_csrf")
    assert token
    return {"Origin": settings.web_origin, "X-CSRF-Token": token}


def _investigate(client, settings, ticket_id: str) -> dict:
    headers = _headers(client, settings, SUPPORT)
    response = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _refund_count(settings, payment_id: str) -> int:
    engine = make_engine(settings.oracle_database_url)
    try:
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM simulator.refund WHERE payment_id = :id"),
                {"id": payment_id},
            ).scalar_one()
    finally:
        engine.dispose()
    return int(count)


def _tools(detail: dict) -> list[str]:
    observations = detail["runs"][-1]["observations"]
    return [item["tool"] for item in observations if item.get("status") == "ok"]


def _citations(detail: dict) -> list[dict]:
    found: list[dict] = []
    for observation in detail["runs"][-1]["observations"]:
        if observation.get("tool") != "search_knowledge":
            continue
        found.extend((observation.get("data") or {}).get("citations") or [])
    for proposal in detail["proposals"]:
        found.extend((proposal.get("evidence") or {}).get("citations") or [])
    return found


def _mutate(settings, sql: str, params: dict) -> None:
    engine = make_engine(settings.database_url)
    try:
        with engine.begin() as connection:
            database = connection.execute(text("SELECT current_database()")).scalar()
            if database != "resolveai_pytest":
                raise RuntimeError(f"Refusing to mutate {database}")
            connection.execute(text(sql), params)
    finally:
        engine.dispose()


def _settled_pending_proposes(client, settings, manifest, api_app) -> None:
    del api_app
    case = _case(manifest, "pending-payment")
    pending = next(item for item in case["payments"] if item["status"] == "pending")
    _mutate(
        settings,
        "UPDATE simulator.payment SET status = 'settled', settled_at = now() WHERE id = :id",
        {"id": pending["id"]},
    )
    detail = _investigate(client, settings, case["ticket_id"])
    assert detail["runs"][-1]["state"] == "awaiting_approval"
    assert detail["proposals"][0]["amount_cents"] == 5000
    assert "propose_refund" in _tools(detail)
    assert "add_internal_note" not in _tools(detail)


def _refund_removed_proposes(client, settings, manifest, api_app, key: str, amount: int) -> None:
    del api_app
    case = _case(manifest, key)
    _mutate(
        settings,
        "DELETE FROM simulator.refund WHERE id = :id",
        {"id": case["refunds"][0]["id"]},
    )
    detail = _investigate(client, settings, case["ticket_id"])
    assert detail["runs"][-1]["state"] == "awaiting_approval"
    assert detail["proposals"][0]["amount_cents"] == amount
    assert _refund_count(settings, case["refunds"][0]["payment_id"]) == 0


def _distinct_with_pending_note(client, settings, manifest, api_app) -> None:
    del api_app
    case = _case(manifest, "distinct-invoices")
    _mutate(
        settings,
        "UPDATE simulator.payment SET status = 'pending', settled_at = NULL WHERE id = :id",
        {"id": case["payments"][1]["id"]},
    )
    detail = _investigate(client, settings, case["ticket_id"])
    assert detail["runs"][-1]["state"] == "awaiting_customer"
    assert "add_internal_note" in _tools(detail)
    assert detail["proposals"] == []


def _share_event(settings, account_id: str) -> None:
    _mutate(
        settings,
        "UPDATE simulator.payment SET event_id = 'evt-invoice-conflict' "
        "WHERE account_id = :account_id",
        {"account_id": account_id},
    )


def _attached_citations(detail: dict) -> list[dict]:
    found: list[dict] = []
    for proposal in detail["proposals"]:
        found.extend((proposal.get("evidence") or {}).get("citations") or [])
    return [item for item in found if isinstance(item, dict)]


def _supports_duplicate(citation: dict) -> bool:
    snippet = str(citation.get("snippet") or "")
    return (
        citation.get("title") == "Duplicate payment refunds"
        and citation.get("heading") == "Duplicate settlement"
        and citation.get("version") == "refund-policy-2026.10.12"
        and "A second settled USD payment" in snippet
        and citation.get("access_scope") == "operator"
        and bool(citation.get("evidence_id"))
    )


def _proposal_case(client, settings, manifest, api_app, key: str, amount: int) -> None:
    del api_app
    detail = _investigate(client, settings, _case(manifest, key)["ticket_id"])
    assert detail["runs"][-1]["state"] == "awaiting_approval"
    assert detail["proposals"][0]["amount_cents"] == amount
    assert "list_refunds" in _tools(detail)
    assert any(_supports_duplicate(item) for item in _attached_citations(detail))
    payment_id = detail["proposals"][0]["target_payment_id"]
    assert _refund_count(settings, payment_id) == 0


def _handoff_case(client, settings, manifest, api_app, key: str, *, search: bool) -> None:
    del api_app
    case = _case(manifest, key)
    if search:
        _share_event(settings, case["account_id"])
    detail = _investigate(client, settings, case["ticket_id"])
    assert detail["runs"][-1]["state"] == "handed_off"
    assert detail["proposals"] == []
    tools = _tools(detail)
    if search:
        assert "search_knowledge" in tools
        assert _has_citation(detail, "Conflicting invoice evidence", "Conflict")
    else:
        assert "list_refunds" in tools or "search_knowledge" in tools
    for payment in case["payments"]:
        expected = 1 if any(item["payment_id"] == payment["id"] for item in case["refunds"]) else 0
        assert _refund_count(settings, payment["id"]) == expected


def _has_citation(detail: dict, title: str, heading: str) -> bool:
    return any(
        item.get("title") == title
        and item.get("heading") == heading
        and item.get("version") == "refund-policy-2026.10.12"
        for item in _citations(detail)
    )


def _pending(client, settings, manifest, api_app) -> None:
    del api_app
    case = _case(manifest, "pending-payment")
    detail = _investigate(client, settings, case["ticket_id"])
    assert detail["runs"][-1]["state"] == "awaiting_customer"
    assert "add_internal_note" in _tools(detail)
    assert "propose_refund" not in _tools(detail)
    assert detail["proposals"] == []


def _pending_followup(client, settings, manifest, api_app) -> None:
    _pending(client, settings, manifest, api_app)
    case = _case(manifest, "pending-payment")
    headers = _headers(client, settings, SUPPORT)
    blocked = client.post(f"/tickets/{case['ticket_id']}/runs", headers=headers)
    assert blocked.status_code == 409
    posted = client.post(
        f"/tickets/{case['ticket_id']}/messages",
        headers=headers,
        json={"body": "The customer confirmed this charge is still pending."},
    )
    assert posted.status_code == 200, posted.text
    resumed = _investigate(client, settings, case["ticket_id"])
    assert resumed["runs"][-1]["state"] == "awaiting_customer"
    assert len(resumed["runs"]) == 2


def _support_cannot_approve(client, settings, manifest, api_app) -> None:
    del api_app
    detail = _investigate(client, settings, _case(manifest, "duplicate-approve")["ticket_id"])
    proposal = detail["proposals"][0]
    denied = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=_headers(client, settings, SUPPORT),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert denied.status_code == 403
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def _approver_rejects(client, settings, manifest, api_app) -> None:
    del api_app
    detail = _investigate(client, settings, _case(manifest, "duplicate-reject")["ticket_id"])
    proposal = detail["proposals"][0]
    rejected = client.post(
        f"/proposals/{proposal['id']}/reject",
        headers=_headers(client, settings, APPROVER),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["data"]["proposals"][0]["status"] == "rejected"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def _withdrawal(client, settings, manifest, api_app) -> None:
    del api_app
    detail = _investigate(client, settings, _case(manifest, "duplicate-approve")["ticket_id"])
    proposal = detail["proposals"][0]
    approved = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=_headers(client, settings, APPROVER),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert approved.status_code == 200, approved.text
    posted = client.post(
        f"/tickets/{_case(manifest, 'duplicate-approve')['ticket_id']}/messages",
        headers=_headers(client, settings, SUPPORT),
        json={"body": "The customer withdrew the refund request."},
    )
    assert posted.status_code == 200, posted.text
    assert posted.json()["data"]["proposals"][0]["status"] == "superseded"
    refused = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=_headers(client, settings, APPROVER),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert refused.status_code == 409
    assert refused.json()["error_code"] == "stale_proposal"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def _stale_payment(client, settings, manifest, api_app) -> None:
    del api_app
    detail = _investigate(client, settings, _case(manifest, "duplicate-approve")["ticket_id"])
    proposal = detail["proposals"][0]
    assert (
        client.post(
            f"/proposals/{proposal['id']}/approve",
            headers=_headers(client, settings, APPROVER),
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        ).status_code
        == 200
    )
    engine = make_engine(settings.simulator_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE simulator.payment SET resource_version = resource_version + 1 "
                    "WHERE id = :id"
                ),
                {"id": proposal["target_payment_id"]},
            )
    finally:
        engine.dispose()
    stale = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=_headers(client, settings, APPROVER),
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "stale_precondition"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0


def _one_refund(client, settings, manifest, api_app) -> None:
    del api_app
    detail = _investigate(client, settings, _case(manifest, "duplicate-approve")["ticket_id"])
    proposal = detail["proposals"][0]
    headers = _headers(client, settings, APPROVER)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    approved = client.post(
        f"/proposals/{proposal['id']}/approve", headers=headers, json=body
    )
    assert approved.status_code == 200
    executed = client.post(f"/proposals/{proposal['id']}/execute", headers=headers, json=body)
    assert executed.status_code == 200, executed.text
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def _lost_response(client, settings, manifest, api_app) -> None:
    del api_app
    settings.simulator_allow_faults = True
    detail = _investigate(client, settings, _case(manifest, "duplicate-approve")["ticket_id"])
    proposal = detail["proposals"][0]
    headers = _headers(client, settings, APPROVER)
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    approved = client.post(
        f"/proposals/{proposal['id']}/approve", headers=headers, json=body
    )
    assert approved.status_code == 200
    fault = {**headers, "X-ResolveAI-Test-Fault": "commit_then_fail"}
    executed = client.post(f"/proposals/{proposal['id']}/execute", headers=fault, json=body)
    assert executed.status_code == 200, executed.text
    assert executed.json()["data"]["actions"][0]["result"]["recovered_from_unknown"] is True
    repeated = client.post(f"/proposals/{proposal['id']}/execute", headers=fault, json=body)
    assert repeated.status_code == 200
    assert _refund_count(settings, proposal["target_payment_id"]) == 1


def _model_limit(client, settings, manifest, api_app) -> None:
    del api_app
    settings.run_max_model_calls = 1
    detail = _investigate(client, settings, _case(manifest, "duplicate-approve")["ticket_id"])
    assert detail["runs"][-1]["state"] == "failed"
    assert detail["runs"][-1]["error_code"] == "budget_exhausted"
    assert detail["proposals"] == []


def _live_blocked(client, settings, manifest, api_app) -> None:
    class _Billable:
        prompt_version = "catalog-live-blocked"
        billable = True
        provider_name = "openai"
        api_key = "present"

        def next_action(self, snapshot):
            del snapshot
            raise AssertionError("A blocked live provider must not be called")

    api_app.state.provider_factory = _Billable
    headers = _headers(client, settings, SUPPORT)
    response = client.post(
        f"/tickets/{_case(manifest, 'duplicate-approve')['ticket_id']}/runs",
        headers=headers,
        json={"allow_live": True},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "live_model_blocked"


def _approver_hidden(client, settings, manifest, api_app) -> None:
    del manifest, api_app
    engine = make_engine(settings.oracle_database_url)
    try:
        with engine.connect() as connection:
            evidence_id = connection.execute(
                text(
                    """
                    SELECT c.id FROM app.knowledge_chunk AS c
                    JOIN app.knowledge_document AS d ON d.id = c.document_id
                    WHERE d.access_scope = 'approver' AND d.is_current
                    LIMIT 1
                    """
                )
            ).scalar_one()
    finally:
        engine.dispose()
    denied = client.get(
        f"/evidence/{evidence_id}", headers=_headers(client, settings, SUPPORT)
    )
    assert denied.status_code == 404
    allowed = client.get(
        f"/evidence/{evidence_id}", headers=_headers(client, settings, APPROVER)
    )
    assert allowed.status_code == 200, allowed.text


def _retired_hidden(client, settings, manifest, api_app) -> None:
    del client, api_app
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    tenant_id = UUID(manifest["tenant"]["id"])
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
            {"id": uuid4(), "tenant_id": tenant_id, "created_at": utcnow()},
        )
        document_id = session.execute(
            text("SELECT id FROM app.knowledge_document WHERE title = 'Retired rule' LIMIT 1")
        ).scalar_one()
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


ADAPTIVE_SETUPS = (
    {
        "case_id": "inv-01",
        "family": "invoice_consistency",
        "fixture": "duplicate-approve",
        "mutation": "none",
    },
    {
        "case_id": "inv-02",
        "family": "invoice_consistency",
        "fixture": "duplicate-reject",
        "mutation": "none",
    },
    {
        "case_id": "inv-03",
        "family": "invoice_consistency",
        "fixture": "distinct-invoices",
        "mutation": "none",
    },
    {
        "case_id": "inv-04",
        "family": "invoice_consistency",
        "fixture": "distinct-invoices",
        "mutation": "shared-event",
    },
    {
        "case_id": "inv-05",
        "family": "invoice_consistency",
        "fixture": "existing-refund",
        "mutation": "none",
    },
    {
        "case_id": "inv-06",
        "family": "invoice_consistency",
        "fixture": "pending-refund",
        "mutation": "none",
    },
    {
        "case_id": "pay-01",
        "family": "payment_progress",
        "fixture": "pending-payment",
        "mutation": "none",
    },
    {
        "case_id": "pay-02",
        "family": "payment_progress",
        "fixture": "pending-payment",
        "mutation": "operator-followup",
    },
    {
        "case_id": "pay-03",
        "family": "payment_progress",
        "fixture": "pending-payment",
        "mutation": "settle-pending",
    },
    {
        "case_id": "pay-04",
        "family": "payment_progress",
        "fixture": "existing-refund",
        "mutation": "delete-refund",
    },
    {
        "case_id": "pay-05",
        "family": "payment_progress",
        "fixture": "pending-refund",
        "mutation": "delete-refund",
    },
    {
        "case_id": "pay-06",
        "family": "payment_progress",
        "fixture": "distinct-invoices",
        "mutation": "make-pending",
    },
)


def use_baseline(api_app) -> None:
    api_app.state.provider_factory = BaselineWorkflow


_CASES = {
    "inv-01": lambda c, s, m, a: _proposal_case(c, s, m, a, "duplicate-approve", 4200),
    "inv-02": lambda c, s, m, a: _proposal_case(c, s, m, a, "duplicate-reject", 2500),
    "inv-03": lambda c, s, m, a: _handoff_case(c, s, m, a, "distinct-invoices", search=False),
    "inv-04": lambda c, s, m, a: _handoff_case(c, s, m, a, "distinct-invoices", search=True),
    "inv-05": lambda c, s, m, a: _handoff_case(c, s, m, a, "existing-refund", search=False),
    "inv-06": lambda c, s, m, a: _handoff_case(c, s, m, a, "pending-refund", search=False),
    "pay-01": _pending,
    "pay-02": _pending_followup,
    "pay-03": _settled_pending_proposes,
    "pay-04": lambda c, s, m, a: _refund_removed_proposes(c, s, m, a, "existing-refund", 1500),
    "pay-05": lambda c, s, m, a: _refund_removed_proposes(c, s, m, a, "pending-refund", 2000),
    "pay-06": _distinct_with_pending_note,
    "pol-01": lambda c, s, m, a: _proposal_case(c, s, m, a, "duplicate-approve", 4200),
    "pol-02": lambda c, s, m, a: _handoff_case(c, s, m, a, "distinct-invoices", search=True),
    "pol-03": _approver_hidden,
    "pol-04": _retired_hidden,
    "auth-01": _support_cannot_approve,
    "auth-02": _approver_rejects,
    "auth-03": _withdrawal,
    "auth-04": _stale_payment,
    "safe-01": _one_refund,
    "safe-02": _lost_response,
    "safe-03": _model_limit,
    "safe-04": _live_blocked,
}
