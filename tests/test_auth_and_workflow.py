from uuid import UUID

from sqlalchemy import text
from tests.support import ORIGIN, approver_headers, case_by_key, support_headers

from resolveai.agent.provider import FakeProvider, ProviderAction
from resolveai.clock import utcnow
from resolveai.db.session import make_engine
from resolveai.services.proposals import create_refund_proposal
from resolveai.tools.client import SimulatorClient, ToolResult


def _refund_count(settings, payment_id: str) -> int:
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM simulator.refund WHERE payment_id = :id"),
            {"id": payment_id},
        ).scalar_one()
    engine.dispose()
    return int(count)


def _replay_count(settings, action_id: str) -> int:
    engine = make_engine(settings.oracle_database_url)
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT replay_count FROM simulator.operation WHERE action_id = :id"),
            {"id": action_id},
        ).scalar_one()
    engine.dispose()
    return int(count)


def _investigate(client, ticket_id: str) -> dict:
    headers = support_headers(client)
    response = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_session_guards_and_role_separation(client, manifest) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    missing = client.get(f"/tickets/{case['ticket_id']}")
    assert missing.status_code == 401
    bad_login = client.post(
        "/sessions",
        json={"email": "support@demo.resolveai.test", "password": "nope"},
        headers={"Origin": ORIGIN},
    )
    assert bad_login.status_code == 401
    support_headers(client)
    guessed = client.get("/tickets/99999999-9999-4999-8999-999999999999")
    assert guessed.status_code == 404
    no_origin = client.post(f"/tickets/{case['ticket_id']}/runs")
    assert no_origin.status_code == 403
    bad_csrf = client.post(
        f"/tickets/{case['ticket_id']}/runs",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "not-the-token"},
    )
    assert bad_csrf.status_code == 403
    detail = _investigate(client, case["ticket_id"])
    proposal = detail["proposals"][0]
    support = {"Origin": ORIGIN, "X-CSRF-Token": client.cookies.get("resolveai_csrf")}
    denied = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=support,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert denied.status_code == 403
    approver = approver_headers(client)
    cannot_run = client.post(f"/tickets/{case['ticket_id']}/runs", headers=approver)
    assert cannot_run.status_code == 403


