"""Immutable refund proposals. A change creates a new revision."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import Proposal
from resolveai.domain.hashing import canonical_hash, refund_action_id, refund_payload
from resolveai.domain.policy import load_refund_policy
from resolveai.errors import APIError
from resolveai.services.audit import audit
from resolveai.services.input import latest_input_revision


def create_refund_proposal(
    session: Session,
    *,
    tenant_id: UUID,
    ticket_id: UUID,
    run_id: UUID,
    actor_user_id: UUID | None,
    account_id: UUID,
    payment_id: UUID,
    event_id: str,
    amount_cents: int,
    payment_version: int,
    evidence: dict[str, Any],
    observed_input_revision: int | None = None,
) -> Proposal:
    policy_version = load_refund_policy().version
    action_id = refund_action_id(payment_id)
    payload = refund_payload(
        action_id=action_id,
        account_id=account_id,
        payment_id=payment_id,
        event_id=event_id,
        amount_cents=amount_cents,
        policy_version=policy_version,
    )
    digest = canonical_hash(payload)
    current_input = latest_input_revision(session, ticket_id)
    if observed_input_revision is not None and observed_input_revision != current_input:
        raise APIError(
            409,
            "stale_input",
            "The investigation did not observe the current request",
        )
    bound_input = current_input if observed_input_revision is None else observed_input_revision
    preconditions = {
        "account_id": str(account_id),
        "payment_id": str(payment_id),
        "event_id": event_id,
        "payment_resource_version": payment_version,
        "policy_version": policy_version,
        "amount_cents": amount_cents,
        "currency": "USD",
        "input_revision": bound_input,
    }
    rows = session.scalars(
        select(Proposal)
        .where(Proposal.tenant_id == tenant_id, Proposal.action_id == action_id)
        .order_by(Proposal.revision)
    ).all()
    if any(row.status == "executed" for row in rows):
        raise APIError(409, "already_executed", "This payment already has an executed refund")
    current = next((row for row in rows if row.status in {"open", "approved"}), None)
    if (
        current is not None
        and current.payload_hash == digest
        and current.preconditions == preconditions
    ):
        return current
    if current is not None:
        current.status = "superseded"
    revision = (rows[-1].revision if rows else 0) + 1
    proposal = Proposal(
        tenant_id=tenant_id,
        ticket_id=ticket_id,
        run_id=run_id,
        action_type="refund",
        action_id=action_id,
        revision=revision,
        payload=payload,
        payload_hash=digest,
        policy_version=policy_version,
        target_payment_id=payment_id,
        target_event_id=event_id,
        amount_cents=amount_cents,
        currency="USD",
        preconditions=preconditions,
        evidence=evidence,
        status="open",
        created_at=utcnow(),
    )
    session.add(proposal)
    session.flush()
    audit(
        session,
        tenant_id=tenant_id,
        event_type="proposal_created",
        actor_user_id=actor_user_id,
        ticket_id=ticket_id,
        run_id=run_id,
        payload={"proposal_id": str(proposal.id), "revision": revision, "action_id": action_id},
    )
    return proposal
