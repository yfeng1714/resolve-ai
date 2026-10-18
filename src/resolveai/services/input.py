"""Customer and operator messages are the request. Internal notes are not."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import Message, Proposal, ReplyDraft, Run, Ticket

INPUT_AUTHORS = ("customer", "operator")
_WAITING_RUNS = (
    "analyzing",
    "gathering_evidence",
    "proposing",
    "awaiting_customer",
    "awaiting_approval",
)


def latest_input_revision(session: Session, ticket_id: UUID) -> int:
    value = session.scalar(
        select(func.max(Message.revision)).where(
            Message.ticket_id == ticket_id,
            Message.author_kind.in_(INPUT_AUTHORS),
        )
    )
    return int(value or 0)


def invalidate_pending_intent(session: Session, ticket: Ticket) -> None:
    """Supersede unexecuted intent after a new customer or operator message.

    Confirmed refunds stay. Internal notes and simulated replies do not call this.
    """
    current = latest_input_revision(session, ticket.id)
    proposals = session.scalars(
        select(Proposal).where(
            Proposal.ticket_id == ticket.id,
            Proposal.status.in_(("open", "approved")),
        )
    ).all()
    for proposal in proposals:
        bound = int((proposal.preconditions or {}).get("input_revision") or 0)
        if bound != current:
            proposal.status = "superseded"
    runs = session.scalars(
        select(Run).where(Run.ticket_id == ticket.id, Run.state.in_(_WAITING_RUNS))
    ).all()
    cancelled_ids = {run.id for run in runs}
    for run in runs:
        run.state = "cancelled"
        run.error_code = "input_changed"
        run.updated_at = utcnow()
    if not cancelled_ids:
        return
    drafts = session.scalars(
        select(ReplyDraft).where(
            ReplyDraft.ticket_id == ticket.id,
            ReplyDraft.run_id.in_(cancelled_ids),
            ReplyDraft.status == "draft",
        )
    ).all()
    for draft in drafts:
        draft.status = "superseded"
