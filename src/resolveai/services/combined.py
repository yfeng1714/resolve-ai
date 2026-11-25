"""One refund and one period-end cancellation on the same ticket.

Outcome rows are inserted, never updated. The reducer is the only writer of
the combined customer draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import (
    ActionLedger,
    CombinedHalf,
    CombinedReply,
    Proposal,
    RecoveryJob,
    ReplyDraft,
    Run,
    Ticket,
)
from resolveai.services.audit import audit
from resolveai.services.input import latest_input_revision

HALVES = ("refund", "cancel_at_period_end")
REPLY_READY = {"confirmed", "uncertain", "waiting", "not_dispatched", "not_executed"}


@dataclass(frozen=True)
class AuthoritativeEffect:
    """A confirmed ledger or an admitted command. Cached half codes do not outrank it."""

    code: str
    action_id: str
    proposal_id: UUID | None
    recovery_job_id: UUID | None


def authoritative_effect(
    session: Session, ticket: Ticket, action_type: str
) -> AuthoritativeEffect | None:
    """Return the real financial effect for this action type, when one exists."""
    confirmed = session.execute(
        select(ActionLedger, Proposal)
        .join(Proposal, Proposal.id == ActionLedger.proposal_id)
        .where(
            Proposal.ticket_id == ticket.id,
            Proposal.tenant_id == ticket.tenant_id,
            Proposal.action_type == action_type,
            ActionLedger.tenant_id == ticket.tenant_id,
            ActionLedger.status == "confirmed",
        )
        .order_by(ActionLedger.updated_at.desc(), ActionLedger.id.desc())
        .limit(1)
    ).first()
    if confirmed is not None:
        ledger, proposal = confirmed
        job_id = session.scalar(
            select(RecoveryJob.id)
            .where(
                RecoveryJob.ledger_id == ledger.id,
                RecoveryJob.tenant_id == ticket.tenant_id,
                RecoveryJob.admitted_at.is_not(None),
            )
            .order_by(RecoveryJob.admitted_at.desc())
            .limit(1)
        )
        return AuthoritativeEffect("confirmed", ledger.action_id, proposal.id, job_id)
    admitted = session.execute(
        select(RecoveryJob, Proposal)
        .join(Proposal, Proposal.id == RecoveryJob.proposal_id)
        .where(
            Proposal.ticket_id == ticket.id,
            Proposal.tenant_id == ticket.tenant_id,
            Proposal.action_type == action_type,
            RecoveryJob.tenant_id == ticket.tenant_id,
            RecoveryJob.admitted_at.is_not(None),
        )
        .order_by(RecoveryJob.admitted_at.desc(), RecoveryJob.id.desc())
        .limit(1)
    ).first()
    if admitted is None:
        return None
    job, proposal = admitted
    return AuthoritativeEffect("uncertain", proposal.action_id, proposal.id, job.id)


def _remember_effect(
    session: Session,
    ticket: Ticket,
    run: Run,
    half: str,
    effect: AuthoritativeEffect,
    *,
    prior_outcome_id: UUID | None = None,
    input_revision: int | None = None,
) -> bool:
    """Insert a version only when this ticket is a combined request and the effect is new."""
    if not ticket_is_combined(session, ticket):
        return False
    current = current_half(
        session,
        ticket.id,
        half,
        latest_input_revision(session, ticket.id) if input_revision is None else input_revision,
    )
    if (
        current is not None
        and current.outcome_code == effect.code
        and current.action_id == effect.action_id
    ):
        return False
    append_outcome(
        session,
        ticket,
        run,
        half=half,
        code=effect.code,
        action_id=effect.action_id,
        proposal_id=effect.proposal_id,
        recovery_job_id=effect.recovery_job_id,
        input_revision=input_revision,
        prior_outcome_id=prior_outcome_id,
    )
    return True


def ticket_is_combined(session: Session, ticket: Ticket) -> bool:
    from resolveai.db.models import Message
    from resolveai.domain.cancellation import intent_gate
    from resolveai.domain.incident import incident_requested

    rows = session.scalars(
        select(Message)
        .where(
            Message.ticket_id == ticket.id,
            Message.author_kind.in_(("customer", "operator")),
        )
        .order_by(Message.revision)
    ).all()
    messages = [{"body": row.body} for row in rows]
    if incident_requested(messages):
        return False
    return intent_gate(messages) == "combined_intent"


def current_half(
    session: Session, ticket_id: UUID, half: str, revision: int | None = None
) -> CombinedHalf | None:
    stmt = select(CombinedHalf).where(
        CombinedHalf.ticket_id == ticket_id,
        CombinedHalf.half == half,
    )
    if revision is not None:
        stmt = stmt.where(CombinedHalf.input_revision == revision)
    return session.scalar(
        stmt.order_by(
            CombinedHalf.input_revision.desc(),
            CombinedHalf.outcome_version.desc(),
            CombinedHalf.id.desc(),
        ).limit(1)
    )


def current_halves(session: Session, ticket_id: UUID) -> dict[str, CombinedHalf]:
    revision = latest_input_revision(session, ticket_id)
    found = {}
    for half in HALVES:
        row = current_half(session, ticket_id, half, revision)
        if row is not None:
            found[half] = row
    return found


def append_outcome(
    session: Session,
    ticket: Ticket,
    run: Run,
    *,
    half: str,
    code: str,
    reason: str = "",
    action_id: str | None = None,
    proposal_id: UUID | None = None,
    recovery_job_id: UUID | None = None,
    input_revision: int | None = None,
    prior_outcome_id: UUID | None = None,
) -> CombinedHalf:
    revision = (
        latest_input_revision(session, ticket.id) if input_revision is None else input_revision
    )
    current = current_half(session, ticket.id, half, revision)
    if (
        current is not None
        and current.outcome_code == code
        and current.reason_code == reason
        and current.action_id == action_id
        and current.proposal_id == proposal_id
        and current.recovery_job_id == recovery_job_id
    ):
        return current
    version = 1 if current is None else int(current.outcome_version) + 1
    created = CombinedHalf(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        run_id=run.id,
        input_revision=revision,
        half=half,
        outcome_version=version,
        outcome_code=code,
        reason_code=reason,
        action_id=action_id,
        proposal_id=proposal_id,
        recovery_job_id=recovery_job_id,
        prior_outcome_id=current.id
        if prior_outcome_id is None and current is not None
        else prior_outcome_id,
        created_at=utcnow(),
    )
    session.add(created)
    session.flush()
    return created


def record_financial_state(
    session: Session,
    ticket: Ticket,
    run: Run,
    proposal: Proposal,
    job: RecoveryJob | None,
    *,
    code: str | None = None,
    reason: str = "",
) -> bool:
    if not ticket_is_combined(session, ticket):
        return False
    half = "refund" if proposal.action_type == "refund" else "cancel_at_period_end"
    resolved, resolved_reason = (code, reason) if code else _from_job(proposal, job)
    if resolved not in {"waiting", "not_executed", "not_dispatched"}:
        resolved_reason = ""
    if job is None and resolved in {"confirmed", "uncertain"}:
        job = session.scalar(
            select(RecoveryJob)
            .where(
                RecoveryJob.proposal_id == proposal.id,
                RecoveryJob.admitted_at.is_not(None),
            )
            .order_by(RecoveryJob.admitted_at.desc())
            .limit(1)
        )
    append_outcome(
        session,
        ticket,
        run,
        half=half,
        code=resolved,
        reason=resolved_reason,
        action_id=proposal.action_id,
        proposal_id=proposal.id,
        recovery_job_id=None if job is None else job.id,
    )
    reduce_combined_reply(session, ticket, run)
    return True


def _from_job(proposal: Proposal, job: RecoveryJob | None) -> tuple[str, str]:
    if job is None:
        if proposal.status == "rejected":
            return "not_executed", "rejected"
        return "open", ""
    if job.admitted_at is not None:
        return "uncertain", job.block_reason or "unknown"
    if job.state == "blocked" and job.block_reason == "unavailable":
        return "not_dispatched", "unavailable"
    if job.state == "blocked":
        return "not_executed", job.block_reason or "not_executed"
    if job.state in {"queued", "leased"}:
        return "in_flight", ""
    if job.state == "cancelled":
        return "not_executed", job.block_reason or "input_changed"
    return "open", ""


def ensure_investigation_halves(session: Session, ticket: Ticket, run: Run) -> None:
    if not ticket_is_combined(session, ticket):
        return
    proposals = session.scalars(
        select(Proposal).where(Proposal.ticket_id == ticket.id, Proposal.run_id == run.id)
    ).all()
    refunds = [
        row for row in proposals if row.action_type == "refund" and row.status != "superseded"
    ]
    cancels = [
        row
        for row in proposals
        if row.action_type == "cancel_at_period_end" and row.status != "superseded"
    ]
    _record_investigation_half(
        session,
        ticket,
        run,
        half="refund",
        action_type="refund",
        proposal=None if not refunds else refunds[-1],
        absent_code="not_executed",
        absent_reason="no_duplicate",
    )
    if cancels:
        _record_investigation_half(
            session,
            ticket,
            run,
            half="cancel_at_period_end",
            action_type="cancel_at_period_end",
            proposal=cancels[-1],
            absent_code="not_executed",
            absent_reason="not_eligible",
        )
    elif run.state == "awaiting_customer":
        _record_investigation_half(
            session,
            ticket,
            run,
            half="cancel_at_period_end",
            action_type="cancel_at_period_end",
            proposal=None,
            absent_code="waiting",
            absent_reason="choice_required",
        )
    else:
        _record_investigation_half(
            session,
            ticket,
            run,
            half="cancel_at_period_end",
            action_type="cancel_at_period_end",
            proposal=None,
            absent_code="not_executed",
            absent_reason="not_eligible",
        )
    reduce_combined_reply(session, ticket, run)


def _record_investigation_half(
    session: Session,
    ticket: Ticket,
    run: Run,
    *,
    half: str,
    action_type: str,
    proposal: Proposal | None,
    absent_code: str,
    absent_reason: str,
) -> None:
    effect = authoritative_effect(session, ticket, action_type)
    if effect is not None:
        _remember_effect(session, ticket, run, half, effect)
        return
    current = current_half(session, ticket.id, half)
    if current is not None and current.outcome_code in {"confirmed", "uncertain"}:
        return
    if proposal is not None:
        append_outcome(
            session,
            ticket,
            run,
            half=half,
            code="open",
            action_id=proposal.action_id,
            proposal_id=proposal.id,
        )
        return
    if (
        current is not None
        and current.outcome_code == absent_code
        and current.reason_code == absent_reason
    ):
        return
    append_outcome(
        session,
        ticket,
        run,
        half=half,
        code=absent_code,
        reason=absent_reason,
    )


def copy_forward(session: Session, ticket: Ticket) -> None:
    if not ticket_is_combined(session, ticket):
        return
    revision = latest_input_revision(session, ticket.id)
    run = _any_run(session, ticket)
    if run is None:
        return
    for half in HALVES:
        previous = session.scalar(
            select(CombinedHalf)
            .where(
                CombinedHalf.ticket_id == ticket.id,
                CombinedHalf.half == half,
                CombinedHalf.input_revision < revision,
            )
            .order_by(
                CombinedHalf.input_revision.desc(),
                CombinedHalf.outcome_version.desc(),
                CombinedHalf.id.desc(),
            )
            .limit(1)
        )
        if previous is None or current_half(session, ticket.id, half, revision) is not None:
            continue
        action_type = "refund" if half == "refund" else "cancel_at_period_end"
        effect = authoritative_effect(session, ticket, action_type)
        if effect is None and previous.recovery_job_id is not None:
            job = session.get(RecoveryJob, previous.recovery_job_id)
            if job is not None and job.admitted_at is not None and previous.action_id:
                effect = AuthoritativeEffect(
                    "uncertain",
                    previous.action_id,
                    previous.proposal_id,
                    job.id,
                )
        if effect is not None:
            _remember_effect(
                session,
                ticket,
                run,
                half,
                effect,
                prior_outcome_id=previous.id,
                input_revision=revision,
            )
        elif previous.outcome_code in {"confirmed", "uncertain"} and previous.action_id:
            append_outcome(
                session,
                ticket,
                run,
                half=half,
                code=previous.outcome_code,
                reason=previous.reason_code,
                action_id=previous.action_id,
                proposal_id=previous.proposal_id,
                recovery_job_id=previous.recovery_job_id,
                input_revision=revision,
                prior_outcome_id=previous.id,
            )
        else:
            append_outcome(
                session,
                ticket,
                run,
                half=half,
                code="not_executed",
                reason="input_changed",
                action_id=previous.action_id,
                input_revision=revision,
                prior_outcome_id=previous.id,
            )
    reduce_combined_reply(session, ticket, run)


def _any_run(session: Session, ticket: Ticket) -> Run | None:
    return session.scalar(
        select(Run).where(Run.ticket_id == ticket.id).order_by(Run.created_at.desc()).limit(1)
    )


def reduce_combined_reply(session: Session, ticket: Ticket, run: Run) -> ReplyDraft | None:
    _retire_unmatched_drafts(session, ticket)
    halves = current_halves(session, ticket.id)
    if set(halves) != set(HALVES):
        return None
    codes = {row.outcome_code for row in halves.values()}
    if codes & {"open", "in_flight"}:
        _set_summary(run, ticket, "awaiting_approval", "awaiting_approval")
        return None
    if "waiting" in codes and not (codes & {"confirmed", "uncertain"}):
        _set_summary(run, ticket, "awaiting_customer", "open")
        return None
    if codes <= {"not_executed", "not_dispatched"}:
        _set_summary(run, ticket, "completed", "open")
        return None
    if not ({"confirmed", "uncertain"} & codes):
        return None
    if not codes <= REPLY_READY:
        return None
    body = _body(session, ticket, halves)
    refund = halves["refund"]
    cancel = halves["cancel_at_period_end"]
    existing = _current_draft(session, ticket.id)
    if (
        existing is not None
        and existing[0].status == "draft"
        and existing[1].refund_outcome_id == refund.id
        and existing[1].cancellation_outcome_id == cancel.id
        and existing[0].body == body
    ):
        return existing[0]
    _supersede_unsent(session, ticket.id)
    draft = _new_draft(session, ticket, run, body)
    session.add(
        CombinedReply(
            tenant_id=ticket.tenant_id,
            ticket_id=ticket.id,
            run_id=run.id,
            reply_draft_id=draft.id,
            input_revision=latest_input_revision(session, ticket.id),
            refund_outcome_id=refund.id,
            cancellation_outcome_id=cancel.id,
            created_at=utcnow(),
        )
    )
    _set_summary(run, ticket, "awaiting_reply_review", "awaiting_reply")
    session.flush()
    return draft


def _set_summary(run: Run, ticket: Ticket, run_state: str, ticket_status: str) -> None:
    """A cancelled investigation stays cancelled. The ticket summary still moves."""
    if run.state != "cancelled":
        run.state = run_state
    ticket.status = ticket_status
    ticket.updated_at = utcnow()


def lock_current_halves(session: Session, ticket: Ticket) -> dict[str, CombinedHalf]:
    """Read the current halves in refund, then cancellation, order.

    The caller already holds the ticket lock. These rows are immutable, so the
    read does not take a row lock that the API role is not granted.
    """
    revision = latest_input_revision(session, ticket.id)
    locked: dict[str, CombinedHalf] = {}
    for half in HALVES:
        row = session.scalar(
            select(CombinedHalf)
            .where(
                CombinedHalf.ticket_id == ticket.id,
                CombinedHalf.half == half,
                CombinedHalf.input_revision == revision,
            )
            .order_by(CombinedHalf.outcome_version.desc(), CombinedHalf.id.desc())
            .limit(1)
        )
        if row is not None:
            locked[half] = row
    return locked


def _retire_unmatched_drafts(session: Session, ticket: Ticket) -> None:
    session.flush()
    rows = session.scalars(
        select(ReplyDraft).where(ReplyDraft.ticket_id == ticket.id, ReplyDraft.status == "draft")
    ).all()
    for draft in rows:
        if draft.status != "draft":
            continue
        binding = combined_reply_for(session, draft.id)
        if binding is not None and not reply_matches_current(session, ticket, binding):
            draft.status = "superseded"


def combined_reply_for(session: Session, reply_id: UUID) -> CombinedReply | None:
    return session.scalar(select(CombinedReply).where(CombinedReply.reply_draft_id == reply_id))


def reply_matches_current(session: Session, ticket: Ticket, binding: CombinedReply) -> bool:
    halves = current_halves(session, ticket.id)
    if set(halves) != set(HALVES):
        return False
    return (
        binding.input_revision == latest_input_revision(session, ticket.id)
        and binding.refund_outcome_id == halves["refund"].id
        and binding.cancellation_outcome_id == halves["cancel_at_period_end"].id
    )


def _body(session: Session, ticket: Ticket, halves: dict[str, CombinedHalf]) -> str:
    lines = [f"Hello {ticket.customer_label},", ""]
    for half in HALVES:
        row = halves[half]
        label = "refund" if half == "refund" else "cancellation"
        if row.outcome_code == "confirmed":
            lines.append(_confirmed_section(session, ticket, row))
        elif row.outcome_code == "uncertain":
            lines.append(
                f"The {label} {row.action_id} is not confirmed. It is still being checked."
            )
        elif row.outcome_code == "not_dispatched":
            lines.append(
                f"The {label} attempt was not sent because the account read was unavailable. "
                "This is not a decision that the request is ineligible."
            )
        elif row.outcome_code == "not_executed":
            lines.append(f"The {label} action was not executed ({row.reason_code}).")
        elif row.outcome_code == "waiting":
            lines.append("A period-end choice is still required. No cancellation was submitted.")
    return "\n".join(lines) + "\n"


def _confirmed_section(session: Session, ticket: Ticket, row: CombinedHalf) -> str:
    """Quote the verified ledger receipt. An action id alone is not a receipt."""
    from resolveai.domain.money import format_usd

    ledger = None
    if row.action_id:
        ledger = session.scalar(
            select(ActionLedger)
            .where(
                ActionLedger.tenant_id == ticket.tenant_id,
                ActionLedger.action_id == row.action_id,
                ActionLedger.status == "confirmed",
            )
            .limit(1)
        )
    result = ledger.result if ledger is not None and isinstance(ledger.result, dict) else {}
    operation_id = ""
    if ledger is not None and ledger.operation_id:
        operation_id = ledger.operation_id
    elif result.get("operation_id"):
        operation_id = str(result.get("operation_id"))
    if row.half == "refund":
        amount = result.get("amount_cents")
        if amount is None and row.proposal_id is not None:
            proposal = session.get(Proposal, row.proposal_id)
            amount = None if proposal is None else proposal.amount_cents
        shown = format_usd(int(amount or 0))
        payment = result.get("payment_id") or ""
        event = result.get("event_id") or ""
        return (
            f"We confirmed a refund of {shown} USD for the duplicate payment.\n"
            f"Refund reference: {operation_id}.\n"
            f"Payment {payment}, event {event}."
        )
    effective = str(result.get("effective_at") or "")
    return (
        f"Cancellation scheduled for {effective}. Access continues until then.\n"
        f"Reference: {operation_id}."
    )


def _current_draft(session: Session, ticket_id: UUID) -> tuple[ReplyDraft, CombinedReply] | None:
    binding = session.scalar(
        select(CombinedReply)
        .where(CombinedReply.ticket_id == ticket_id)
        .order_by(CombinedReply.created_at.desc())
        .limit(1)
    )
    if binding is None:
        return None
    draft = session.get(ReplyDraft, binding.reply_draft_id)
    if draft is None:
        return None
    return draft, binding


def _supersede_unsent(session: Session, ticket_id: UUID) -> None:
    session.flush()
    rows = session.scalars(
        select(ReplyDraft).where(ReplyDraft.ticket_id == ticket_id, ReplyDraft.status == "draft")
    ).all()
    for draft in rows:
        if draft.status != "draft":
            continue
        if combined_reply_for(session, draft.id) is not None:
            draft.status = "superseded"


def _new_draft(session: Session, ticket: Ticket, run: Run, body: str) -> ReplyDraft:
    from resolveai.services.replies import body_hash

    drafts = session.scalars(
        select(ReplyDraft).where(ReplyDraft.ticket_id == ticket.id).order_by(ReplyDraft.revision)
    ).all()
    revision = (drafts[-1].revision if drafts else 0) + 1
    created = ReplyDraft(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        run_id=run.id,
        revision=revision,
        recipient=ticket.customer_email,
        body=body,
        body_hash=body_hash(body),
        status="draft",
        send_action_id=f"reply:{ticket.id}:{revision}",
        receipt=None,
        created_at=utcnow(),
    )
    session.add(created)
    session.flush()
    audit(
        session,
        tenant_id=ticket.tenant_id,
        event_type="reply_drafted",
        actor_user_id=None,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"reply_id": str(created.id), "revision": revision, "combined": True},
    )
    return created


def same_input_read_retry(session: Session, ticket: Ticket, job: RecoveryJob) -> bool:
    return (
        ticket_is_combined(session, ticket)
        and job.state == "blocked"
        and job.admitted_at is None
        and job.block_reason == "unavailable"
        and job.input_revision == latest_input_revision(session, ticket.id)
    )
