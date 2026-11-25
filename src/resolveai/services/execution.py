"""Human approval and the only path that can submit a refund."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import (
    ActionLedger,
    Approval,
    CombinedReply,
    Proposal,
    RecoveryJob,
    ReplyDraft,
    Run,
    Ticket,
)
from resolveai.domain.hashing import canonical_hash, refund_payload
from resolveai.domain.observations import parse_payments, parse_refunds
from resolveai.domain.policy import evaluate_refund
from resolveai.errors import APIError
from resolveai.locale import phrase
from resolveai.services.audit import audit
from resolveai.services.input import latest_input_revision
from resolveai.services.replies import append_message, new_receipt, render_reply, upsert_reply_draft
from resolveai.tools.client import ToolResult

DEFINITE_NO_WRITE = frozenset(
    {
        "stale",
        "not_eligible",
        "validation",
        "hash_mismatch",
        "denied",
        "not_found",
        "duplicate_business",
        "policy_rejected",
    }
)


def _before_send_receipt() -> None:
    """Test seam. The draft row lock is still held when this runs."""
    return None


def _after_combined_ticket_lock() -> None:
    """Test seam. The combined reply path holds the ticket lock and not the draft."""
    return None


def _before_dispatch() -> None:
    """Test seam. The reservation is committed and its locks are released."""
    return None


def approve_proposal(
    session: Session,
    *,
    actor: Any,
    proposal_id: UUID,
    revision: int,
    payload_hash: str,
    ttl_seconds: int,
) -> Proposal:
    _require_approver(actor)
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    _require_exact(proposal, revision, payload_hash)
    _require_current_input(session, proposal, ticket, run)
    from resolveai.services.grants import (
        action_is_admitted,
        current_grant,
        grant_deadline,
        latest_approval,
        release_invalid_grant,
    )

    latest = latest_approval(session, proposal.tenant_id, proposal.id)
    if latest is not None and current_grant(session, proposal) is None:
        release_invalid_grant(
            session, tenant_id=proposal.tenant_id, approval_id=latest.id, commit=False
        )
        session.refresh(proposal)
    if current_grant(session, proposal) is not None:
        raise APIError(409, "stale_proposal", "This proposal already has a valid grant")
    if action_is_admitted(session, proposal):
        session.commit()
        raise APIError(409, "already_executed", "An admitted refund cannot be approved again")
    if proposal.status != "open":
        session.commit()
        raise APIError(409, "stale_proposal", "Only an open proposal revision can be approved")
    proposal.status = "approved"
    session.add(
        Approval(
            tenant_id=proposal.tenant_id,
            proposal_id=proposal.id,
            revision=proposal.revision,
            payload_hash=proposal.payload_hash,
            actor_user_id=actor.user_id,
            decision="approved",
            expires_at=grant_deadline(session, ttl_seconds),
            created_at=utcnow(),
        )
    )
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="proposal_approved",
        actor_user_id=actor.user_id,
        ticket_id=proposal.ticket_id,
        run_id=proposal.run_id,
        payload={"proposal_id": str(proposal.id), "revision": proposal.revision},
    )
    return proposal


def reject_proposal(
    session: Session,
    *,
    actor: Any,
    proposal_id: UUID,
    revision: int,
    payload_hash: str,
    tools: Any,
    ttl_seconds: int,
) -> Proposal:
    del tools
    _require_approver(actor)
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    _require_exact(proposal, revision, payload_hash)
    _require_current_input(session, proposal, ticket, run)
    if proposal.status != "open":
        raise APIError(409, "stale_proposal", "Only an open proposal revision can be rejected")
    proposal.status = "rejected"
    from resolveai.services.grants import grant_deadline

    session.add(
        Approval(
            tenant_id=proposal.tenant_id,
            proposal_id=proposal.id,
            revision=proposal.revision,
            payload_hash=proposal.payload_hash,
            actor_user_id=actor.user_id,
            decision="rejected",
            expires_at=grant_deadline(session, ttl_seconds),
            created_at=utcnow(),
        )
    )
    body = (
        phrase(
            "reply_cancellation_rejected",
            customer_name=ticket.customer_label,
        )
        if proposal.action_type == "cancel_at_period_end"
        else render_reply(
            kind="rejected",
            ticket=ticket,
            evidence=proposal.evidence,
            amount_cents=proposal.amount_cents or 0,
            action_id=proposal.action_id,
            operation_id=None,
            payment_id=proposal.target_payment_id or UUID(int=0),
            event_id=proposal.target_event_id or "",
        )
    )
    from resolveai.services.combined import record_financial_state

    if record_financial_state(
        session, ticket, run, proposal, None, code="not_executed", reason="rejected"
    ):
        audit(
            session,
            tenant_id=actor.tenant_id,
            event_type="proposal_rejected",
            actor_user_id=actor.user_id,
            ticket_id=ticket.id,
            run_id=run.id,
            payload={"proposal_id": str(proposal.id)},
        )
        return proposal
    upsert_reply_draft(session, ticket=ticket, run=run, body=body, actor_user_id=actor.user_id)
    run.state = "awaiting_reply_review"
    run.updated_at = utcnow()
    ticket.status = "awaiting_reply"
    ticket.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="proposal_rejected",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"proposal_id": str(proposal.id)},
    )
    return proposal


def execute_proposal(
    session: Session,
    *,
    actor: Any,
    proposal_id: UUID,
    revision: int,
    payload_hash: str,
    tools: Any,
    fault: str | None = None,
    model_name: str | None = None,
) -> ActionLedger:
    _require_approver(actor)
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    _require_exact(proposal, revision, payload_hash)
    _require_current_input(session, proposal, ticket, run)
    if proposal.status == "superseded":
        raise APIError(409, "stale_proposal", "This proposal revision was superseded")
    from resolveai.services.grants import (
        current_grant,
        invalid_grant_code,
        latest_approval,
        release_invalid_grant,
    )

    latest = latest_approval(session, actor.tenant_id, proposal.id)
    if latest is not None and current_grant(session, proposal) is None:
        release_invalid_grant(
            session, tenant_id=actor.tenant_id, approval_id=latest.id, commit=False
        )
        session.refresh(proposal)
    approval = current_grant(session, proposal)
    if str(ticket.account_id) != proposal.preconditions.get("account_id"):
        raise APIError(403, "denied", "Proposal account does not match the ticket")
    if proposal.action_type == "cancel_at_period_end":
        return _execute_cancellation(
            session,
            actor=actor,
            proposal=proposal,
            ticket=ticket,
            run=run,
            tools=tools,
            model_name=model_name,
            approval_ready=approval is not None and proposal.status in {"approved", "executed"},
        )

    ledger = session.scalar(
        select(ActionLedger)
        .where(
            ActionLedger.tenant_id == actor.tenant_id,
            ActionLedger.action_id == proposal.action_id,
        )
        .with_for_update()
    )
    if ledger is not None and ledger.payload_hash != proposal.payload_hash:
        raise APIError(
            409,
            "payload_conflict",
            "This action identity was already used with a different payload",
        )
    if ledger is not None and ledger.status == "confirmed":
        if ledger.proposal_id != proposal.id:
            raise APIError(409, "already_executed", "This payment already has an executed refund")
        return _reconfirm_or_keep(session, actor, proposal, ticket, run, ledger, tools)
    if approval is None or proposal.status not in {"approved", "executed"}:
        session.commit()
        raise APIError(
            409,
            invalid_grant_code(session, proposal),
            "This refund grant is no longer valid",
        )
    from resolveai.services.tenancy import require_bound_account

    require_bound_account(session, ticket)
    if not _proposal_matches_live_state(tools, ticket, proposal):
        _reject_before_write(session, actor, proposal, ticket, run)
        raise APIError(
            409,
            run.error_code or "stale_precondition",
            "Approved refund is no longer valid",
        )
    from resolveai.services.recovery import enqueue_refund

    job = enqueue_refund(
        session,
        actor=actor,
        proposal=proposal,
        ticket=ticket,
        run=run,
        ledger=ledger,
        test_fault=fault,
        model_name=model_name,
    )
    session.commit()
    _before_dispatch()
    session.expire_all()
    current_job = session.get(type(job), job.id)
    current_proposal = session.get(type(proposal), proposal.id)
    if current_job is None or current_proposal is None or current_job.state == "cancelled":
        raise APIError(409, "stale_proposal", "New ticket input invalidated this proposal")
    return session.get(ActionLedger, job.ledger_id) or ledger


def _execute_cancellation(
    session: Session,
    *,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    tools: Any,
    model_name: str | None,
    approval_ready: bool,
) -> ActionLedger:
    from resolveai.domain.cancellation import canonical_instant
    from resolveai.services.cancellation import subscription_views
    from resolveai.services.grants import invalid_grant_code
    from resolveai.services.recovery import enqueue_cancellation
    from resolveai.services.tenancy import require_bound_account

    require_bound_account(session, ticket)
    ledger = session.scalar(
        select(ActionLedger)
        .where(
            ActionLedger.tenant_id == actor.tenant_id,
            ActionLedger.action_id == proposal.action_id,
        )
        .with_for_update()
    )
    if ledger is not None and ledger.status == "confirmed":
        return ledger
    if not approval_ready:
        session.commit()
        raise APIError(
            409,
            invalid_grant_code(session, proposal),
            "This cancellation grant is no longer valid",
        )
    read = tools.list_subscriptions(ticket.account_id)
    if read.status != "ok":
        raise APIError(503, "unavailable", "Subscriptions could not be read", retryable=True)
    viewed = next(
        (
            row
            for row in subscription_views(read.data)
            if row.id == str(proposal.target_subscription_id)
        ),
        None,
    )
    expected = proposal.preconditions.get("effective_at")
    version = proposal.preconditions.get("subscription_resource_version")
    if (
        viewed is None
        or viewed.resource_version != version
        or viewed.current_period_end != expected
    ):
        raise APIError(409, "version_conflict", "The subscription changed before scheduling")
    if viewed.current_period_end <= canonical_instant(utcnow()):
        raise APIError(409, "period_elapsed", "The period can no longer be scheduled")
    if viewed.cancel_at_period_end:
        raise APIError(409, "already_scheduled", "Cancellation is already scheduled")
    effect = str(proposal.preconditions.get("effect_hash") or "")
    if ledger is not None and ledger.payload_hash != effect:
        raise APIError(409, "payload_conflict", "This period was reserved with a different effect")
    if ledger is not None and ledger.status == "confirmed":
        return ledger
    job = enqueue_cancellation(
        session,
        actor=actor,
        proposal=proposal,
        ticket=ticket,
        run=run,
        ledger=ledger,
        model_name=model_name,
    )
    session.commit()
    found = session.get(ActionLedger, job.ledger_id)
    if found is None:
        raise APIError(503, "unavailable", "The cancellation reservation could not be reloaded")
    return found


def reconcile_action(session: Session, *, actor: Any, action_id: str, tools: Any) -> ActionLedger:
    """Manual read of an action. Worker-owned refunds keep the worker's transaction.

    Identifiers are read without locks, then ticket, proposal, run, ledger and
    job are locked in that order so this cannot deadlock a worker fence.
    """
    _require_approver(actor)
    identified = session.execute(
        select(ActionLedger.id, ActionLedger.proposal_id).where(
            ActionLedger.tenant_id == actor.tenant_id,
            ActionLedger.action_id == action_id,
        )
    ).one_or_none()
    if identified is None or identified.proposal_id is None:
        raise APIError(404, "not_found", "Action was not found")
    ledger_id, proposal_id = identified
    located = session.execute(
        select(Proposal.ticket_id, Proposal.run_id).where(
            Proposal.id == proposal_id,
            Proposal.tenant_id == actor.tenant_id,
        )
    ).one_or_none()
    if located is None:
        raise APIError(404, "not_found", "Action was not found")
    ticket_id, run_id = located
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id, Ticket.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    proposal = session.scalar(
        select(Proposal)
        .where(Proposal.id == proposal_id, Proposal.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    run = session.scalar(
        select(Run).where(Run.id == run_id, Run.tenant_id == actor.tenant_id).with_for_update()
    )
    ledger = session.scalar(
        select(ActionLedger)
        .where(ActionLedger.id == ledger_id, ActionLedger.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if ticket is None or proposal is None or run is None or ledger is None:
        raise APIError(404, "not_found", "Action was not found")
    job = session.scalar(
        select(RecoveryJob)
        .where(RecoveryJob.ledger_id == ledger.id, RecoveryJob.tenant_id == actor.tenant_id)
        .order_by(RecoveryJob.created_at.desc())
        .limit(1)
        .with_for_update()
    )
    for row in (ticket, proposal, run, ledger, job):
        if row is not None:
            session.refresh(row)
    if job is not None and ledger.status != "confirmed":
        raise APIError(409, "stale_proposal", "The recovery worker owns this action")
    if ledger.status == "confirmed":
        return _reconfirm_or_keep(session, actor, proposal, ticket, run, ledger, tools)
    return _accept_verified(session, actor, proposal, ticket, run, ledger, tools, recovered=True)


def _reply_was_sent(session: Session, ticket_id: UUID) -> bool:
    sent = session.scalar(
        select(ReplyDraft.id)
        .where(ReplyDraft.ticket_id == ticket_id, ReplyDraft.status == "sent")
        .limit(1)
    )
    return sent is not None


def _reconfirm_or_keep(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger,
    tools: Any,
) -> ActionLedger:
    """Keep a stored confirmation when a later read is unavailable or a reply was sent."""
    if _reply_was_sent(session, ticket.id):
        return ledger
    from resolveai.services.tenancy import require_bound_account

    require_bound_account(session, ticket)
    if proposal.action_type == "cancel_at_period_end":
        kind, _receipt = _verified_cancellation(tools, ticket, proposal)
        if kind in {"conflict", "missing"}:
            raise APIError(
                409,
                "inconsistent_recovery",
                "The confirmed cancellation does not match the simulator",
            )
        return ledger
    kind, receipt = _verified_receipt(tools, ticket, proposal)
    if kind == "ok" and receipt is not None:
        return _accept_verified(
            session,
            actor,
            proposal,
            ticket,
            run,
            ledger,
            tools,
            recovered=False,
            prepared=(kind, receipt),
        )
    if kind == "conflict":
        raise APIError(
            409,
            "inconsistent_recovery",
            "The confirmed refund does not match the simulator",
        )
    return ledger


def send_reply(
    session: Session,
    *,
    actor: Any,
    reply_id: UUID,
    revision: int,
    body_hash_value: str,
) -> ReplyDraft:
    _require_approver(actor)
    from resolveai.services.combined import (
        combined_reply_for,
        lock_current_halves,
        reply_matches_current,
    )
    from resolveai.services.incident import lock_incident_reply

    located = session.execute(
        select(ReplyDraft.ticket_id, CombinedReply.id)
        .outerjoin(CombinedReply, CombinedReply.reply_draft_id == ReplyDraft.id)
        .where(ReplyDraft.id == reply_id, ReplyDraft.tenant_id == actor.tenant_id)
    ).one_or_none()
    if located is not None and located[1] is not None:
        ticket = _lock_ticket(session, actor.tenant_id, located[0])
        _after_combined_ticket_lock()
        lock_current_halves(session, ticket)
    else:
        lock_incident_reply(session, reply_id, sending=True)
        ticket = None
    draft = _locked_reply(session, actor.tenant_id, reply_id)
    if draft is None:
        raise APIError(404, "not_found", "Reply draft was not found")
    if draft.revision != revision or draft.body_hash != body_hash_value:
        raise APIError(409, "stale_proposal", "Reply revision does not match")
    if ticket is None:
        ticket = _ticket(session, actor.tenant_id, draft.ticket_id)
    run = session.get(Run, draft.run_id)
    if run is None:
        raise APIError(404, "not_found", "Run was not found")
    if draft.recipient != ticket.customer_email:
        raise APIError(403, "denied", "Reply recipient does not match the customer")
    ledger = session.scalar(
        select(ActionLedger)
        .where(
            ActionLedger.tenant_id == actor.tenant_id,
            ActionLedger.action_id == draft.send_action_id,
        )
        .with_for_update()
    )
    if draft.status == "sent" and ledger is not None and ledger.status == "confirmed":
        return draft
    binding = combined_reply_for(session, draft.id)
    if binding is not None and not reply_matches_current(session, ticket, binding):
        raise APIError(409, "stale_proposal", "The combined reply is not current")
    if draft.status != "draft":
        raise APIError(409, "stale_proposal", "Only an unsent draft can be sent")
    if ledger is not None and ledger.payload_hash != draft.body_hash:
        raise APIError(409, "payload_conflict", "Send identity was used with a different body")
    now = utcnow()
    if ledger is None:
        ledger = ActionLedger(
            tenant_id=actor.tenant_id,
            action_id=draft.send_action_id,
            action_type="reply",
            proposal_id=None,
            reply_draft_id=draft.id,
            payload_hash=draft.body_hash,
            status="pending",
            dispatch_state="reserved",
            operation_id=None,
            result=None,
            error_code=None,
            created_at=now,
            updated_at=now,
        )
        session.add(ledger)
        session.flush()
    _before_send_receipt()
    receipt = new_receipt(draft)
    draft.status = "sent"
    draft.receipt = receipt
    ledger.status = "confirmed"
    ledger.dispatch_state = "settled"
    ledger.result = receipt
    ledger.operation_id = receipt["receipt_id"]
    ledger.updated_at = utcnow()
    append_message(session, ticket=ticket, author_kind="system", body=draft.body)
    ticket.status = "reply_sent"
    ticket.updated_at = utcnow()
    run.state = "completed"
    run.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="reply_sent",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"reply_id": str(draft.id), "receipt_id": receipt["receipt_id"]},
    )
    session.commit()
    return draft


def reject_reply(
    session: Session,
    *,
    actor: Any,
    reply_id: UUID,
    revision: int,
    body_hash_value: str,
) -> None:
    _require_approver(actor)
    from resolveai.db.models import CombinedReply
    from resolveai.services.incident import lock_incident_reply

    located = session.execute(
        select(ReplyDraft.ticket_id, CombinedReply.id)
        .outerjoin(CombinedReply, CombinedReply.reply_draft_id == ReplyDraft.id)
        .where(ReplyDraft.id == reply_id, ReplyDraft.tenant_id == actor.tenant_id)
    ).one_or_none()
    handoff = None
    if located is not None and located[1] is not None:
        _lock_ticket(session, actor.tenant_id, located[0])
        _after_combined_ticket_lock()
    else:
        handoff = lock_incident_reply(session, reply_id, sending=False)
    draft = _locked_reply(session, actor.tenant_id, reply_id)
    if draft is None:
        raise APIError(404, "not_found", "Reply draft was not found")
    if draft.revision != revision or draft.body_hash != body_hash_value:
        raise APIError(409, "stale_proposal", "Reply revision does not match")
    if draft.status == "sent":
        raise APIError(409, "already_sent", "A sent reply cannot be rejected")
    if draft.status == "rejected":
        return
    if handoff is not None:
        from resolveai.services.incident import incident_reply_is_current

        current_ticket = session.get(Ticket, draft.ticket_id)
        if current_ticket is not None and not incident_reply_is_current(
            session, handoff, current_ticket, draft
        ):
            if draft.status == "draft":
                draft.status = "rejected"
            return
    from resolveai.services.combined import combined_reply_for, reply_matches_current

    binding = combined_reply_for(session, draft.id)
    if binding is not None:
        current_ticket = session.scalar(
            select(Ticket).where(Ticket.id == draft.ticket_id).with_for_update()
        )
        current = current_ticket is not None and reply_matches_current(
            session, current_ticket, binding
        )
        if draft.status == "draft":
            draft.status = "rejected"
            session.flush()
            audit(
                session,
                tenant_id=actor.tenant_id,
                event_type="reply_rejected",
                actor_user_id=actor.user_id,
                ticket_id=draft.ticket_id,
                run_id=draft.run_id,
                payload={"reply_id": str(draft.id)},
            )
        if not current or current_ticket is None:
            return
        from resolveai.services.combined import reduce_combined_reply

        current_run = session.get(Run, draft.run_id)
        if current_run is not None:
            reduce_combined_reply(session, current_ticket, current_run)
        return
    draft.status = "rejected"
    ticket = _ticket(session, actor.tenant_id, draft.ticket_id)
    run = session.get(Run, draft.run_id)
    if run is None:
        raise APIError(404, "not_found", "Run was not found")
    ticket.status = "open"
    ticket.updated_at = utcnow()
    run.state = "completed"
    run.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="reply_rejected",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"reply_id": str(draft.id)},
    )


def _consume_write_result(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger,
    result: ToolResult,
    tools: Any,
) -> ActionLedger:
    lost_response = result.error_code == "unknown" or result.status != "ok"
    if result.status == "ok" and result.data.get("operation_status") == "confirmed":
        run.state = "verifying"
        run.updated_at = utcnow()
        return _accept_verified(
            session, actor, proposal, ticket, run, ledger, tools, recovered=False
        )
    if lost_response and result.error_code not in DEFINITE_NO_WRITE:
        ledger.status = "unknown"
        ledger.error_code = "unknown"
        ledger.updated_at = utcnow()
        run.state = "verifying"
        run.updated_at = utcnow()
        session.commit()
        return _accept_verified(
            session, actor, proposal, ticket, run, ledger, tools, recovered=True
        )
    code = result.error_code or "simulator_error"
    if code in DEFINITE_NO_WRITE:
        ledger.status = "rejected"
        ledger.dispatch_state = "settled"
        ledger.error_code = code
        ledger.updated_at = utcnow()
        run.state = "failed"
        run.error_code = code
        run.updated_at = utcnow()
        session.commit()
        raise APIError(409, code, "The simulator rejected the refund without a new write")
    ledger.status = "unknown"
    ledger.error_code = "unknown"
    ledger.updated_at = utcnow()
    session.commit()
    return _recover(session, actor, proposal, ticket, run, ledger, tools)


def _recover(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger,
    tools: Any,
) -> ActionLedger:
    return _accept_verified(session, actor, proposal, ticket, run, ledger, tools, recovered=True)


def _accept_verified(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger,
    tools: Any,
    *,
    recovered: bool,
    commit: bool = True,
    prepared: tuple[str, dict[str, Any] | None] | None = None,
) -> ActionLedger:
    if prepared is None:
        from resolveai.services.tenancy import require_bound_account

        require_bound_account(session, ticket)
        kind, receipt = _verified_receipt(tools, ticket, proposal)
    else:
        kind, receipt = prepared
    if kind != "ok" or receipt is None:
        if commit:
            _mark_uncertain(session, actor, proposal, ticket, run, ledger)
        return ledger
    run.state = "verifying"
    run.updated_at = utcnow()
    previous = ledger.result if isinstance(ledger.result, dict) else {}
    if recovered or previous.get("recovered_from_unknown") is True:
        receipt = {**receipt, "recovered_from_unknown": True}
    ledger.status = "confirmed"
    ledger.dispatch_state = "settled"
    ledger.error_code = None
    ledger.operation_id = str(receipt["operation_id"])
    ledger.result = receipt
    ledger.updated_at = utcnow()
    _mark_executed(session, actor, proposal, ticket, run, ledger, commit=commit)
    return ledger


def _verified_cancellation(
    tools: Any, ticket: Ticket, proposal: Proposal
) -> tuple[str, dict[str, Any] | None]:
    """Match this cancellation's operation. Another flag is not a receipt."""
    from resolveai.services.cancellation import receipt_matches, subscription_views

    operation = tools.get_operation(ticket.account_id, proposal.action_id)
    listing = tools.list_subscriptions(ticket.account_id)
    if operation.status != "ok" or listing.status != "ok":
        if operation.error_code == "not_found" and listing.status == "ok":
            return "missing", None
        return "unavailable", None
    data = operation.data if isinstance(operation.data, dict) else {}
    viewed = next(
        (
            row
            for row in subscription_views(listing.data)
            if row.id == str(proposal.target_subscription_id)
        ),
        None,
    )
    subscription = (
        {}
        if viewed is None
        else {
            "id": viewed.id,
            "cancel_at_period_end": viewed.cancel_at_period_end,
            "current_period_end": viewed.current_period_end,
        }
    )
    command = {
        "effect_hash": str((proposal.preconditions or {}).get("effect_hash") or ""),
        "action_id": proposal.action_id,
        "account_id": str(ticket.account_id),
        "subscription_id": str(proposal.target_subscription_id),
        "effective_at": (proposal.preconditions or {}).get("effective_at"),
    }
    if receipt_matches(command, data, subscription):
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        return "ok", result
    return "conflict", None


