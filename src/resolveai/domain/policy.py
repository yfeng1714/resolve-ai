"""Versioned duplicate-payment refund rule.

Eligibility is calculated here. Model text, ticket prose, and tool arguments
cannot change the amount, the target payment, or the approval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Literal
from uuid import UUID

from resolveai.settings import repo_root

Outcome = Literal["eligible", "clarify_pending", "handoff"]


@dataclass(frozen=True)
class RefundPolicy:
    version: str
    currency: str
    max_amount_cents: int
    window_days: int


@dataclass(frozen=True)
class PaymentView:
    id: UUID
    event_id: str
    amount_cents: int
    currency: str
    status: str
    settled_at: datetime | None
    resource_version: int
    invoice_id: UUID | None = None


@dataclass(frozen=True)
class RefundView:
    payment_id: UUID
    event_id: str
    amount_cents: int
    status: str


@dataclass(frozen=True)
class PolicyDecision:
    outcome: Outcome
    payment_id: UUID | None
    event_id: str | None
    amount_cents: int | None
    payment_version: int | None
    reasons: list[str]
    policy_version: str


@lru_cache(maxsize=1)
def load_refund_policy() -> RefundPolicy:
    path = repo_root() / "policies" / "refund_policy_v1.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    return RefundPolicy(
        version=raw["policy_version"],
        currency=raw["currency"],
        max_amount_cents=int(raw["max_amount_cents"]),
        window_days=int(raw["window_days"]),
    )


def evaluate_refund(
    payments: list[PaymentView],
    refunds: list[RefundView],
    now: datetime,
) -> PolicyDecision:
    policy = load_refund_policy()
    blocking = [refund for refund in refunds if refund.status in {"pending", "succeeded"}]
    blocked_events = {refund.event_id for refund in blocking}
    grouped: dict[str, list[PaymentView]] = {}
    for payment in payments:
        grouped.setdefault(payment.event_id, []).append(payment)

    reasons: list[str] = []
    eligible: list[PaymentView] = []
    for event_id, group in grouped.items():
        settled = sorted(
            (
                payment
                for payment in group
                if payment.status == "settled" and payment.settled_at is not None
            ),
            key=lambda payment: (payment.settled_at, str(payment.id)),
        )
        if any(payment.status == "pending" for payment in group) and settled:
            reasons.append("pending_payment_present")
        if len(settled) > 2:
            reasons.append("ambiguous_duplicates")
            continue
        if len(settled) != 2:
            continue
        duplicate = settled[1]
        if duplicate.currency != policy.currency or settled[0].currency != policy.currency:
            reasons.append("non_usd")
            continue
        if event_id in blocked_events:
            reasons.append("refund_already_open_or_settled")
            continue
        used = sum(
            refund.amount_cents
            for refund in blocking
            if refund.payment_id == duplicate.id
        )
        if used > 0:
            reasons.append("refund_already_open_or_settled")
            continue
        if duplicate.amount_cents > policy.max_amount_cents:
            reasons.append("amount_exceeds_policy_cap")
            continue
        if duplicate.amount_cents <= 0:
            reasons.append("invalid_amount")
            continue
        if duplicate.settled_at is None or duplicate.settled_at < now - timedelta(
            days=policy.window_days
        ):
            reasons.append("outside_refund_window")
            continue
        if settled[0].invoice_id != duplicate.invoice_id:
            reasons.append("conflicting_invoice_evidence")
            continue
        eligible.append(duplicate)

    if len(eligible) == 1:
        payment = eligible[0]
        return PolicyDecision(
            outcome="eligible",
            payment_id=payment.id,
            event_id=payment.event_id,
            amount_cents=payment.amount_cents,
            payment_version=payment.resource_version,
            reasons=["eligible_duplicate"],
            policy_version=policy.version,
        )
    if len(eligible) > 1:
        reasons.append("ambiguous_duplicates")
    if not reasons:
        reasons.append("no_duplicate_settled_payment")
    unique_reasons = list(dict.fromkeys(reasons))
    pending_only = unique_reasons == ["pending_payment_present"]
    return PolicyDecision(
        outcome="clarify_pending" if pending_only else "handoff",
        payment_id=None,
        event_id=None,
        amount_cents=None,
        payment_version=None,
        reasons=unique_reasons,
        policy_version=policy.version,
    )