def test_approved_refund_is_exact_and_rejection_has_no_side_effect(
    client, settings, manifest
) -> None:
    approve_case = case_by_key(manifest, "duplicate-approve")
    reject_case = case_by_key(manifest, "duplicate-reject")
    detail = _investigate(client, approve_case["ticket_id"])
    proposal = detail["proposals"][0]
    assert proposal["amount_cents"] == 4200
    assert "10000" in detail["messages"][0]["body"]
    assert proposal["currency"] == "USD"
    approver = approver_headers(client)
    approved = client.post(
        f"/proposals/{proposal['id']}/approve",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert approved.status_code == 200
    executed = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert executed.status_code == 200, executed.text
    action = executed.json()["data"]["actions"][0]
    assert action["status"] == "confirmed"
    reply = executed.json()["data"]["replies"][0]
    assert "$42.00" in reply["body"]
    assert action["operation_id"] in reply["body"]
    assert "We confirmed a refund" in reply["body"]
    sent = client.post(
        f"/reply-drafts/{reply['id']}/approve-and-send",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert sent.status_code == 200, sent.text
    sent_reply = sent.json()["data"]["replies"][0]
    assert sent_reply["status"] == "sent"
    assert sent.json()["data"]["ticket"]["status"] == "reply_sent"
    again = client.post(
        f"/reply-drafts/{reply['id']}/approve-and-send",
        headers=approver,
        json={"revision": reply["revision"], "body_hash": reply["body_hash"]},
    )
    assert again.status_code == 200
    resent = again.json()["data"]["replies"][0]["receipt"]["receipt_id"]
    assert resent == sent_reply["receipt"]["receipt_id"]
    duplicate = approve_case["payments"][1]["id"]
    assert _refund_count(settings, duplicate) == 1
    assert _replay_count(settings, proposal["action_id"]) == 0

    rejected_detail = _investigate(client, reject_case["ticket_id"])
    rejected_proposal = rejected_detail["proposals"][0]
    approver = approver_headers(client)
    decision = client.post(
        f"/proposals/{rejected_proposal['id']}/reject",
        headers=approver,
        json={
            "revision": rejected_proposal["revision"],
            "payload_hash": rejected_proposal["payload_hash"],
        },
    )
    assert decision.status_code == 200, decision.text
    draft = decision.json()["data"]["replies"][0]
    assert "No refund was issued" in draft["body"]
    assert decision.json()["data"]["actions"] == []
    refused = client.post(
        f"/reply-drafts/{draft['id']}/reject",
        headers=approver,
        json={"revision": draft["revision"], "body_hash": draft["body_hash"]},
    )
    assert refused.status_code == 200
    assert refused.json()["data"]["replies"][0]["status"] == "rejected"
    assert refused.json()["data"]["replies"][0]["receipt"] is None
    assert _refund_count(settings, reject_case["payments"][1]["id"]) == 0


def test_ineligible_cases_do_not_propose(client, settings, manifest) -> None:
    expected = {
        "distinct-invoices": "handed_off",
        "pending-payment": "awaiting_customer",
        "existing-refund": "handed_off",
        "pending-refund": "handed_off",
    }
    for key, state in expected.items():
        case = case_by_key(manifest, key)
        detail = _investigate(client, case["ticket_id"])
        assert detail["runs"][-1]["state"] == state
        assert detail["proposals"] == []
        refunded = {row["payment_id"] for row in case["refunds"]}
        for payment in case["payments"]:
            expected = 1 if payment["id"] in refunded else 0
            assert _refund_count(settings, payment["id"]) == expected


def test_stale_and_edited_proposals_do_not_write(client, api_app, settings, manifest) -> None:
    case = case_by_key(manifest, "duplicate-approve")
    detail = _investigate(client, case["ticket_id"])
    proposal = detail["proposals"][0]
    approver = approver_headers(client)
    assert (
        client.post(
            f"/proposals/{proposal['id']}/approve",
            headers=approver,
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        ).status_code
        == 200
    )
    engine = make_engine(settings.simulator_database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE simulator.payment
                SET resource_version = resource_version + 1
                WHERE id = :id
                """
            ),
            {"id": proposal["target_payment_id"]},
        )
    engine.dispose()
    stale = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=approver,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "stale_precondition"
    assert _refund_count(settings, proposal["target_payment_id"]) == 0

    fresh = _investigate(client, case_by_key(manifest, "duplicate-reject")["ticket_id"])
    approver = approver_headers(client)
    original = fresh["proposals"][0]
    session = api_app.state.session_factory()
    try:
        revised = create_refund_proposal(
            session,
            tenant_id=UUID(manifest["tenant"]["id"]),
            ticket_id=UUID(case_by_key(manifest, "duplicate-reject")["ticket_id"]),
            run_id=UUID(original["run_id"]),
            actor_user_id=None,
            account_id=UUID(case_by_key(manifest, "duplicate-reject")["account_id"]),
            payment_id=UUID(original["target_payment_id"]),
            event_id=original["target_event_id"],
            amount_cents=original["amount_cents"] + 100,
            payment_version=int(original["preconditions"]["payment_resource_version"]),
            evidence=original["evidence"],
        )
        session.commit()
        revised_id = revised.id
        revised_revision = revised.revision
        revised_hash = revised.payload_hash
    finally:
        session.close()
    old = client.post(
        f"/proposals/{original['id']}/approve",
        headers=approver,
        json={"revision": original["revision"], "payload_hash": original["payload_hash"]},
    )
    assert old.status_code == 409
    approved = client.post(
        f"/proposals/{revised_id}/approve",
        headers=approver,
        json={"revision": revised_revision, "payload_hash": revised_hash},
    )
    assert approved.status_code == 200, approved.text
    executed = client.post(
        f"/proposals/{revised_id}/execute",
        headers=approver,
        json={"revision": revised_revision, "payload_hash": revised_hash},
    )
    assert executed.status_code == 409
    assert executed.json()["error_code"] == "policy_rejected"
    assert _refund_count(settings, original["target_payment_id"]) == 0


def test_unknown_write_is_queried_not_repeated(client, settings, manifest, restore_runtime) -> None:
    del restore_runtime
    settings.simulator_allow_faults = True
    case = case_by_key(manifest, "duplicate-approve")
    detail = _investigate(client, case["ticket_id"])
    proposal = detail["proposals"][0]
    approver = approver_headers(client)
    assert (
        client.post(
            f"/proposals/{proposal['id']}/approve",
            headers=approver,
            json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
        ).status_code
        == 200
    )
    executed = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers={**approver, "X-ResolveAI-Test-Fault": "commit_then_fail"},
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert executed.status_code == 200, executed.text
    action = executed.json()["data"]["actions"][0]
    assert action["status"] == "confirmed"
    assert action["result"]["recovered_from_unknown"] is True
    assert _refund_count(settings, proposal["target_payment_id"]) == 1
    assert _replay_count(settings, proposal["action_id"]) == 0
    repeated = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers={**approver, "X-ResolveAI-Test-Fault": "commit_then_fail"},
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert repeated.status_code == 200
    assert _refund_count(settings, proposal["target_payment_id"]) == 1
    assert _replay_count(settings, proposal["action_id"]) == 0


def test_budget_replan_and_ignored_amount(
    client, api_app, settings, manifest, restore_runtime
) -> None:
    del restore_runtime
    case = case_by_key(manifest, "duplicate-approve")
    settings.run_max_model_calls = 1
    limited = _investigate(client, case["ticket_id"])
    assert limited["runs"][-1]["state"] == "failed"
    assert limited["runs"][-1]["error_code"] == "budget_exhausted"
    assert limited["proposals"] == []
    assert _refund_count(settings, case["payments"][1]["id"]) == 0
    settings.run_max_model_calls = 12

    class ReplanProvider:
        prompt_version = "test-replan"

        def next_action(self, snapshot):
            del snapshot
            return ProviderAction(route="replan", reason="Looking again without a new observation.")

    api_app.state.provider_factory = ReplanProvider
    replanned = _investigate(client, case_by_key(manifest, "duplicate-reject")["ticket_id"])
    assert replanned["runs"][-1]["state"] == "failed"
    assert replanned["runs"][-1]["replans"] > 2
    assert replanned["proposals"] == []

    class GreedyProvider(FakeProvider):
        def next_action(self, snapshot):
            action = super().next_action(snapshot)
            if action.tool_name == "propose_refund":
                return ProviderAction(
                    route="tool",
                    tool_name="propose_refund",
                    reason=action.reason,
                    arguments={"amount_cents": 1_000_000},
                )
            return action

    api_app.state.provider_factory = GreedyProvider
    eligible = _investigate(client, case_by_key(manifest, "duplicate-reject")["ticket_id"])
    proposal = eligible["proposals"][0]
    assert proposal["amount_cents"] == 2500
    invocation = next(
        item for item in eligible["runs"][-1]["observations"] if item["tool"] == "propose_refund"
    )
    assert invocation["status"] == "ok"
    assert "amount_cents" in invocation["ignored_fields"]


def test_read_retry_then_proposes(client, api_app, manifest, restore_runtime) -> None:
    del restore_runtime

    class FlakyClient:
        def __init__(self, inner: SimulatorClient) -> None:
            self.inner = inner
            self.used = False

        def get_customer_account(self, account_id):
            if not self.used:
                self.used = True
                return ToolResult(
                    status="error",
                    data={},
                    error_code="unavailable",
                    retryable=True,
                    operation_id=None,
                    resource_version=None,
                    observed_at=utcnow().isoformat(),
                    request_id="retry-1",
                )
            return self.inner.get_customer_account(account_id)

        def close(self) -> None:
            self.inner.close()

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

    api_app.state.tool_client_factory = lambda app_settings: FlakyClient(
        SimulatorClient(app_settings)
    )
    case = case_by_key(manifest, "duplicate-approve")
    detail = _investigate(client, case["ticket_id"])
    assert detail["runs"][-1]["read_retries"] == 1
    assert detail["proposals"][0]["amount_cents"] == 4200


def test_chinese_reply_follows_the_request_locale(client, manifest) -> None:
    case = case_by_key(manifest, "duplicate-reject")
    detail = _investigate(client, case["ticket_id"])
    proposal = detail["proposals"][0]
    headers = {
        **approver_headers(client),
        "X-ResolveAI-Locale": "zh-CN",
    }
    rejected = client.post(
        f"/proposals/{proposal['id']}/reject",
        headers=headers,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert rejected.status_code == 200, rejected.text
    body = rejected.json()["data"]["replies"][0]["body"]
    assert "未发放退款" in body
    assert "No refund was issued" not in body
    assert rejected.headers["content-language"] == "zh-CN"


def test_health_exposes_no_credentials(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    text = response.text
    assert "resolveai_local" not in text
    assert "password" not in text.lower()
    assert response.json()["data"]["llm_budget_usd"] == "0"
    assert response.json()["data"]["model_provider"] == "fake"
    assert response.json()["data"]["allow_live"] is False
    assert response.json()["data"]["run_max_model_calls"] == 12
    assert "sk-" not in text