def _verified_receipt(
    tools: Any, ticket: Ticket, proposal: Proposal
) -> tuple[str, dict[str, Any] | None]:
    """Read the simulator again and match it to the approved proposal.

    The write response itself is not evidence. ``ok`` is the only kind that may
    become a success reply. ``missing``, ``conflict`` and ``unavailable`` stay uncertain.
    """
    operation = tools.get_operation(ticket.account_id, proposal.action_id)
    refunds = tools.list_refunds(ticket.account_id)
    operation_missing = operation.error_code == "not_found"
    refund_rows: list[dict[str, Any]] = []
    if refunds.status == "ok":
        raw_rows = refunds.data.get("refunds", [])
        if isinstance(raw_rows, list):
            refund_rows = [row for row in raw_rows if isinstance(row, dict)]
    matching = [row for row in refund_rows if row.get("action_id") == proposal.action_id]
    if operation.status != "ok" and not operation_missing:
        return "unavailable", None
    if refunds.status != "ok" and not operation_missing:
        return "unavailable", None
    if operation_missing and refunds.status != "ok":
        return "unavailable", None
    if operation_missing and not matching:
        return "missing", None
    if operation_missing or not matching:
        return "conflict", None
    data = operation.data if isinstance(operation.data, dict) else {}
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    refund = matching[0]
    result_account = result.get("account_id")
    if result_account not in (None, "", str(ticket.account_id)):
        return "conflict", None
    operation_id = str(data.get("operation_id") or result.get("operation_id") or "")
    expected_payment = str(proposal.target_payment_id)
    checks = (
        data.get("status") == "confirmed",
        data.get("action_id") == proposal.action_id,
        data.get("payload_hash") == proposal.payload_hash,
        bool(operation_id),
        str(result.get("payment_id") or "") == expected_payment,
        result.get("event_id") == proposal.target_event_id,
        _as_int(result.get("amount_cents")) == proposal.amount_cents,
        result.get("currency") == proposal.currency == "USD",
        str(result.get("operation_id") or operation_id) == operation_id,
        str(refund.get("payment_id") or "") == expected_payment,
        refund.get("event_id") == proposal.target_event_id,
        _as_int(refund.get("amount_cents")) == proposal.amount_cents,
        refund.get("currency") == "USD",
        refund.get("status") == "succeeded",
        str(refund.get("operation_id") or "") == operation_id,
    )
    if not all(checks):
        return "conflict", None
    return "ok", {
        "operation_status": "confirmed",
        "operation_id": operation_id,
        "action_id": proposal.action_id,
        "amount_cents": proposal.amount_cents,
        "currency": "USD",
        "payment_id": expected_payment,
        "event_id": proposal.target_event_id,
        "refund_id": refund.get("id"),
        "resource_version": result.get("resource_version"),
    }


