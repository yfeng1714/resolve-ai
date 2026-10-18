"""Reply text is rendered from verified records, not from model prose."""

from uuid import UUID

from resolveai.domain.money import format_usd
from resolveai.locale import phrase


def confirmed_reply(
    *,
    customer_name: str,
    amount_cents: int,
    operation_id: str,
    payment_id: UUID | str,
    event_id: str,
    invoice_number: str | None,
) -> str:
    invoice = phrase("invoice_clause", invoice_number=invoice_number) if invoice_number else ""
    return phrase(
        "reply_confirmed",
        customer_name=customer_name,
        amount=format_usd(amount_cents),
        invoice=invoice,
        operation_id=operation_id,
        payment_id=payment_id,
        event_id=event_id,
    )


def unknown_reply(*, customer_name: str, amount_cents: int, action_id: str) -> str:
    return phrase(
        "reply_unknown",
        customer_name=customer_name,
        amount=format_usd(amount_cents),
        action_id=action_id,
    )


def rejected_reply(*, customer_name: str) -> str:
    return phrase("reply_rejected", customer_name=customer_name)
