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
    RecoveryJob,
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
    session.flush()
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
    approvals: list[Approval] = []
    if proposal_ids:
        approvals = list(
            session.scalars(
                select(Approval)
                .where(Approval.proposal_id.in_(proposal_ids))
                .order_by(Approval.created_at, Approval.id)
            ).all()
        )
    approval_by_proposal: dict[UUID, Approval] = {}
    history_by_proposal: dict[UUID, list[Approval]] = {}
    for row in approvals:
        approval_by_proposal[row.proposal_id] = row
        history_by_proposal.setdefault(row.proposal_id, []).append(row)
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
                "target_payment_id": None
                if row.target_payment_id is None
                else str(row.target_payment_id),
                "target_event_id": row.target_event_id,
                "target_subscription_id": None
                if row.target_subscription_id is None
                else str(row.target_subscription_id),
                "effective_at": None if row.effective_at is None else _iso(row.effective_at),
                "preconditions": row.preconditions,
                "evidence": row.evidence,
                "created_at": _iso(row.created_at),
                "approval": _approval_view(session, row, approval_by_proposal.get(row.id)),
                "grant_history": [
                    view
                    for item in history_by_proposal.get(row.id, [])
                    if (view := _approval_view(session, row, item)) is not None
                ],
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
        "recovery_jobs": _recovery_jobs(session, actor, runs),
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
        "incident": _incident_view(session, ticket),
        "combined": _combined_view(session, ticket),
    }


def _recovery_jobs(session: Session, actor: Actor, runs: list[Run]) -> list[dict[str, Any]]:
    if not runs:
        return []
    rows = session.scalars(
        select(RecoveryJob)
        .where(
            RecoveryJob.tenant_id == actor.tenant_id,
            RecoveryJob.run_id.in_([row.id for row in runs]),
        )
        .order_by(RecoveryJob.created_at)
    ).all()
    return [
        {
            "id": str(row.id),
            "state": row.state,
            "block_reason": row.block_reason,
            "admitted": row.admitted_at is not None,
            "command_generation": row.command_generation,
            "proposal_id": str(row.proposal_id),
        }
        for row in rows
    ]


def _approval_view(
    session: Session, proposal: Proposal, approval: Approval | None
) -> dict[str, Any] | None:
    if approval is None:
        return None
    from resolveai.services.grants import grant_label, revocation_for

    revoked = revocation_for(session, approval.id)
    return {
        "id": str(approval.id),
        "decision": approval.decision,
        "revision": approval.revision,
        "payload_hash": approval.payload_hash,
        "label": grant_label(session, approval, proposal),
        "expires_at": _iso(approval.expires_at),
        "reason": None if revoked is None else revoked.reason,
    }


def ticket_summary(row: Ticket) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "subject": row.subject,
        "status": row.status,
        "customer_label": row.customer_label,
        "updated_at": _iso(row.updated_at),
    }


def _combined_view(session: Session, ticket: Ticket) -> dict[str, Any]:
    from resolveai.services.combined import current_halves, ticket_is_combined

    active = ticket_is_combined(session, ticket)
    halves = current_halves(session, ticket.id) if active else {}
    return {
        "active": active,
        "halves": [
            {
                "half": half,
                "outcome_code": row.outcome_code,
                "reason_code": row.reason_code,
                "action_id": row.action_id,
                "outcome_id": str(row.id),
            }
            for half, row in halves.items()
        ],
    }


def _incident_view(session: Session, ticket: Ticket) -> dict[str, Any]:
    from resolveai.db.models import IncidentHandoff
    from resolveai.domain.cancellation import canonical_instant
    from resolveai.domain.incident import incident_requested
    from resolveai.services.incident import current_incident_run, current_report

    messages = session.scalars(
        select(Message)
        .where(
            Message.ticket_id == ticket.id,
            Message.author_kind.in_(("customer", "operator")),
        )
        .order_by(Message.revision)
    ).all()
    active = incident_requested([{"body": row.body} for row in messages])
    report = current_report(session, ticket.id)
    current_run = current_incident_run(session, ticket)
    rows = session.scalars(
        select(IncidentHandoff)
        .where(IncidentHandoff.ticket_id == ticket.id)
        .order_by(IncidentHandoff.created_at, IncidentHandoff.id)
    ).all()
    current = None
    if current_run is not None:
        current = next((row for row in rows if row.run_id == current_run.id), None)
    return {
        "active": active,
        "report": None
        if report is None
        else {
            "id": str(report.id),
            "service_code": report.service_code,
            "region_code": report.region_code,
            "reported_started_at": canonical_instant(report.reported_started_at),
            "request_id": report.request_id,
            "input_revision": report.input_revision,
        },
        "handoff": None if current is None else _handoff_view(current),
        "history": [_handoff_view(row) for row in rows if current is None or row.id != current.id],
    }


def _handoff_view(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "kind": row.kind,
        "reason_code": row.reason_code,
        "incident_id": None if row.incident_id is None else str(row.incident_id),
        "input_revision": row.input_revision,
        "snapshot_digest": row.snapshot_digest,
    }