def _mark_uncertain(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger,
    *,
    commit: bool = True,
) -> None:
    ledger.status = "unknown"
    ledger.error_code = "unknown"
    ledger.updated_at = utcnow()
    from resolveai.services.combined import record_financial_state

    if record_financial_state(
        session, ticket, run, proposal, None, code="uncertain", reason="unknown"
    ):
        audit(
            session,
            tenant_id=actor.tenant_id,
            event_type="action_unknown",
            actor_user_id=actor.user_id,
            ticket_id=ticket.id,
            run_id=run.id,
            payload={"action_id": ledger.action_id},
        )
        if commit:
            session.commit()
        return
    body = render_reply(
        kind="unknown",
        ticket=ticket,
        evidence=proposal.evidence,
        amount_cents=proposal.amount_cents,
        action_id=proposal.action_id,
        operation_id=None,
        payment_id=proposal.target_payment_id,
        event_id=proposal.target_event_id,
    )
    draft = upsert_reply_draft(
        session, ticket=ticket, run=run, body=body, actor_user_id=actor.user_id
    )
    ledger.reply_draft_id = draft.id
    if proposal.status == "executed":
        proposal.status = "approved"
    run.state = "awaiting_reply_review"
    run.error_code = "unknown"
    run.updated_at = utcnow()
    ticket.status = "awaiting_reply"
    ticket.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="action_unknown",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"action_id": ledger.action_id},
    )
    if commit:
        session.commit()


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _lock_ticket_for_proposal(session: Session, proposal: Proposal) -> tuple[Ticket, Run]:
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == proposal.ticket_id, Ticket.tenant_id == proposal.tenant_id)
        .with_for_update()
    )
    run = session.get(Run, proposal.run_id)
    if ticket is None or run is None or run.tenant_id != proposal.tenant_id:
        raise APIError(404, "not_found", "Ticket was not found")
    return ticket, run


