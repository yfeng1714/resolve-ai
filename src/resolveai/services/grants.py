"""Refund approval expiry and revocation.

Decisions are inserted once. The runtime role cannot update or delete them.
A recovery job stays bound to the approval id stored at enqueue.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import (
    ActionLedger,
    Approval,
    ApprovalRevocation,
    Proposal,
    RecoveryJob,
    Run,
    Ticket,
)
from resolveai.errors import APIError
from resolveai.services.audit import audit
from resolveai.services.input import latest_input_revision

PRE_ADMISSION = ("queued", "leased", "blocked")


def grant_deadline(session: Session, ttl_seconds: int):
    return session.scalar(
        text("SELECT clock_timestamp() + make_interval(secs => :ttl)"),
        {"ttl": ttl_seconds},
    )


def latest_approval(session: Session, tenant_id: UUID, proposal_id: UUID) -> Approval | None:
    return session.scalar(
        select(Approval)
        .where(Approval.tenant_id == tenant_id, Approval.proposal_id == proposal_id)
        .order_by(Approval.created_at.desc(), Approval.id.desc())
        .limit(1)
    )


def approval_history(session: Session, tenant_id: UUID, proposal_id: UUID) -> list[Approval]:
    return list(
        session.scalars(
            select(Approval)
            .where(Approval.tenant_id == tenant_id, Approval.proposal_id == proposal_id)
            .order_by(Approval.created_at, Approval.id)
        ).all()
    )


def revocation_for(session: Session, approval_id: UUID) -> ApprovalRevocation | None:
    return session.scalar(
        select(ApprovalRevocation).where(ApprovalRevocation.approval_id == approval_id)
    )


def grant_is_live(session: Session, approval: Approval, proposal: Proposal) -> bool:
    if approval.decision != "approved":
        return False
    if approval.revision != proposal.revision or approval.payload_hash != proposal.payload_hash:
        return False
    live = session.scalar(
        text(
            """
            SELECT approval_grant.expires_at > clock_timestamp()
               AND NOT EXISTS (
                   SELECT 1 FROM app.approval_revocation AS revocation
                   WHERE revocation.approval_id = approval_grant.id
               )
            FROM app.approval AS approval_grant
            WHERE approval_grant.id = :id
            """
        ),
        {"id": approval.id},
    )
    return bool(live)


def current_grant(session: Session, proposal: Proposal) -> Approval | None:
    rows = session.scalars(
        select(Approval)
        .where(
            Approval.tenant_id == proposal.tenant_id,
            Approval.proposal_id == proposal.id,
            Approval.decision == "approved",
            Approval.revision == proposal.revision,
            Approval.payload_hash == proposal.payload_hash,
        )
        .order_by(Approval.created_at.desc(), Approval.id.desc())
    ).all()
    for row in rows:
        if grant_is_live(session, row, proposal):
            return row
    return None


def grant_label(session: Session, approval: Approval, proposal: Proposal) -> str:
    if approval.decision == "rejected":
        return "rejected"
    if revocation_for(session, approval.id) is not None:
        return "revoked"
    if grant_is_live(session, approval, proposal):
        return "valid"
    return "expired"


def invalid_grant_code(session: Session, proposal: Proposal) -> str:
    latest = latest_approval(session, proposal.tenant_id, proposal.id)
    if latest is not None and revocation_for(session, latest.id) is not None:
        return "approval_revoked"
    if latest is not None and latest.decision == "approved":
        return "approval_expired"
    return "not_approved"


def release_invalid_grant(
    session: Session,
    *,
    tenant_id: UUID,
    approval_id: UUID,
    commit: bool,
) -> None:
    """Cancel unsent work for one invalid grant. A valid replacement is left alone."""
    preview = session.get(Approval, approval_id)
    if preview is None or preview.tenant_id != tenant_id:
        return
    proposal_preview = session.get(Proposal, preview.proposal_id)
    if proposal_preview is None or proposal_preview.tenant_id != tenant_id:
        return
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == proposal_preview.ticket_id, Ticket.tenant_id == tenant_id)
        .with_for_update()
    )
    proposal = session.scalar(
        select(Proposal)
        .where(Proposal.id == preview.proposal_id, Proposal.tenant_id == tenant_id)
        .with_for_update()
    )
    approval = session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == tenant_id)
        .with_for_update()
    )
    run = (
        None
        if proposal is None
        else session.scalar(
            select(Run)
            .where(Run.id == proposal.run_id, Run.tenant_id == tenant_id)
            .with_for_update()
        )
    )
    if ticket is None or proposal is None or approval is None or run is None:
        return
    session.refresh(ticket)
    session.refresh(proposal)
    session.refresh(approval)
    session.refresh(run)
    if grant_is_live(session, approval, proposal):
        return
    reason = (
        "approval_revoked"
        if revocation_for(session, approval.id) is not None
        else "approval_expired"
    )
    ledger_ids = list(
        session.scalars(
            select(RecoveryJob.ledger_id)
            .where(
                RecoveryJob.tenant_id == tenant_id,
                RecoveryJob.approval_id == approval.id,
                RecoveryJob.admitted_at.is_(None),
                RecoveryJob.state.in_(PRE_ADMISSION),
            )
            .order_by(RecoveryJob.ledger_id)
        ).all()
    )
    for ledger_id in ledger_ids:
        session.scalar(
            select(ActionLedger)
            .where(ActionLedger.id == ledger_id, ActionLedger.tenant_id == tenant_id)
            .with_for_update()
        )
    jobs = list(
        session.scalars(
            select(RecoveryJob)
            .where(
                RecoveryJob.tenant_id == tenant_id,
                RecoveryJob.approval_id == approval.id,
                RecoveryJob.admitted_at.is_(None),
                RecoveryJob.state.in_(PRE_ADMISSION),
            )
            .order_by(RecoveryJob.id)
            .with_for_update()
        ).all()
    )
    now = utcnow()
    for job in jobs:
        session.refresh(job)
        if job.admitted_at is not None or job.approval_id != approval.id:
            continue
        if job.state not in PRE_ADMISSION:
            continue
        job.state = "cancelled"
        job.block_reason = reason
        job.owner_token = None
        job.updated_at = now
    from resolveai.services.combined import (
        _remember_effect,
        authoritative_effect,
        record_financial_state,
        ticket_is_combined,
    )

    if ticket_is_combined(session, ticket):
        effect = authoritative_effect(session, ticket, proposal.action_type)
        if effect is not None and effect.action_id == proposal.action_id:
            half = "refund" if proposal.action_type == "refund" else "cancel_at_period_end"
            if _remember_effect(session, ticket, run, half, effect):
                from resolveai.services.combined import reduce_combined_reply

                reduce_combined_reply(session, ticket, run)
        else:
            record_financial_state(
                session, ticket, run, proposal, None, code="not_executed", reason=reason
            )
    replacement = current_grant(session, proposal)
    if replacement is None or replacement.id == approval.id:
        if _can_reopen(session, proposal, approval, ticket):
            proposal.status = "open"
    session.flush()
    if commit:
        session.commit()


def action_is_admitted(session: Session, proposal: Proposal) -> bool:
    """True when this stable action already has an admitted command."""
    found = session.scalar(
        select(RecoveryJob.id)
        .join(ActionLedger, ActionLedger.id == RecoveryJob.ledger_id)
        .where(
            ActionLedger.tenant_id == proposal.tenant_id,
            ActionLedger.action_id == proposal.action_id,
            RecoveryJob.tenant_id == proposal.tenant_id,
            RecoveryJob.admitted_at.is_not(None),
        )
        .limit(1)
    )
    return found is not None


def _can_reopen(session: Session, proposal: Proposal, approval: Approval, ticket: Ticket) -> bool:
    if proposal.status != "approved":
        return False
    if approval.revision != proposal.revision or approval.payload_hash != proposal.payload_hash:
        return False
    if action_is_admitted(session, proposal):
        return False
    latest_id = session.scalar(
        select(Proposal.id)
        .where(Proposal.ticket_id == ticket.id, Proposal.tenant_id == proposal.tenant_id)
        .order_by(Proposal.created_at.desc(), Proposal.id.desc())
        .limit(1)
    )
    if latest_id != proposal.id:
        return False
    bound = proposal.preconditions.get("input_revision")
    if bound is None or int(bound) != latest_input_revision(session, ticket.id):
        return False
    return True


def revoke_approval(
    session: Session,
    *,
    actor: Any,
    approval_id: UUID,
    reason: str,
) -> ApprovalRevocation:
    if actor.role != "approver":
        raise APIError(403, "denied", "Only an approver can revoke a refund grant")
    cleaned = reason.strip()
    if not cleaned or len(cleaned) > 500:
        raise APIError(422, "validation", "A revocation reason is required")
    preview = session.get(Approval, approval_id)
    if preview is None or preview.tenant_id != actor.tenant_id:
        raise APIError(404, "not_found", "Approval was not found")
    if preview.decision != "approved":
        raise APIError(409, "stale_proposal", "Only an approval can be revoked")
    proposal_preview = session.get(Proposal, preview.proposal_id)
    if proposal_preview is None:
        raise APIError(404, "not_found", "Approval was not found")
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == proposal_preview.ticket_id, Ticket.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    proposal = session.scalar(
        select(Proposal)
        .where(Proposal.id == preview.proposal_id, Proposal.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    approval = session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if ticket is None or proposal is None or approval is None:
        raise APIError(404, "not_found", "Approval was not found")
    session.refresh(approval)
    existing = revocation_for(session, approval.id)
    if existing is not None:
        return existing
    if approval.decision != "approved":
        raise APIError(409, "stale_proposal", "Only an approval can be revoked")
    created = ApprovalRevocation(
        tenant_id=actor.tenant_id,
        approval_id=approval.id,
        actor_user_id=actor.user_id,
        reason=cleaned,
        created_at=utcnow(),
    )
    session.add(created)
    session.flush()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="approval_revoked",
        actor_user_id=actor.user_id,
        ticket_id=proposal.ticket_id,
        run_id=proposal.run_id,
        payload={"approval_id": str(approval.id), "proposal_id": str(proposal.id)},
    )
    release_invalid_grant(session, tenant_id=actor.tenant_id, approval_id=approval.id, commit=False)
    return created
