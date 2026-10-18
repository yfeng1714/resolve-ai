from datetime import UTC, datetime, timedelta
from uuid import UUID

from resolveai.domain.hashing import canonical_hash, refund_payload
from resolveai.domain.money import format_usd
from resolveai.domain.passwords import hash_password, verify_password
from resolveai.domain.policy import PaymentView, RefundView, evaluate_refund, load_refund_policy
from resolveai.domain.replies import confirmed_reply, unknown_reply

NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


def _payment(
    suffix: str,
    *,
    event: str,
    amount: int,
    status: str = "settled",
    settled_at: datetime | None = None,
    currency: str = "USD",
    invoice_id: UUID | None = None,
) -> PaymentView:
    return PaymentView(
        id=UUID(f"66666666-6666-4666-8666-6666666666{suffix}"),
        event_id=event,
        amount_cents=amount,
        currency=currency,
        status=status,
        settled_at=(
            NOW - timedelta(days=1)
            if settled_at is None and status == "settled"
            else settled_at
        ),
        resource_version=1,
        invoice_id=invoice_id,
    )


def test_policy_file_is_the_rule_source() -> None:
    policy = load_refund_policy()
    assert policy.version == "refund-policy-2026.10.12"
    assert policy.currency == "USD"
    assert policy.max_amount_cents == 10000
    assert policy.window_days == 7


def test_duplicate_within_cap_and_window_is_eligible() -> None:
    decision = evaluate_refund(
        [
            _payment("11", event="evt", amount=4200, settled_at=NOW - timedelta(days=1)),
            _payment("12", event="evt", amount=4200, settled_at=NOW - timedelta(minutes=1)),
        ],
        [],
        NOW,
    )
    assert decision.outcome == "eligible"
    assert decision.amount_cents == 4200
    assert str(decision.payment_id).endswith("12")


def test_window_and_cap_boundaries() -> None:
    policy = load_refund_policy()
    inside = evaluate_refund(
        [
            _payment("11", event="evt", amount=1000, settled_at=NOW - timedelta(days=8)),
            _payment(
                "12",
                event="evt",
                amount=policy.max_amount_cents,
                settled_at=NOW - timedelta(days=policy.window_days),
            ),
        ],
        [],
        NOW,
    )
    assert inside.outcome == "eligible"
    outside = evaluate_refund(
        [
            _payment("21", event="evt", amount=1000, settled_at=NOW - timedelta(days=8)),
            _payment(
                "22",
                event="evt",
                amount=policy.max_amount_cents,
                settled_at=NOW - timedelta(days=policy.window_days, seconds=1),
            ),
        ],
        [],
        NOW,
    )
    assert outside.outcome == "handoff"
    assert "outside_refund_window" in outside.reasons
    over = evaluate_refund(
        [
            _payment("31", event="evt", amount=1000),
            _payment("32", event="evt", amount=policy.max_amount_cents + 1),
        ],
        [],
        NOW,
    )
    assert over.outcome == "handoff"
    assert "amount_exceeds_policy_cap" in over.reasons


def test_other_billing_shapes_do_not_propose() -> None:
    pending = evaluate_refund(
        [
            _payment("41", event="evt", amount=5000),
            _payment("42", event="evt", amount=5000, status="pending", settled_at=None),
        ],
        [],
        NOW,
    )
    assert pending.outcome == "clarify_pending"
    distinct = evaluate_refund(
        [
            _payment("51", event="one", amount=3000),
            _payment("52", event="two", amount=1800),
        ],
        [],
        NOW,
    )
    assert distinct.outcome == "handoff"
    blocked = evaluate_refund(
        [
            _payment("61", event="evt", amount=1500),
            _payment("62", event="evt", amount=1500),
        ],
        [
            RefundView(
                payment_id=UUID("66666666-6666-4666-8666-666666666662"),
                event_id="evt",
                amount_cents=1500,
                status="succeeded",
            )
        ],
        NOW,
    )
    assert blocked.outcome == "handoff"
    ambiguous = evaluate_refund(
        [
            _payment("71", event="evt", amount=1000, settled_at=NOW - timedelta(days=3)),
            _payment("72", event="evt", amount=1000, settled_at=NOW - timedelta(days=2)),
            _payment("73", event="evt", amount=1000, settled_at=NOW - timedelta(days=1)),
        ],
        [],
        NOW,
    )
    assert ambiguous.outcome == "handoff"
    assert "ambiguous_duplicates" in ambiguous.reasons


def test_hash_and_reply_are_stable() -> None:
    payload = refund_payload(
        action_id="refund:pay",
        account_id="44444444-4444-4444-8444-444444444401",
        payment_id="66666666-6666-4666-8666-666666666612",
        event_id="evt",
        amount_cents=4200,
        policy_version="refund-policy-2026.10.12",
    )
    assert canonical_hash(payload) == canonical_hash(dict(reversed(list(payload.items()))))
    assert format_usd(4200) == "$42.00"
    confirmed = confirmed_reply(
        customer_name="Avery Chen",
        amount_cents=4200,
        operation_id="op-1",
        payment_id="pay",
        event_id="evt",
        invoice_number="INV-1001",
    )
    assert "We confirmed a refund of $42.00 USD" in confirmed
    assert "op-1" in confirmed
    unknown = unknown_reply(customer_name="Avery Chen", amount_cents=4200, action_id="refund:pay")
    assert "not confirmed" in unknown
    assert "We confirmed a refund" not in unknown


def test_password_hash_is_not_plaintext() -> None:
    stored = hash_password("support-demo-pass")
    assert "support-demo-pass" not in stored
    assert verify_password("support-demo-pass", stored)
    assert not verify_password("approver-demo-pass", stored)


def test_conflicting_invoices_are_not_treated_as_one_duplicate() -> None:
    left = UUID("77777777-7777-4777-8777-777777777731")
    right = UUID("77777777-7777-4777-8777-777777777732")
    shared = UUID("77777777-7777-4777-8777-777777777733")
    conflict = evaluate_refund(
        [
            _payment("81", event="evt", amount=1800, invoice_id=left),
            _payment("82", event="evt", amount=1800, invoice_id=right),
        ],
        [],
        NOW,
    )
    assert conflict.outcome == "handoff"
    assert "conflicting_invoice_evidence" in conflict.reasons
    same_invoice = evaluate_refund(
        [
            _payment("83", event="evt", amount=1800, invoice_id=shared),
            _payment("84", event="evt", amount=1800, invoice_id=shared),
        ],
        [],
        NOW,
    )
    assert same_invoice.outcome == "eligible"
