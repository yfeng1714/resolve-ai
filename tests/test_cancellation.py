"""Period-end cancellation. These tests do not call a paid model."""

from __future__ import annotations

import os
import subprocess
import sys

from sqlalchemy import text
from tests.support import approver_headers, login, support_headers
from tests.test_auth_and_workflow import _refund_count

from resolveai.db.session import make_engine
from resolveai.settings import repo_root
from resolveai.worker.__main__ import drain

ONE = "55555555-5555-4555-8555-555555555521"
TWO = "55555555-5555-4555-8555-555555555522"
SCHEDULED = "55555555-5555-4555-8555-555555555523"
MIXED = "55555555-5555-4555-8555-555555555524"
ELAPSED = "55555555-5555-4555-8555-555555555525"
IMMEDIATE = "55555555-5555-4555-8555-555555555526"
COMBINED = "55555555-5555-4555-8555-555555555527"
HARBOR = "55555555-5555-4555-8555-555555555531"
STANDARD = "99999999-9999-4999-8999-999999999901"
PLUS = "99999999-9999-4999-8999-999999999903"
HARBOR_SUB = "99999999-9999-4999-8999-999999999911"


def _investigate(client, ticket_id: str, headers: dict[str, str]) -> dict:
    response = client.post(f"/tickets/{ticket_id}/runs", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _approve_execute(client, detail: dict, approver: dict[str, str]) -> dict:
    proposal = detail["proposals"][-1]
    body = {"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]}
    approved = client.post(f"/proposals/{proposal['id']}/approve", headers=approver, json=body)
    assert approved.status_code == 200, approved.text
    executed = client.post(f"/proposals/{proposal['id']}/execute", headers=approver, json=body)
    assert executed.status_code == 200, executed.text
    return proposal


def _subscription(settings, subscription_id: str) -> dict:
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT cancel_at_period_end, resource_version, status,
                       current_period_end
                FROM simulator.subscription WHERE id = :id
                """
            ),
            {"id": subscription_id},
        ).one()
    engine.dispose()
    return {
        "flag": row.cancel_at_period_end,
        "version": row.resource_version,
        "status": row.status,
        "ends": row.current_period_end,
    }


def _operations(settings, action_prefix: str) -> int:
    engine = make_engine(settings.simulator_database_url)
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM simulator.operation WHERE action_id LIKE :prefix"),
            {"prefix": action_prefix},
        ).scalar_one()
    engine.dispose()
    return int(count)


def test_each_tenant_schedules_its_own_period_end(client, settings, manifest) -> None:
    del manifest
    north = _investigate(client, ONE, support_headers(client))
    assert north["proposals"][-1]["action_type"] == "cancel_at_period_end"
    assert "Cancellation scheduled" not in "".join(
        reply["body"] for reply in north["replies"]
    )
    proposal = _approve_execute(client, north, approver_headers(client))
    assert drain(settings, limit=3) >= 1
    state = _subscription(settings, STANDARD)
    assert state["flag"] is True
    assert state["status"] == "active"
    assert state["version"] == 2
    assert _operations(settings, proposal["action_id"]) == 1
    again = approver_headers(client)
    repeated = client.post(
        f"/proposals/{proposal['id']}/execute",
        headers=again,
        json={"revision": proposal["revision"], "payload_hash": proposal["payload_hash"]},
    )
    assert repeated.status_code == 200, repeated.text
    assert _operations(settings, proposal["action_id"]) == 1
    assert state["version"] == _subscription(settings, STANDARD)["version"]

    harbor_headers = login(client, "support.harbor@demo.resolveai.test", "support-harbor-pass")
    harbor = _investigate(client, HARBOR, harbor_headers)
    harbor_approver = login(
        client, "approver.harbor@demo.resolveai.test", "approver-harbor-pass"
    )
    harbor_proposal = _approve_execute(client, harbor, harbor_approver)
    assert drain(settings, limit=3) >= 1
    harbor_state = _subscription(settings, HARBOR_SUB)
    assert harbor_state["flag"] is True
    assert harbor_state["version"] == 2
    assert _operations(settings, harbor_proposal["action_id"]) == 1
    assert _refund_count(settings, "66666666-6666-4666-8666-666666666612") == 0


def test_clarification_choice_and_gates(client, settings) -> None:
    support = support_headers(client)
    waiting = _investigate(client, TWO, support)
    assert waiting["proposals"] == []
    assert waiting["runs"][-1]["state"] == "awaiting_customer"
    missing = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support,
        json={"subscription_id": HARBOR_SUB},
    )
    assert missing.status_code == 409
    chosen = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support,
        json={"subscription_id": PLUS},
    )
    assert chosen.status_code == 200, chosen.text
    resumed = _investigate(client, TWO, support)
    proposal = resumed["proposals"][-1]
    assert proposal["target_subscription_id"] == PLUS
    assert proposal["amount_cents"] is None
    _approve_execute(client, resumed, approver_headers(client))
    drain(settings, limit=3)
    assert _subscription(settings, PLUS)["flag"] is True
    assert _subscription(settings, "99999999-9999-4999-8999-999999999902")["flag"] is False

    explained = _investigate(client, SCHEDULED, support_headers(client))
    assert explained["proposals"] == []
    mixed = _investigate(client, MIXED, support_headers(client))
    assert mixed["proposals"] == []
    assert mixed["runs"][-1]["state"] == "awaiting_customer"
    elapsed = _investigate(client, ELAPSED, support_headers(client))
    assert elapsed["proposals"] == []
    immediate = _investigate(client, IMMEDIATE, support_headers(client))
    assert immediate["proposals"] == []
    combined = _investigate(client, COMBINED, support_headers(client))
    kinds = {item["action_type"] for item in combined["proposals"]}
    assert kinds == {"refund", "cancel_at_period_end"}
    assert combined["runs"][-1]["state"] == "awaiting_approval"


def test_downgrade_refuses_a_choice_and_a_populated_cancellation(settings, client) -> None:
    support = support_headers(client)
    _investigate(client, TWO, support)
    chosen = client.post(
        f"/tickets/{TWO}/subscription-choice",
        headers=support,
        json={"subscription_id": PLUS},
    )
    assert chosen.status_code == 200, chosen.text
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT count(*) FROM app.subscription_choice")
        ).scalar_one()
        choice_hash = connection.execute(
            text(
                """
                SELECT md5(string_agg(
                  id::text || subscription_id::text || input_revision::text,
                  ',' ORDER BY id
                ))
                FROM app.subscription_choice
                """
            )
        ).scalar_one()
    engine.dispose()
    assert before == 1
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": settings.database_url,
            "PYTHONPATH": str(repo_root() / "src"),
        }
    )
    refused = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "20261027_0007"],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    engine = make_engine(settings.database_url)
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        after = connection.execute(
            text("SELECT count(*) FROM app.subscription_choice")
        ).scalar_one()
        after_hash = connection.execute(
            text(
                """
                SELECT md5(string_agg(
                  id::text || subscription_id::text || input_revision::text,
                  ',' ORDER BY id
                ))
                FROM app.subscription_choice
                """
            )
        ).scalar_one()
    engine.dispose()
    assert version == "20261112_0014"
    assert after == before
    assert after_hash == choice_hash