def _commit_dispatch(
    session: Session,
    actor: Any,
    proposal_id: UUID,
    action_id: str,
) -> tuple[Proposal, Ticket, Run, ActionLedger]:
    """Recheck the ticket under its lock, then commit the dispatch cutoff.

    The reservation commit releases the ticket lock. A withdrawal can land in
    that gap, so this reloads the proposal and run before any simulator call.
    The commit at the end is the cutoff: input that arrives afterward does not
    cancel a write that has already been dispatched.
    """
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    ledger = session.scalar(
        select(ActionLedger)
        .where(ActionLedger.tenant_id == actor.tenant_id, ActionLedger.action_id == action_id)
        .with_for_update()
    )
    if ledger is None:
        raise APIError(409, "unknown", "The reserved refund could not be reloaded")
    session.refresh(proposal)
    session.refresh(ticket)
    session.refresh(run)
    _require_current_input(session, proposal, ticket, run)
    if proposal.status != "approved" or run.state == "cancelled":
        raise APIError(409, "stale_proposal", "New ticket input invalidated this proposal")
    if ledger.dispatch_state != "reserved":
        raise APIError(409, "unknown", "Only a reserved attempt can be dispatched")
    # A reserved attempt has not been sent. Point it at the proposal being
    # dispatched. Dispatched or unknown attempts keep the proposal they already
    # belong to, so a later approval cannot rewrite that history.
    if ledger.proposal_id != proposal.id:
        ledger.proposal_id = proposal.id
    ledger.dispatch_state = "dispatched"
    ledger.status = "pending"
    ledger.updated_at = utcnow()
    run.state = "executing"
    run.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="action_dispatched",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"action_id": ledger.action_id, "proposal_id": str(proposal.id)},
    )
    session.commit()
    return proposal, ticket, run, ledger


