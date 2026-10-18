"""Conditional refund writes.

The payment row lock and the one-open-refund-per-event index close the
read/check/write race. Replays of the same action and payload return the
original receipt and do not create a second refund.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import Account, Customer, Invoice, Operation, Payment, Refund
from resolveai.domain.hashing import canonical_hash, refund_action_id, refund_payload
from resolveai.domain.policy import (
    PaymentView,
    RefundView,
    evaluate_refund,
    load_refund_policy,
)
from resolveai.errors import APIError


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def account_view(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    account = session.get(Account, account_id)
    if account is None:
        raise APIError(404, "not_found", "Account was not found")
    customer = session.get(Customer, account.customer_id)
    if customer is None:
        raise APIError(404, "not_found", "Account was not found")
    return {
        "account_id": str(account.id),
        "customer_id": str(customer.id),
        "display_name": customer.display_name,
        "email": customer.email,
        "currency": account.currency,
        "status": account.status,
    }


def invoices_view(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    _require_account(session, account_id)
    rows = session.scalars(
        select(Invoice).where(Invoice.account_id == account_id).order_by(Invoice.number)
    ).all()
    return {
        "invoices": [
            {
                "id": str(row.id),
                "number": row.number,
                "amount_cents": row.amount_cents,
                "currency": row.currency,
                "status": row.status,
                "issued_at": _iso(row.issued_at),
            }
            for row in rows
        ]
    }


def payments_view(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    _require_account(session, account_id)
    rows = session.scalars(
        select(Payment)
        .where(Payment.account_id == account_id)
        .order_by(Payment.created_at, Payment.id)
    ).all()
    return {"payments": [_payment_dict(row) for row in rows]}


def refunds_view(session: Session, account_id: uuid.UUID) -> dict[str, Any]:
    _require_account(session, account_id)
    rows = session.scalars(
        select(Refund).where(Refund.account_id == account_id).order_by(Refund.created_at)
    ).all()
    return {
        "refunds": [
            {
                "id": str(row.id),
                "payment_id": str(row.payment_id),
                "event_id": row.event_id,
                "amount_cents": row.amount_cents,
                "currency": row.currency,
                "status": row.status,
                "action_id": row.action_id,
                "operation_id": row.operation_id,
            }
            for row in rows
        ]
    }


def operation_view(session: Session, account_id: uuid.UUID, action_id: str) -> dict[str, Any]:
    operation = session.get(Operation, action_id)
    if operation is None or operation.account_id != account_id:
        raise APIError(404, "not_found", "Operation was not found")
    return {
        "action_id": operation.action_id,
        "operation_id": str(operation.operation_id),
        "status": operation.status,
        "payload_hash": operation.payload_hash,
        "result": operation.result,
    }


def apply_refund(session: Session, command: Any) -> tuple[bool, dict[str, Any]]:
    """Insert a refund or return the original receipt.

    Returns `(created, result)`. `created` is true only for the transaction
    that first commits the business row.
    """
    if command.action_id != refund_action_id(command.payment_id):
        raise APIError(422, "validation", "Action identity does not match the payment")
    policy = load_refund_policy()
    if command.policy_version != policy.version:
        raise APIError(422, "validation", "Policy version is not current")
    expected_hash = canonical_hash(
        refund_payload(
            action_id=command.action_id,
            account_id=command.account_id,
            payment_id=command.payment_id,
            event_id=command.event_id,
            amount_cents=command.amount_cents,
            policy_version=command.policy_version,
        )
    )
    if expected_hash != command.payload_hash:
        raise APIError(422, "hash_mismatch", "Payload hash does not match the refund body")

    payments = session.scalars(
        select(Payment)
        .where(Payment.account_id == command.account_id)
        .order_by(Payment.id)
        .with_for_update()
    ).all()
    if not payments:
        raise APIError(404, "not_found", "Account was not found")
    payment = next((row for row in payments if row.id == command.payment_id), None)
    if payment is None or payment.event_id != command.event_id:
        raise APIError(404, "not_found", "Payment was not found")

    operation = session.scalar(
        select(Operation).where(Operation.action_id == command.action_id).with_for_update()
    )
    if operation is not None:
        same_account = operation.account_id == command.account_id
        same_payload = operation.payload_hash == command.payload_hash
        if not same_account or not same_payload:
            raise APIError(
                409,
                "payload_conflict",
                "Action identity was used with a different payload",
            )
        operation.replay_count += 1
        replayed = dict(operation.result)
        replayed["replayed"] = True
        return False, replayed

    refund_rows = session.scalars(
        select(Refund).where(Refund.account_id == command.account_id)
    ).all()
    decision = evaluate_refund(
        [_payment_view(row) for row in payments],
        [_refund_view(row) for row in refund_rows],
        utcnow(),
    )
    if (
        decision.outcome != "eligible"
        or decision.payment_id != command.payment_id
        or decision.amount_cents != command.amount_cents
        or decision.event_id != command.event_id
    ):
        raise APIError(422, "not_eligible", "The payment is not eligible for this refund")
    if payment.resource_version != command.expected_payment_version:
        raise APIError(409, "stale", "Payment version changed")

    payment.resource_version += 1
    operation_id = uuid.uuid4()
    refund_id = uuid.uuid4()
    result = {
        "operation_status": "confirmed",
        "operation_id": str(operation_id),
        "refund_id": str(refund_id),
        "action_id": command.action_id,
        "amount_cents": command.amount_cents,
        "currency": "USD",
        "payment_id": str(command.payment_id),
        "event_id": command.event_id,
        "resource_version": payment.resource_version,
        "replayed": False,
    }
    session.add(
        Operation(
            action_id=command.action_id,
            operation_id=operation_id,
            payload_hash=command.payload_hash,
            account_id=command.account_id,
            status="confirmed",
            result=result,
            write_attempts=1,
            replay_count=0,
            created_at=utcnow(),
        )
    )
    session.add(
        Refund(
            id=refund_id,
            account_id=command.account_id,
            payment_id=command.payment_id,
            event_id=command.event_id,
            amount_cents=command.amount_cents,
            currency="USD",
            status="succeeded",
            action_id=command.action_id,
            operation_id=str(operation_id),
            created_at=utcnow(),
        )
    )
    session.flush()
    return True, result


def _require_account(session: Session, account_id: uuid.UUID) -> None:
    if session.get(Account, account_id) is None:
        raise APIError(404, "not_found", "Account was not found")


def _payment_dict(row: Payment) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "invoice_id": None if row.invoice_id is None else str(row.invoice_id),
        "event_id": row.event_id,
        "amount_cents": row.amount_cents,
        "currency": row.currency,
        "status": row.status,
        "settled_at": _iso(row.settled_at),
        "resource_version": row.resource_version,
    }


def _payment_view(row: Payment) -> PaymentView:
    return PaymentView(
        id=row.id,
        event_id=row.event_id,
        amount_cents=row.amount_cents,
        currency=row.currency,
        status=row.status,
        settled_at=row.settled_at,
        resource_version=row.resource_version,
        invoice_id=row.invoice_id,
    )


def _refund_view(row: Refund) -> RefundView:
    return RefundView(
        payment_id=row.payment_id,
        event_id=row.event_id,
        amount_cents=row.amount_cents,
        status=row.status,
    )
