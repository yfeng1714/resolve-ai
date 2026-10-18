"""Parse simulator read payloads into policy inputs."""

from datetime import datetime
from uuid import UUID

from resolveai.domain.policy import PaymentView, RefundView


def parse_payments(data: dict) -> list[PaymentView]:
    views: list[PaymentView] = []
    for row in data.get("payments", []):
        settled = row.get("settled_at")
        invoice_id = row.get("invoice_id")
        views.append(
            PaymentView(
                id=UUID(row["id"]),
                event_id=row["event_id"],
                amount_cents=int(row["amount_cents"]),
                currency=row["currency"],
                status=row["status"],
                settled_at=datetime.fromisoformat(settled) if settled else None,
                resource_version=int(row["resource_version"]),
                invoice_id=UUID(invoice_id) if invoice_id else None,
            )
        )
    return views


def parse_refunds(data: dict) -> list[RefundView]:
    return [
        RefundView(
            payment_id=UUID(row["payment_id"]),
            event_id=row["event_id"],
            amount_cents=int(row["amount_cents"]),
            status=row["status"],
        )
        for row in data.get("refunds", [])
    ]
