from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from resolveai.api.security import Actor
from resolveai.db.models import (
    ActionLedger,
    Approval,
    Message,
    Proposal,
    ReplyDraft,
    Run,
    Ticket,
)
from resolveai.errors import APIError


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def ticket_detail(session: Session, actor: Actor, ticket_id: UUID) -> dict[str, Any]:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != actor.tenant_id:
        raise APIError(404, "not_found", "Ticket was not found")
    messages = session.scalars(
        select(Message).where(Message.ticket_id == ticket.id).order_by(Message.revision)
    ).all()
    runs = session.scalars(
        select(Run)
        .where(Run.ticket_id == ticket.id, Run.tenant_id == actor.tenant_id)
        .order_by(Run.created_at)
    ).all()
    proposals = session.scalars(
        select(Proposal)
        .where(Proposal.ticket_id == ticket.id, Proposal.tenant_id == actor.tenant_id)
        .order_by(Proposal.created_at, Proposal.revision)
    ).all()
    proposal_ids = [row.id for row in proposals]
    approvals = []
    if proposal_ids:
        approvals = session.scalars(
            select(Approval).where(Approval.proposal_id.in_(proposal_ids))
        ).all()
    approval_by_proposal = {row.proposal_id: row for row in approvals}
    replies = session.scalars(
        select(ReplyDraft)
        .where(ReplyDraft.ticket_id == ticket.id, ReplyDraft.tenant_id == actor.tenant_id)
        .order_by(ReplyDraft.revision)
    ).all()
    filters = []
    if proposals:
        filters.append(ActionLedger.proposal_id.in_([row.id for row in proposals]))
    if replies:
        filters.append(ActionLedger.reply_draft_id.in_([row.id for row in replies]))
    actions = []
    if filters:
        actions = session.scalars(
            select(ActionLedger).where(ActionLedger.tenant_id == actor.tenant_id, or_(*filters))
        ).all()
    return {
        "viewer": {
            "role": actor.role,
            "display_name": actor.display_name,
            "email": actor.email,
        },
        "ticket": {
            "id": str(ticket.id),
            "subject": ticket.subject,
            "status": ticket.status,
            "customer_label": ticket.customer_label,
            "customer_email": ticket.customer_email,
            "created_at": _iso(ticket.created_at),
            "updated_at": _iso(ticket.updated_at),
        },
        "messages": [
            {
                "id": str(row.id),
                "revision": row.revision,
                "author_kind": row.author_kind,
                "body": row.body,
                "created_at": _iso(row.created_at),
            }
            for row in messages
        ],
        "runs": [
            {
                "id": str(row.id),
                "state": row.state,
                "message_revision": row.message_revision,
                "provider": row.provider,
                "prompt_version": row.prompt_version,
                "model_calls": row.model_calls,
                "tool_calls": row.tool_calls,
                "replans": row.replans,
                "read_retries": row.read_retries,
                "budget_usd_micros": row.budget_usd_micros,
                "error_code": row.error_code,
                "observations": row.observations,
                "concise_reasons": row.concise_reasons,
                "created_at": _iso(row.created_at),
                "updated_at": _iso(row.updated_at),
            }
            for row in runs
        ],
        "proposals": [
            {
                "id": str(row.id),
                "run_id": str(row.run_id),
                "revision": row.revision,
                "status": row.status,
                "action_id": row.action_id,
                "action_type": row.action_type,
                "amount_cents": row.amount_cents,
                "currency": row.currency,
                "payload_hash": row.payload_hash,
                "policy_version": row.policy_version,
                "target_payment_id": str(row.target_payment_id),
                "target_event_id": row.target_event_id,
                "preconditions": row.preconditions,
                "evidence": row.evidence,
                "created_at": _iso(row.created_at),
                "approval": None
                if approval_by_proposal.get(row.id) is None
                else {
                    "decision": approval_by_proposal[row.id].decision,
                    "revision": approval_by_proposal[row.id].revision,
                    "payload_hash": approval_by_proposal[row.id].payload_hash,
                },
            }
            for row in proposals
        ],
        "actions": [
            {
                "action_id": row.action_id,
                "action_type": row.action_type,
                "status": row.status,
                "dispatch_state": row.dispatch_state,
                "operation_id": row.operation_id,
                "error_code": row.error_code,
                "payload_hash": row.payload_hash,
                "result": row.result,
                "updated_at": _iso(row.updated_at),
            }
            for row in actions
        ],
        "replies": [
            {
                "id": str(row.id),
                "revision": row.revision,
                "status": row.status,
                "recipient": row.recipient,
                "body": row.body,
                "body_hash": row.body_hash,
                "send_action_id": row.send_action_id,
                "receipt": row.receipt,
                "created_at": _iso(row.created_at),
            }
            for row in replies
        ],
    }


def ticket_summary(row: Ticket) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "subject": row.subject,
        "status": row.status,
        "customer_label": row.customer_label,
        "updated_at": _iso(row.updated_at),
    }
