from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.support import case_by_key

from resolveai.db.session import make_engine
from resolveai.domain.hashing import canonical_hash, refund_action_id, refund_payload
from resolveai.domain.policy import load_refund_policy

AVERY_DUPLICATE = "66666666-6666-4666-8666-666666666612"
AVERY_ORIGINAL = "66666666-6666-4666-8666-666666666611"


def _refund_body(case: dict, payment_id: str, amount_cents: int, version: int = 1) -> dict:
    payment = next(item for item in case["payments"] if item["id"] == payment_id)
    payload = refund_payload(
        action_id=refund_action_id(payment_id),
        account_id=case["account_id"],
        payment_id=payment_id,
        event_id=payment["event_id"],
        amount_cents=amount_cents,
        policy_version=load_refund_policy().version,
    )
    return {
        **payload,
        "payload_hash": canonical_hash(payload),
        "expected_payment_version": version,
    }


def _headers(settings, account_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.simulator_token}",
        "X-Scope-Account-Id": account_id,
    }


def test_idempotent_concurrent_and_conflicting_refunds(
    settings, simulator_app, seeded, manifest
) -> None:
    del simulator_app, seeded
    case = case_by_key(manifest, "duplicate-approve")
    body = _refund_body(case, AVERY_DUPLICATE, 4200)
    headers = _headers(settings, case["account_id"])

    def post(payload: dict) -> httpx.Response:
        return httpx.post(
            f"{settings.simulator_base_url}/v1/refunds",
            json=payload,
            headers=headers,
            timeout=5,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(body), range(2)))
    assert [response.status_code for response in responses] == [200, 200]
    oracle = make_engine(settings.oracle_database_url)
    with oracle.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM simulator.refund WHERE payment_id = :id"),
            {"id": AVERY_DUPLICATE},
        ).scalar_one()
        replay = connection.execute(
            text("SELECT replay_count FROM simulator.operation WHERE action_id = :id"),
            {"id": refund_action_id(AVERY_DUPLICATE)},
        ).scalar_one()
    oracle.dispose()
    assert count == 1
    assert replay == 1

    conflict = _refund_body(case, AVERY_DUPLICATE, 4100, version=2)
    rejected = post(conflict)
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "payload_conflict"

    other = _refund_body(case, AVERY_ORIGINAL, 4200, version=1)
    ineligible = post(other)
    assert ineligible.status_code == 422
    assert ineligible.json()["error_code"] == "not_eligible"
    with make_engine(settings.oracle_database_url).connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM simulator.refund WHERE event_id = 'evt-avery-1001'")
        ).scalar_one()
    assert count == 1


def test_event_constraint_rejects_a_second_open_refund(settings, seeded, manifest) -> None:
    del seeded
    case = case_by_key(manifest, "duplicate-approve")
    engine = make_engine(settings.simulator_database_url)
    connection = engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            text(
                """
                INSERT INTO simulator.refund (
                  id, account_id, payment_id, event_id, amount_cents, currency,
                  status, action_id, operation_id, created_at
                ) VALUES (
                  '88888888-8888-4888-8888-888888888811', :account, :payment, 'evt-avery-1001',
                  4200, 'USD', 'succeeded', :action, 'op-test', now()
                )
                """
            ),
            {
                "account": case["account_id"],
                "payment": AVERY_DUPLICATE,
                "action": refund_action_id(AVERY_DUPLICATE),
            },
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    """
                    INSERT INTO simulator.refund (
                      id, account_id, payment_id, event_id, amount_cents, currency,
                      status, action_id, operation_id, created_at
                    ) VALUES (
                      '88888888-8888-4888-8888-888888888812', :account, :payment, 'evt-avery-1001',
                      100, 'USD', 'pending', 'refund:other-action', 'op-test-2', now()
                    )
                    """
                ),
                {"account": case["account_id"], "payment": AVERY_ORIGINAL},
            )
    finally:
        transaction.rollback()
        connection.close()
        engine.dispose()


def test_fault_header_is_ignored_unless_enabled(settings, simulator_app, seeded, manifest) -> None:
    del simulator_app, seeded
    case = case_by_key(manifest, "duplicate-approve")
    body = _refund_body(case, AVERY_DUPLICATE, 4200)
    headers = _headers(settings, case["account_id"])
    headers["X-ResolveAI-Test-Fault"] = "commit_then_fail"
    response = httpx.post(
        f"{settings.simulator_base_url}/v1/refunds",
        json=body,
        headers=headers,
        timeout=5,
    )
    assert response.status_code == 200
    assert response.json()["data"]["replayed"] is False


def test_scoped_reads_reject_a_mismatched_account(
    settings, simulator_app, seeded, manifest
) -> None:
    del simulator_app, seeded
    case = case_by_key(manifest, "duplicate-approve")
    other = case_by_key(manifest, "duplicate-reject")
    response = httpx.get(
        f"{settings.simulator_base_url}/v1/accounts/{case['account_id']}",
        headers=_headers(settings, other["account_id"]),
        timeout=5,
    )
    assert response.status_code == 403
    denied = httpx.get(
        f"{settings.simulator_base_url}/v1/accounts/{case['account_id']}",
        timeout=5,
    )
    assert denied.status_code == 401


def test_same_event_on_different_invoices_is_rejected(
    settings, simulator_app, seeded, manifest
) -> None:
    del simulator_app, seeded
    case = case_by_key(manifest, "distinct-invoices")
    payment_id = "66666666-6666-4666-8666-666666666632"
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
    payload = refund_payload(
        action_id=refund_action_id(payment_id),
        account_id=case["account_id"],
        payment_id=payment_id,
        event_id="evt-invoice-conflict",
        amount_cents=1800,
        policy_version=load_refund_policy().version,
    )
    response = httpx.post(
        f"{settings.simulator_base_url}/v1/refunds",
        json={**payload, "payload_hash": canonical_hash(payload), "expected_payment_version": 1},
        headers=_headers(settings, case["account_id"]),
        timeout=5,
    )
    assert response.status_code == 422
    assert response.json()["error_code"] == "not_eligible"
    oracle = make_engine(settings.oracle_database_url)
    with oracle.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM simulator.refund WHERE payment_id = :id"),
            {"id": payment_id},
        ).scalar_one()
    oracle.dispose()
    assert count == 0
