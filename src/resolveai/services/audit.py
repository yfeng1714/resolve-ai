from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import AuditEvent


def audit(
    session: Session,
    *,
    tenant_id: UUID,
    event_type: str,
    actor_user_id: UUID | None = None,
    ticket_id: UUID | None = None,
    run_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            ticket_id=ticket_id,
            run_id=run_id,
            event_type=event_type,
            payload=payload or {},
            created_at=utcnow(),
        )
    )