def _require_current_input(session: Session, proposal: Proposal, ticket: Ticket, run: Run) -> None:
    current = latest_input_revision(session, ticket.id)
    bound = (proposal.preconditions or {}).get("input_revision")
    if bound is None or int(bound) != current:
        proposal.status = "superseded"
        if run.state in {
            "analyzing",
            "gathering_evidence",
            "proposing",
            "awaiting_customer",
            "awaiting_approval",
        }:
            run.state = "cancelled"
            run.error_code = "input_changed"
            run.updated_at = utcnow()
        session.commit()
        raise APIError(409, "stale_proposal", "New ticket input invalidated this proposal")


def _mark_executed(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger,
    *,
    commit: bool = True,
) -> None:
    proposal.status = "executed"
    from resolveai.services.combined import record_financial_state

    if record_financial_state(session, ticket, run, proposal, None, code="confirmed"):
        audit(
            session,
            tenant_id=actor.tenant_id,
            event_type="action_confirmed",
            actor_user_id=actor.user_id,
            ticket_id=ticket.id,
            run_id=run.id,
            payload={"action_id": ledger.action_id, "operation_id": ledger.operation_id},
        )
        if commit:
            session.commit()
        return
    body = render_reply(
        kind="confirmed",
        ticket=ticket,
        evidence=proposal.evidence,
        amount_cents=proposal.amount_cents,
        action_id=proposal.action_id,
        operation_id=ledger.operation_id,
        payment_id=proposal.target_payment_id,
        event_id=proposal.target_event_id,
    )
    draft = upsert_reply_draft(
        session, ticket=ticket, run=run, body=body, actor_user_id=actor.user_id
    )
    ledger.reply_draft_id = draft.id
    run.state = "awaiting_reply_review"
    run.error_code = None
    run.updated_at = utcnow()
    ticket.status = "awaiting_reply"
    ticket.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="action_confirmed",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"action_id": ledger.action_id, "operation_id": ledger.operation_id},
    )
    if commit:
        session.commit()


