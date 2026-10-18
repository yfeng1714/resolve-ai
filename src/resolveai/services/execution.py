"""Human approval and the only path that can submit a refund."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import ActionLedger, Approval, Proposal, ReplyDraft, Run, Ticket
from resolveai.domain.hashing import canonical_hash, refund_payload
from resolveai.domain.observations import parse_payments, parse_refunds
from resolveai.domain.policy import evaluate_refund
from resolveai.errors import APIError
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
) -> Proposal:
    _require_approver(actor)
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    _require_exact(proposal, revision, payload_hash)
    _require_current_input(session, proposal, ticket, run)
    if proposal.status != "open":
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
) -> Proposal:
    del tools
    _require_approver(actor)
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    _require_exact(proposal, revision, payload_hash)
    _require_current_input(session, proposal, ticket, run)
    if proposal.status != "open":
        raise APIError(409, "stale_proposal", "Only an open proposal revision can be rejected")
    proposal.status = "rejected"
    session.add(
        Approval(
            tenant_id=proposal.tenant_id,
            proposal_id=proposal.id,
            revision=proposal.revision,
            payload_hash=proposal.payload_hash,
            actor_user_id=actor.user_id,
            decision="rejected",
            created_at=utcnow(),
        )
    )
    body = render_reply(
        kind="rejected",
        ticket=ticket,
        evidence=proposal.evidence,
        amount_cents=proposal.amount_cents,
        action_id=proposal.action_id,
        operation_id=None,
        payment_id=proposal.target_payment_id,
        event_id=proposal.target_event_id,
    )
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
) -> ActionLedger:
    _require_approver(actor)
    proposal, ticket, run = _proposal_context(session, actor.tenant_id, proposal_id)
    _require_exact(proposal, revision, payload_hash)
    _require_current_input(session, proposal, ticket, run)
    if proposal.status == "superseded":
        raise APIError(409, "stale_proposal", "This proposal revision was superseded")
    if proposal.status not in {"approved", "executed"}:
        raise APIError(409, "not_approved", "Execution requires an approved revision")
    approval = session.scalar(select(Approval).where(Approval.proposal_id == proposal.id))
    if (
        approval is None
        or approval.decision != "approved"
        or approval.revision != proposal.revision
        or approval.payload_hash != proposal.payload_hash
    ):
        raise APIError(409, "not_approved", "Approval does not match this revision")
    if str(ticket.account_id) != proposal.preconditions.get("account_id"):
        raise APIError(403, "denied", "Proposal account does not match the ticket")

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
        return _accept_verified(
            session, actor, proposal, ticket, run, ledger, tools, recovered=False
        )
    if ledger is not None and (
        ledger.status == "unknown" or ledger.dispatch_state == "dispatched"
    ):
        if ledger.proposal_id != proposal.id:
            raise APIError(
                409,
                "stale_proposal",
                "This action is already in progress for another proposal",
            )
        return _recover(session, actor, proposal, ticket, run, ledger, tools)

    if not _proposal_matches_live_state(tools, ticket, proposal):
        _reject_before_write(session, actor, proposal, ticket, run)
        raise APIError(
            409,
            run.error_code or "stale_precondition",
            "Approved refund is no longer valid",
        )

    if ledger is not None and ledger.status == "rejected":
        if ledger.error_code not in DEFINITE_NO_WRITE:
            raise APIError(409, ledger.error_code or "rejected", "This action was rejected")
        looked = tools.get_operation(ticket.account_id, proposal.action_id)
        if looked.status == "ok" and looked.data.get("status") == "confirmed":
            return _accept_verified(
                session, actor, proposal, ticket, run, ledger, tools, recovered=True
            )
        if looked.error_code != "not_found":
            raise APIError(409, "unknown", "The previous attempt is still unresolved")
        ledger.status = "pending"
        ledger.dispatch_state = "reserved"
        ledger.error_code = None
        ledger.updated_at = utcnow()

    if ledger is None:
        now = utcnow()
        ledger = ActionLedger(
            tenant_id=actor.tenant_id,
            action_id=proposal.action_id,
            action_type="refund",
            proposal_id=proposal.id,
            reply_draft_id=None,
            payload_hash=proposal.payload_hash,
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
        audit(
            session,
            tenant_id=actor.tenant_id,
            event_type="action_reserved",
            actor_user_id=actor.user_id,
            ticket_id=ticket.id,
            run_id=run.id,
            payload={"action_id": ledger.action_id},
        )
        session.commit()
        _before_dispatch()

    if ledger.dispatch_state == "reserved":
        proposal, ticket, run, ledger = _commit_dispatch(
            session, actor, proposal.id, ledger.action_id
        )

    result = tools.create_refund(
        account_id=ticket.account_id,
        payment_id=proposal.target_payment_id,
        event_id=proposal.target_event_id,
        amount_cents=proposal.amount_cents,
        action_id=proposal.action_id,
        payload_hash=proposal.payload_hash,
        expected_payment_version=int(proposal.preconditions["payment_resource_version"]),
        policy_version=proposal.policy_version,
        fault=fault,
    )
    if result.status == "ok" and result.data.get("operation_status") == "confirmed":
        run.state = "verifying"
        run.updated_at = utcnow()
        return _accept_verified(
            session, actor, proposal, ticket, run, ledger, tools, recovered=False
        )
    return _consume_write_result(session, actor, proposal, ticket, run, ledger, result, tools)


def reconcile_action(session: Session, *, actor: Any, action_id: str, tools: Any) -> ActionLedger:
    _require_approver(actor)
    ledger = session.scalar(
        select(ActionLedger)
        .where(ActionLedger.tenant_id == actor.tenant_id, ActionLedger.action_id == action_id)
        .with_for_update()
    )
    if ledger is None or ledger.proposal_id is None:
        raise APIError(404, "not_found", "Action was not found")
    proposal = session.get(Proposal, ledger.proposal_id)
    if proposal is None or proposal.tenant_id != actor.tenant_id:
        raise APIError(404, "not_found", "Action was not found")
    ticket, run = _ticket_and_run(session, proposal)
    if ledger.status == "confirmed":
        return _accept_verified(
            session, actor, proposal, ticket, run, ledger, tools, recovered=False
        )
    return _accept_verified(
        session, actor, proposal, ticket, run, ledger, tools, recovered=True
    )


def send_reply(
    session: Session,
    *,
    actor: Any,
    reply_id: UUID,
    revision: int,
    body_hash_value: str,
) -> ReplyDraft:
    _require_approver(actor)
    draft = session.scalar(
        select(ReplyDraft)
        .where(ReplyDraft.id == reply_id, ReplyDraft.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if draft is None:
        raise APIError(404, "not_found", "Reply draft was not found")
    if draft.revision != revision or draft.body_hash != body_hash_value:
        raise APIError(409, "stale_proposal", "Reply revision does not match")
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
    draft = session.scalar(
        select(ReplyDraft)
        .where(ReplyDraft.id == reply_id, ReplyDraft.tenant_id == actor.tenant_id)
        .with_for_update()
    )
    if draft is None:
        raise APIError(404, "not_found", "Reply draft was not found")
    if draft.revision != revision or draft.body_hash != body_hash_value:
        raise APIError(409, "stale_proposal", "Reply revision does not match")
    if draft.status == "sent":
        raise APIError(409, "already_sent", "A sent reply cannot be rejected")
    if draft.status == "rejected":
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
    return _accept_verified(
        session, actor, proposal, ticket, run, ledger, tools, recovered=True
    )


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
) -> ActionLedger:
    run.state = "verifying"
    run.updated_at = utcnow()
    kind, receipt = _verified_receipt(tools, ticket, proposal)
    if kind != "ok" or receipt is None:
        _mark_uncertain(session, actor, proposal, ticket, run, ledger)
        return ledger
    previous = ledger.result if isinstance(ledger.result, dict) else {}
    if recovered or previous.get("recovered_from_unknown") is True:
        receipt = {**receipt, "recovered_from_unknown": True}
    ledger.status = "confirmed"
    ledger.dispatch_state = "settled"
    ledger.error_code = None
    ledger.operation_id = str(receipt["operation_id"])
    ledger.result = receipt
    ledger.updated_at = utcnow()
    _mark_executed(session, actor, proposal, ticket, run, ledger)
    return ledger


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
    matching = [
        row for row in refund_rows if row.get("action_id") == proposal.action_id
    ]
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
) -> None:
    ledger.status = "unknown"
    ledger.error_code = "unknown"
    ledger.updated_at = utcnow()
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


def _require_current_input(
    session: Session, proposal: Proposal, ticket: Ticket, run: Run
) -> None:
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
) -> None:
    proposal.status = "executed"
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


def _ticket_and_run(session: Session, proposal: Proposal) -> tuple[Ticket, Run]:
    ticket = _ticket(session, proposal.tenant_id, proposal.ticket_id)
    run = session.get(Run, proposal.run_id)
    if run is None or run.tenant_id != proposal.tenant_id:
        raise APIError(404, "not_found", "Run was not found")
    return ticket, run
