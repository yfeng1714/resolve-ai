"""Tenant account binding. The simulator itself stays account-scoped."""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from resolveai.db.models import Ticket
from resolveai.errors import APIError


def account_is_bound(
    session: Session, tenant_id: UUID, account_id: UUID, customer_id: UUID
) -> bool:
    found = session.execute(
        text(
            """
            SELECT 1
            FROM app.tenant_account
            WHERE tenant_id = :tenant_id
              AND account_id = :account_id
              AND customer_id = :customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "account_id": account_id,
            "customer_id": customer_id,
        },
    ).one_or_none()
    return found is not None


def require_bound_account(session: Session, ticket: Ticket) -> None:
    """Refuse a simulator call unless this ticket's account binding is visible."""
    if not account_is_bound(session, ticket.tenant_id, ticket.account_id, ticket.customer_id):
        raise APIError(409, "account_not_bound", "The ticket account is not bound to this tenant")