def _proposal_matches_live_state(tools: Any, ticket: Ticket, proposal: Proposal) -> bool:
    payments = tools.list_payments(ticket.account_id)
    refunds = tools.list_refunds(ticket.account_id)
    if payments.status != "ok" or refunds.status != "ok":
        raise APIError(503, "unavailable", "Billing records could not be read", retryable=True)
    decision = evaluate_refund(parse_payments(payments.data), parse_refunds(refunds.data), utcnow())
    if (
        decision.outcome != "eligible"
        or decision.payment_id != proposal.target_payment_id
        or decision.amount_cents != proposal.amount_cents
        or decision.event_id != proposal.target_event_id
        or decision.policy_version != proposal.policy_version
    ):
        proposal.status = "failed"
        return False
    expected = canonical_hash(
        refund_payload(
            action_id=proposal.action_id,
            account_id=ticket.account_id,
            payment_id=proposal.target_payment_id,
            event_id=proposal.target_event_id,
            amount_cents=proposal.amount_cents,
            policy_version=proposal.policy_version,
        )
    )
    if expected != proposal.payload_hash:
        proposal.status = "failed"
        return False
    if decision.payment_version != int(proposal.preconditions["payment_resource_version"]):
        proposal.status = "stale"
        return False
    return True


def _reject_before_write(
    session: Session,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
) -> None:
    code = "stale_precondition" if proposal.status == "stale" else "policy_rejected"
    run.state = "failed"
    run.error_code = code
    run.updated_at = utcnow()
    ticket.status = "open"
    ticket.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="stale_proposal" if code == "stale_precondition" else "policy_rejected",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"proposal_id": str(proposal.id), "error_code": code},
    )
    session.commit()


