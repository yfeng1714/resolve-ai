"""Period-end proposals, support choices, and receipt checks.

The choice row is the authority. The operator message only advances the
ticket's input revision and is not parsed for a subscription id.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import Message, Proposal, SubscriptionChoice, Ticket
from resolveai.domain.cancellation import (
    SubscriptionView,
    attempt_hash,
    cancellation_action_id,
    choice_confirms_immediate,
    effect_hash,
    effect_payload,
    intent_gate,
    latest_immediate_revision,
    parse_instant,
)
from resolveai.errors import APIError
from resolveai.services.audit import audit
from resolveai.services.input import (
    invalidate_pending_intent,
    latest_input_revision,
)
from resolveai.services.replies import append_message
from resolveai.services.tenancy import require_bound_account
from resolveai.settings import repo_root


def load_subscription_policy_version() -> str:
    path = repo_root() / "policies" / "subscription_policy_v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return str(raw["policy_version"])


def current_choice(session: Session, ticket_id: UUID) -> SubscriptionChoice | None:
    return session.scalar(
        select(SubscriptionChoice)
        .where(SubscriptionChoice.ticket_id == ticket_id)
        .order_by(SubscriptionChoice.input_revision.desc(), SubscriptionChoice.id.desc())
        .limit(1)
    )


def subscription_views(payload: dict[str, Any] | None) -> list[SubscriptionView]:
    rows = (payload or {}).get("subscriptions") or []
    views: list[SubscriptionView] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        views.append(
            SubscriptionView(
                id=str(row.get("id") or ""),
                plan_code=str(row.get("plan_code") or ""),
                status=str(row.get("status") or ""),
                cancel_at_period_end=bool(row.get("cancel_at_period_end")),
                current_period_end=str(row.get("current_period_end") or ""),
                resource_version=int(row.get("resource_version") or 0),
            )
        )
    return views


def record_subscription_choice(
    session: Session,
    *,
    actor: Any,
    ticket: Ticket,
    subscription_id: UUID,
    tools: Any,
) -> SubscriptionChoice:
    if actor.role != "support":
        raise APIError(403, "denied", "Only support can record a subscription choice")
    require_bound_account(session, ticket)
    read = tools.list_subscriptions(ticket.account_id)
    if read.status != "ok":
        raise APIError(503, "unavailable", "Subscriptions could not be read", retryable=True)
    views = subscription_views(read.data)
    chosen = next((row for row in views if row.id == str(subscription_id)), None)
    if chosen is None:
        raise APIError(409, "choice_not_owned", "That subscription is not on this account")
    message = append_message(
        session,
        ticket=ticket,
        author_kind="operator",
        body="A period-end subscription choice was recorded.",
    )
    choice = SubscriptionChoice(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        account_id=ticket.account_id,
        customer_id=ticket.customer_id,
        subscription_id=subscription_id,
        actor_user_id=actor.user_id,
        input_revision=message.revision,
        created_at=utcnow(),
    )
    session.add(choice)
    session.flush()
    invalidate_pending_intent(session, ticket)
    from resolveai.services.recovery import cancel_pre_admission

    cancel_pre_admission(session, ticket)
    audit(
        session,
        tenant_id=ticket.tenant_id,
        event_type="subscription_choice_recorded",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        payload={
            "choice_id": str(choice.id),
            "subscription_id": str(subscription_id),
            "input_revision": choice.input_revision,
        },
    )
    return choice


def create_cancellation_proposal(
    session: Session,
    *,
    tenant_id: UUID,
    ticket_id: UUID,
    run_id: UUID,
    actor_user_id: UUID | None,
    account_id: UUID,
    subscription: SubscriptionView,
    evidence: dict[str, Any],
    observed_input_revision: int,
    choice_id: UUID | None,
) -> Proposal:
    _require_period_end_intent(session, ticket_id, choice_id)
    policy_version = load_subscription_policy_version()
    effective_at = subscription.current_period_end
    parse_instant(effective_at)
    action_id = cancellation_action_id(str(subscription.id), effective_at)
    effect = effect_payload(
        action_id=action_id,
        account_id=str(account_id),
        subscription_id=str(subscription.id),
        effective_at=effective_at,
        policy_version=policy_version,
    )
    digest = attempt_hash(effect, subscription.resource_version)
    stable = effect_hash(effect)
    current_input = latest_input_revision(session, ticket_id)
    if observed_input_revision != current_input:
        raise APIError(409, "stale_input", "The investigation did not observe the current request")
    if _action_was_admitted(session, tenant_id, action_id):
        raise APIError(
            409,
            "period_already_dispatched",
            "This period was already dispatched and cannot be replaced",
        )
    preconditions = {
        "account_id": str(account_id),
        "subscription_id": str(subscription.id),
        "effective_at": effective_at,
        "subscription_resource_version": subscription.resource_version,
        "policy_version": policy_version,
        "input_revision": observed_input_revision,
        "choice_id": None if choice_id is None else str(choice_id),
        "effect_hash": stable,
    }
    rows = session.scalars(
        select(Proposal)
        .where(Proposal.tenant_id == tenant_id, Proposal.action_id == action_id)
        .order_by(Proposal.revision)
    ).all()
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
        action_type="cancel_at_period_end",
        action_id=action_id,
        revision=revision,
        payload=attempt_payload_from(effect, subscription.resource_version),
        payload_hash=digest,
        policy_version=policy_version,
        target_payment_id=None,
        target_event_id=None,
        amount_cents=None,
        currency=None,
        target_subscription_id=UUID(subscription.id),
        effective_at=parse_instant(effective_at),
        subscription_resource_version=subscription.resource_version,
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


def attempt_payload_from(effect: dict[str, Any], version: int) -> dict[str, Any]:
    from resolveai.domain.cancellation import attempt_payload

    return attempt_payload(effect, version)


def receipt_matches(
    command: dict[str, Any], operation: dict[str, Any], subscription: dict[str, Any]
) -> bool:
    """A confirmation needs this operation's typed result, not a partial payload."""
    result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
    expected = command.get("expected_resource_version")
    version = result.get("resource_version")
    operation_id = operation.get("operation_id")
    result_operation_id = result.get("operation_id")
    if operation.get("status") != "confirmed" or result.get("operation_status") != "confirmed":
        return False
    if not _uuid_text(operation_id) or not _uuid_text(result_operation_id):
        return False
    if operation_id != result_operation_id:
        return False
    if type(expected) is not int or type(version) is not int or version != expected + 1:
        return False
    if result.get("cancel_at_period_end") is not True:
        return False
    action_id = command.get("action_id")
    return (
        operation.get("action_id") == action_id
        and result.get("action_id") == action_id
        and operation.get("payload_hash") == command.get("effect_hash")
        and result.get("account_id") == command.get("account_id")
        and result.get("subscription_id") == command.get("subscription_id")
        and result.get("effective_at") == command.get("effective_at")
        and subscription.get("id") == command.get("subscription_id")
        and subscription.get("cancel_at_period_end") is True
        and subscription.get("current_period_end") == command.get("effective_at")
    )


def _uuid_text(value: Any) -> bool:
    if not isinstance(value, str) or value == "":
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _require_period_end_intent(
    session: Session, ticket_id: UUID, choice_id: UUID | None
) -> None:
    """Provider tool choice cannot skip the fixed customer-intent gates."""
    rows = session.scalars(
        select(Message)
        .where(
            Message.ticket_id == ticket_id,
            Message.author_kind.in_(("customer", "operator")),
        )
        .order_by(Message.revision)
    ).all()
    messages = [{"revision": row.revision, "body": row.body} for row in rows]
    gate = intent_gate(messages)
    if gate == "combined_intent":
        if latest_immediate_revision(messages) is not None:
            choice = current_choice(session, ticket_id)
            revision = None
            if choice is not None and choice_id is not None and choice.id == choice_id:
                revision = choice.input_revision
            if not choice_confirms_immediate(messages, choice_revision=revision):
                raise APIError(
                    409,
                    "immediate_termination",
                    "Immediate termination needs a later period-end choice",
                )
        return
    if latest_immediate_revision(messages) is not None:
        choice = current_choice(session, ticket_id)
        revision = None
        if choice is not None and choice_id is not None and choice.id == choice_id:
            revision = choice.input_revision
        if not choice_confirms_immediate(messages, choice_revision=revision):
            raise APIError(
                409,
                "immediate_termination",
                "Immediate termination needs a later period-end choice",
            )
        return
    if gate == "period_end":
        return
    raise APIError(409, "not_eligible", "This request is not a period-end cancellation")


def _action_was_admitted(session: Session, tenant_id: UUID, action_id: str) -> bool:
    from resolveai.db.models import ActionLedger, RecoveryJob

    found = session.scalar(
        select(RecoveryJob.id)
        .join(ActionLedger, ActionLedger.id == RecoveryJob.ledger_id)
        .where(
            ActionLedger.tenant_id == tenant_id,
            ActionLedger.action_id == action_id,
            RecoveryJob.admitted_at.is_not(None),
        )
        .limit(1)
    )
    return found is not None