def _proposal_context(
    session: Session, tenant_id: UUID, proposal_id: UUID
) -> tuple[Proposal, Ticket, Run]:
    peeked = session.get(Proposal, proposal_id)
    if peeked is None or peeked.tenant_id != tenant_id:
        raise APIError(404, "not_found", "Proposal was not found")
    ticket, run = _lock_ticket_for_proposal(session, peeked)
    proposal = _lock_proposal(session, tenant_id, proposal_id)
    session.refresh(proposal)
    session.refresh(ticket)
    session.refresh(run)
    return proposal, ticket, run


def _lock_proposal(session: Session, tenant_id: UUID, proposal_id: UUID) -> Proposal:
    proposal = session.scalar(
        select(Proposal)
        .where(Proposal.id == proposal_id, Proposal.tenant_id == tenant_id)
        .with_for_update()
    )
    if proposal is None:
        raise APIError(404, "not_found", "Proposal was not found")
    return proposal


def _require_exact(proposal: Proposal, revision: int, payload_hash: str) -> None:
    if proposal.revision != revision or proposal.payload_hash != payload_hash:
        raise APIError(409, "stale_proposal", "Revision and hash do not match this proposal")


def _require_approver(actor: Any) -> None:
    if actor.role != "approver":
        raise APIError(403, "denied", "This action requires the approver role")


def _ticket(session: Session, tenant_id: UUID, ticket_id: UUID) -> Ticket:
    ticket = session.get(Ticket, ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise APIError(404, "not_found", "Ticket was not found")
    return ticket


def _locked_reply(session: Session, tenant_id: UUID, reply_id: UUID) -> ReplyDraft | None:
    """Lock the reply and replace any copy loaded before this wait.

    The HTTP handler may have read the draft before the ticket lock. A later
    FOR UPDATE returns that same object unless its attributes are reloaded
    from the row committed by the session that held the lock.
    """
    loaded = session.get(ReplyDraft, reply_id)
    if loaded is not None:
        session.expire(loaded)
    draft = session.scalar(
        select(ReplyDraft)
        .where(ReplyDraft.id == reply_id, ReplyDraft.tenant_id == tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if draft is not None:
        session.refresh(draft)
    return draft


def _lock_ticket(session: Session, tenant_id: UUID, ticket_id: UUID) -> Ticket:
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
        .with_for_update()
    )
    if ticket is None:
        raise APIError(404, "not_found", "Ticket was not found")
    return ticket


def _ticket_and_run(session: Session, proposal: Proposal) -> tuple[Ticket, Run]:
    ticket = _ticket(session, proposal.tenant_id, proposal.ticket_id)
    run = session.get(Run, proposal.run_id)
    if run is None or run.tenant_id != proposal.tenant_id:
        raise APIError(404, "not_found", "Run was not found")
    return ticket, run
