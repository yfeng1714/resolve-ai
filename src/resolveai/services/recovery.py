"""PostgreSQL recovery jobs for refund execution.

The API enqueues and cancels. Only the worker admits, posts, and settles.
A worker write names one job and rolls back unless that owner still holds an
unexpired lease. API cancellation does not use the lease.
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import (
    ActionLedger,
    Approval,
    Proposal,
    RecoveryJob,
    Run,
    Ticket,
)
from resolveai.errors import APIError
from resolveai.services.audit import audit
from resolveai.services.execution import (
    _accept_verified,
    _proposal_matches_live_state,
)
from resolveai.services.input import latest_input_revision

ACTIVE = ("queued", "leased", "blocked")
LEASE = timedelta(seconds=30)
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
holds: dict[str, Any] = {}


class OwnershipLost(RuntimeError):
    """The worker no longer holds an unexpired lease for this job."""


class GrantInvalid(RuntimeError):
    """The bound grant expired or was revoked before admission committed."""

    def __init__(self, tenant_id: UUID, approval_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.approval_id = approval_id
        super().__init__("approval grant is not valid")


class ServiceActor:
    def __init__(self, tenant_id: UUID, user_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.role = "recovery"


def hold(name: str) -> None:
    """Deterministic crash seam. A hook, an exit, or a file gate."""
    hook = holds.get(name)
    if hook is not None:
        hook()
    if os.environ.get("RESOLVEAI_RECOVERY_EXIT_AFTER") == name:
        os._exit(0)
    if os.environ.get("RESOLVEAI_RECOVERY_HOLD") != name:
        return
    ready = Path(os.environ["RESOLVEAI_RECOVERY_READY"])
    release = Path(os.environ["RESOLVEAI_RECOVERY_RELEASE"])
    ready.write_text(name, encoding="utf-8")
    while not release.exists():
        time.sleep(0.05)


def enqueue_refund(
    session: Session,
    *,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger | None,
    test_fault: str | None,
    model_name: str | None = None,
) -> RecoveryJob:
    """Commit A is the caller's commit. This function only stages the job."""
    from resolveai.services.grants import current_grant

    approval = current_grant(session, proposal)
    if approval is None:
        raise APIError(409, "approval_expired", "Execution requires a valid grant")
    from resolveai.services.tenancy import require_bound_account

    require_bound_account(session, ticket)
    if ledger is not None:
        for existing in _active_jobs(session, ledger.id):
            if existing.admitted_at is not None:
                raise APIError(409, "unknown", "This refund dispatch is already admitted")
            if existing.proposal_id == proposal.id and existing.approval_id == approval.id:
                from resolveai.services.combined import same_input_read_retry

                if same_input_read_retry(session, ticket, existing):
                    continue
                return existing
            existing.state = "cancelled"
            existing.block_reason = "superseded"
            existing.owner_token = None
            existing.updated_at = utcnow()
    now = utcnow()
    if ledger is None:
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
    else:
        ledger.proposal_id = proposal.id
        ledger.payload_hash = proposal.payload_hash
        ledger.status = "pending"
        ledger.dispatch_state = "reserved"
        ledger.error_code = None
        ledger.updated_at = now
    generation = _next_generation(session, ledger.id)
    command = {
        "account_id": str(ticket.account_id),
        "payment_id": str(proposal.target_payment_id),
        "event_id": proposal.target_event_id,
        "amount_cents": proposal.amount_cents,
        "currency": "USD",
        "action_id": proposal.action_id,
        "payload_hash": proposal.payload_hash,
        "expected_payment_version": int(proposal.preconditions["payment_resource_version"]),
        "policy_version": proposal.policy_version,
    }
    if test_fault:
        command["test_fault"] = test_fault
    job = RecoveryJob(
        tenant_id=actor.tenant_id,
        ledger_id=ledger.id,
        command_generation=generation,
        proposal_id=proposal.id,
        proposal_revision=proposal.revision,
        run_id=run.id,
        approval_id=approval.id,
        authorizer_user_id=approval.actor_user_id,
        payload_hash=proposal.payload_hash,
        input_revision=int(proposal.preconditions["input_revision"]),
        payment_resource_version=int(proposal.preconditions["payment_resource_version"]),
        policy_version=proposal.policy_version,
        command=command,
        config_snapshot={
            "provider": run.provider,
            "model_name": model_name or None,
            "prompt_version": run.prompt_version,
        },
        state="queued",
        owner_token=None,
        owner_generation=0,
        leased_until=None,
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now,
        admitted_at=None,
        last_error_code=None,
        block_reason=None,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    _note_combined_enqueue(session, ticket, run, proposal, job)
    return job


def _same_unsent_attempt(
    job: RecoveryJob, proposal: Proposal, approval: Any, command: dict[str, Any]
) -> bool:
    """An identical retry keeps the queued or leased job, including its owner."""
    return (
        job.admitted_at is None
        and job.proposal_id == proposal.id
        and job.approval_id == approval.id
        and job.payload_hash == proposal.payload_hash == command["attempt_hash"]
        and job.subscription_resource_version == command["expected_resource_version"]
        and job.input_revision == int(proposal.preconditions["input_revision"])
        and job.policy_version == proposal.policy_version
        and dict(job.command) == command
    )


def _cancellation_command_is_bound(
    job: RecoveryJob, proposal: Proposal, ticket: Ticket, ledger: ActionLedger
) -> bool:
    """Recompute the approved attempt. A stored hash label is not enough."""
    from resolveai.domain.cancellation import bound_command

    fields = proposal.preconditions or {}
    try:
        version = int(fields["subscription_resource_version"])
        expected = bound_command(
            action_id=proposal.action_id,
            account_id=str(ticket.account_id),
            subscription_id=str(proposal.target_subscription_id),
            effective_at=str(fields["effective_at"]),
            policy_version=str(proposal.policy_version),
            expected_resource_version=version,
        )
    except (KeyError, TypeError, ValueError):
        return False
    return (
        dict(job.command) == expected
        and job.payload_hash == proposal.payload_hash == expected["attempt_hash"]
        and job.subscription_resource_version == version
        and job.policy_version == proposal.policy_version
        and job.input_revision == int(fields["input_revision"])
        and ledger.action_type == "cancel_at_period_end"
        and ledger.payload_hash == expected["effect_hash"]
        and fields.get("effect_hash") == expected["effect_hash"]
    )


def enqueue_cancellation(
    session: Session,
    *,
    actor: Any,
    proposal: Proposal,
    ticket: Ticket,
    run: Run,
    ledger: ActionLedger | None,
    model_name: str | None,
) -> RecoveryJob:
    """Reserve or rebind the unsent period-end effect, then queue a new attempt."""
    from resolveai.services.grants import action_is_admitted, current_grant
    from resolveai.services.tenancy import require_bound_account

    approval = current_grant(session, proposal)
    if approval is None:
        raise APIError(409, "approval_expired", "Execution requires a valid grant")
    require_bound_account(session, ticket)
    from resolveai.domain.cancellation import bound_command

    effect_fields = proposal.preconditions or {}
    version = int(effect_fields["subscription_resource_version"])
    command = bound_command(
        action_id=proposal.action_id,
        account_id=str(ticket.account_id),
        subscription_id=str(proposal.target_subscription_id),
        effective_at=str(effect_fields["effective_at"]),
        policy_version=proposal.policy_version,
        expected_resource_version=version,
    )
    effect = str(command["effect_hash"])
    hash_matches = proposal.payload_hash == command["attempt_hash"]
    if not hash_matches or effect_fields.get("effect_hash") != effect:
        raise APIError(409, "payload_conflict", "The approved attempt does not match its fields")
    if action_is_admitted(session, proposal):
        raise APIError(
            409,
            "period_already_dispatched",
            "This period was already dispatched and cannot be replaced",
        )
    now = utcnow()
    if ledger is None:
        ledger = ActionLedger(
            tenant_id=actor.tenant_id,
            action_id=proposal.action_id,
            action_type="cancel_at_period_end",
            proposal_id=proposal.id,
            reply_draft_id=None,
            payload_hash=effect,
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
    elif ledger.payload_hash != effect or ledger.action_type != "cancel_at_period_end":
        raise APIError(409, "payload_conflict", "This period was reserved with a different effect")
    else:
        active = _active_jobs(session, ledger.id)
        for existing in active:
            if existing.admitted_at is not None:
                raise APIError(
                    409,
                    "period_already_dispatched",
                    "This period was already dispatched",
                )
            if _same_unsent_attempt(existing, proposal, approval, command):
                from resolveai.services.combined import same_input_read_retry

                if same_input_read_retry(session, ticket, existing):
                    continue
                return existing
        for existing in active:
            from resolveai.services.combined import same_input_read_retry

            if same_input_read_retry(session, ticket, existing):
                continue
            existing.state = "cancelled"
            existing.block_reason = "superseded"
            existing.owner_token = None
            existing.updated_at = now
        ledger.proposal_id = proposal.id
        ledger.status = "pending"
        ledger.dispatch_state = "reserved"
        ledger.error_code = None
        ledger.updated_at = now
    job = RecoveryJob(
        tenant_id=actor.tenant_id,
        ledger_id=ledger.id,
        command_generation=_next_generation(session, ledger.id),
        proposal_id=proposal.id,
        proposal_revision=proposal.revision,
        run_id=run.id,
        approval_id=approval.id,
        authorizer_user_id=approval.actor_user_id,
        payload_hash=proposal.payload_hash,
        input_revision=int(proposal.preconditions["input_revision"]),
        payment_resource_version=None,
        subscription_resource_version=version,
        policy_version=proposal.policy_version,
        command=command,
        config_snapshot={
            "provider": run.provider,
            "model_name": model_name or None,
            "prompt_version": run.prompt_version,
        },
        state="queued",
        owner_token=None,
        owner_generation=0,
        leased_until=None,
        attempt_count=0,
        max_attempts=5,
        next_attempt_at=now,
        admitted_at=None,
        last_error_code=None,
        block_reason=None,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    _note_combined_enqueue(session, ticket, run, proposal, job)
    return job


def cancel_pre_admission(session: Session, ticket: Ticket) -> None:
    """Authenticated cancellation. The caller already holds the ticket lock."""
    jobs = session.scalars(
        select(RecoveryJob)
        .join(Proposal, Proposal.id == RecoveryJob.proposal_id)
        .where(
            Proposal.ticket_id == ticket.id,
            RecoveryJob.admitted_at.is_(None),
            RecoveryJob.state.in_(ACTIVE),
        )
        .with_for_update()
    ).all()
    now = utcnow()
    for job in jobs:
        session.refresh(job)
        if job.admitted_at is not None or job.state not in ACTIVE:
            continue
        job.state = "cancelled"
        job.owner_token = None
        job.block_reason = "withdrawn"
        job.updated_at = now


def requeue_queries(session: Session, *, actor: Any, job_id: UUID) -> RecoveryJob:
    if actor.role != "approver":
        raise APIError(403, "denied", "Only an approver can requeue recovery")
    job, _proposal, _ticket, _run, _ledger = _lock_recovery_graph(
        session, job_id, actor.tenant_id
    )
    if job.state != "blocked" or job.block_reason != "delivery_uncertain":
        raise APIError(409, "stale_proposal", "Only a delivery-uncertain job can be requeued")
    if job.admitted_at is None:
        raise APIError(409, "stale_proposal", "Requeue does not grant a new dispatch")
    job.state = "queued"
    job.owner_token = None
    job.attempt_count = 0
    job.last_error_code = None
    job.next_attempt_at = utcnow()
    job.updated_at = utcnow()
    audit(
        session,
        tenant_id=actor.tenant_id,
        event_type="recovery_requeued",
        actor_user_id=actor.user_id,
        payload={"job_id": str(job.id), "post_authorized": False},
    )
    return job


def consistency_report(session: Session, *, tenant_id: UUID, job_id: UUID) -> dict[str, Any]:
    """Explicit repair check. The claim loop does not select settled jobs."""
    job = session.get(RecoveryJob, job_id)
    if job is None or job.tenant_id != tenant_id:
        raise APIError(404, "not_found", "Recovery job was not found")
    ledger = session.get(ActionLedger, job.ledger_id)
    confirmed = ledger is not None and ledger.status == "confirmed"
    if job.state == "settled" and not confirmed:
        return {"consistent": False, "reason": "inconsistent_recovery"}
    return {"consistent": True, "reason": None}


def process_available(session: Session, tools: Any) -> bool:
    claimed = _claim(session)
    if claimed is None:
        return False
    job_id, token, generation = claimed
    try:
        job = session.get(RecoveryJob, job_id)
        if job is None:
            return True
        if dict(job.command).get("action") == "cancel_at_period_end":
            _process_cancellation(session, tools, job_id, token, generation)
            return True
        if job.admitted_at is None:
            if not _admit(session, tools, job_id, token, generation):
                return True
            hold("after_admission")
            hold("before_post")
            job = session.get(RecoveryJob, job_id)
            if job is None:
                return True
            command = dict(job.command)
            from resolveai.services.tenancy import require_bound_account

            proposal_row = session.get(Proposal, job.proposal_id)
            posted_ticket = (
                None if proposal_row is None else session.get(Ticket, proposal_row.ticket_id)
            )
            if posted_ticket is None or str(posted_ticket.account_id) != str(
                command.get("account_id")
            ):
                raise APIError(
                    409,
                    "account_not_bound",
                    "The ticket account is not bound to this tenant",
                )
            require_bound_account(session, posted_ticket)
            tools.create_refund(
                account_id=UUID(command["account_id"]),
                payment_id=UUID(command["payment_id"]),
                event_id=str(command["event_id"]),
                amount_cents=int(command["amount_cents"]),
                action_id=str(command["action_id"]),
                payload_hash=str(command["payload_hash"]),
                expected_payment_version=int(command["expected_payment_version"]),
                policy_version=str(command["policy_version"]),
                fault=command.get("test_fault"),
            )
            hold("before_local_confirm")
        _settle_or_retry(session, tools, job_id, token, generation)
    except GrantInvalid as exc:
        session.rollback()
        from resolveai.services.grants import release_invalid_grant

        release_invalid_grant(
            session,
            tenant_id=exc.tenant_id,
            approval_id=exc.approval_id,
            commit=True,
        )
    except OwnershipLost:
        session.rollback()
    except APIError as exc:
        session.rollback()
        if not _retryable_outage(exc):
            raise
        _defer_pre_admission(session, job_id, token, generation, exc.error_code)
    return True


def _retryable_outage(exc: APIError) -> bool:
    return bool(exc.retryable or exc.error_code == "unavailable")


def _owns(session: Session, job_id: UUID, token: UUID, generation: int) -> bool:
    row = session.execute(
        text(
            """
            SELECT 1 FROM app.recovery_job
            WHERE id = :id
              AND owner_token = :token
              AND owner_generation = :generation
              AND leased_until > clock_timestamp()
            """
        ),
        {"id": job_id, "token": token, "generation": generation},
    ).one_or_none()
    return row is not None


def _require_owner(session: Session, job_id: UUID, token: UUID, generation: int) -> None:
    if not _owns(session, job_id, token, generation):
        session.rollback()
        raise OwnershipLost()


def _commit_owned(
    session: Session,
    job: RecoveryJob,
    token: UUID,
    generation: int,
    *,
    release: bool,
) -> None:
    """Flush staged work, then commit only while this lease still owns the job.

    ``autoflush`` is off, so a SELECT cannot fence writes that flush later.
    A lock wait inside the flush can outlast the lease. The conditional update
    below is the authoritative write; a miss rolls back the flushed work too.
    """
    if job.owner_token != token or job.owner_generation != generation:
        session.rollback()
        raise OwnershipLost()
    session.flush()
    statement = """
        UPDATE app.recovery_job
        SET updated_at = clock_timestamp()
        WHERE id = :id
          AND owner_token = :token
          AND owner_generation = :generation
          AND leased_until > clock_timestamp()
        RETURNING id
    """
    if release:
        statement = """
            UPDATE app.recovery_job
            SET owner_token = NULL,
                updated_at = clock_timestamp()
            WHERE id = :id
              AND owner_token = :token
              AND owner_generation = :generation
              AND leased_until > clock_timestamp()
            RETURNING id
        """
    owned = session.execute(
        text(statement),
        {"id": job.id, "token": token, "generation": generation},
    ).one_or_none()
    if owned is None:
        session.rollback()
        raise OwnershipLost()
    if release:
        session.expire(job, ["owner_token"])
    session.commit()


def _commit_admission(
    session: Session, job: RecoveryJob, token: UUID, generation: int
) -> None:
    """Flush, then admit only while the lease and the bound grant are both live."""
    if job.owner_token != token or job.owner_generation != generation:
        session.rollback()
        raise OwnershipLost()
    session.flush()
    owned = session.execute(
        text(
            """
            UPDATE app.recovery_job AS job
            SET admitted_at = clock_timestamp(),
                updated_at = clock_timestamp()
            FROM app.approval AS approval_grant
            WHERE job.id = :id
              AND job.owner_token = :token
              AND job.owner_generation = :generation
              AND job.leased_until > clock_timestamp()
              AND job.approval_id = :approval_id
              AND approval_grant.id = job.approval_id
              AND approval_grant.tenant_id = job.tenant_id
              AND approval_grant.decision = 'approved'
              AND approval_grant.revision = :revision
              AND approval_grant.payload_hash = :payload_hash
              AND approval_grant.expires_at > clock_timestamp()
              AND NOT EXISTS (
                  SELECT 1 FROM app.approval_revocation AS revocation
                  WHERE revocation.approval_id = approval_grant.id
              )
            RETURNING job.id
            """
        ),
        {
            "id": job.id,
            "token": token,
            "generation": generation,
            "approval_id": job.approval_id,
            "revision": job.proposal_revision,
            "payload_hash": job.payload_hash,
        },
    ).one_or_none()
    if owned is None:
        grant_live = session.scalar(
            text(
                """
                SELECT approval_grant.decision = 'approved'
                   AND approval_grant.revision = :revision
                   AND approval_grant.payload_hash = :payload_hash
                   AND approval_grant.expires_at > clock_timestamp()
                   AND NOT EXISTS (
                       SELECT 1 FROM app.approval_revocation AS revocation
                       WHERE revocation.approval_id = approval_grant.id
                   )
                FROM app.approval AS approval_grant
                WHERE approval_grant.id = :approval_id
                  AND approval_grant.tenant_id = :tenant_id
                """
            ),
            {
                "approval_id": job.approval_id,
                "tenant_id": job.tenant_id,
                "revision": job.proposal_revision,
                "payload_hash": job.payload_hash,
            },
        )
        tenant_id = job.tenant_id
        approval_id = job.approval_id
        session.rollback()
        if not grant_live:
            raise GrantInvalid(tenant_id, approval_id)
        raise OwnershipLost()
    session.expire(job, ["admitted_at"])
    session.commit()


def _claim(session: Session) -> tuple[UUID, UUID, int] | None:
    row = session.execute(text("SELECT * FROM app.claim_recovery_job()")).one_or_none()
    session.commit()
    if row is None:
        return None
    session.info["tenant_id"] = str(row.tenant_id)
    return row.job_id, row.owner_token, int(row.owner_generation)


def _admit(session: Session, tools: Any, job_id: UUID, token: UUID, generation: int) -> bool:
    job, proposal, ticket, run, ledger = _fence(session, job_id, token, generation)
    if job.admitted_at is not None:
        session.rollback()
        return False
    from resolveai.services.tenancy import account_is_bound

    command = dict(job.command)
    if str(ticket.account_id) != str(command.get("account_id")) or not account_is_bound(
        session, ticket.tenant_id, ticket.account_id, ticket.customer_id
    ):
        job.state = "blocked"
        job.block_reason = "account_not_bound"
        job.updated_at = utcnow()
        _commit_owned(session, job, token, generation, release=True)
        return False
    approval = session.get(Approval, job.approval_id)
    current_input = latest_input_revision(session, ticket.id)
    try:
        live_ok = _proposal_matches_live_state(tools, ticket, proposal)
    except APIError as exc:
        session.rollback()
        if _retryable_outage(exc):
            _defer_pre_admission(session, job_id, token, generation, exc.error_code)
            return False
        raise
    _require_owner(session, job_id, token, generation)
    authorized = (
        approval is not None
        and approval.decision == "approved"
        and approval.proposal_id == proposal.id
        and approval.revision == proposal.revision
        and approval.payload_hash == proposal.payload_hash
        and proposal.status == "approved"
        and proposal.revision == job.proposal_revision
        and proposal.payload_hash == job.payload_hash
        and run.state != "cancelled"
        and current_input == job.input_revision
        and ledger.dispatch_state == "reserved"
        and ledger.payload_hash == job.payload_hash
    )
    if not authorized or not live_ok:
        job.state = "blocked"
        job.block_reason = "authorization_stale"
        job.updated_at = utcnow()
        _commit_owned(session, job, token, generation, release=True)
        return False
    now = utcnow()
    job.admitted_at = now
    job.updated_at = now
    ledger.dispatch_state = "dispatched"
    ledger.status = "pending"
    ledger.updated_at = now
    run.state = "executing"
    run.updated_at = now
    audit(
        session,
        tenant_id=job.tenant_id,
        event_type="action_dispatched",
        actor_user_id=job.authorizer_user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"action_id": ledger.action_id, "job_id": str(job.id)},
    )
    _commit_admission(session, job, token, generation)
    return True


def _defer_pre_admission(
    session: Session, job_id: UUID, token: UUID, generation: int, code: str
) -> None:
    """Retry or block before admission. This does not authorize a POST."""
    try:
        job, _proposal, _ticket, _run, _ledger = _fence(session, job_id, token, generation)
    except OwnershipLost:
        session.rollback()
        return
    if job.admitted_at is not None:
        session.rollback()
        return
    job.attempt_count += 1
    job.last_error_code = code
    job.updated_at = utcnow()
    if job.attempt_count >= job.max_attempts:
        job.state = "blocked"
        job.block_reason = "unavailable"
        _note_combined_job(session, job)
    else:
        delay = BACKOFF_SECONDS[min(job.attempt_count - 1, len(BACKOFF_SECONDS) - 1)]
        job.state = "queued"
        job.next_attempt_at = utcnow() + timedelta(seconds=delay)
    try:
        _commit_owned(session, job, token, generation, release=True)
    except OwnershipLost:
        return


def _settle_or_retry(
    session: Session, tools: Any, job_id: UUID, token: UUID, generation: int
) -> None:
    job, proposal, ticket, run, ledger = _fence(session, job_id, token, generation)
    if ledger.status == "confirmed":
        job.state = "settled"
        job.updated_at = utcnow()
        _commit_owned(session, job, token, generation, release=True)
        return
    actor = ServiceActor(job.tenant_id, job.authorizer_user_id)
    from resolveai.services.execution import _verified_receipt
    from resolveai.services.tenancy import require_bound_account

    require_bound_account(session, ticket)
    kind, receipt = _verified_receipt(tools, ticket, proposal)
    _require_owner(session, job_id, token, generation)
    if kind == "ok" and receipt is not None:
        uncertain = job.last_error_code not in (None, "")
        _accept_verified(
            session,
            actor,
            proposal,
            ticket,
            run,
            ledger,
            tools,
            recovered=uncertain or bool(job.command.get("test_fault")),
            commit=False,
            prepared=(kind, receipt),
        )
        if ledger.status != "confirmed":
            session.rollback()
            return
        job.state = "settled"
        job.block_reason = None
        job.updated_at = utcnow()
        _commit_owned(session, job, token, generation, release=True)
        return
    if kind == "conflict":
        job.state = "blocked"
        job.block_reason = "conflict"
        job.last_error_code = "conflict"
        job.updated_at = utcnow()
        from resolveai.services.execution import _mark_uncertain

        _mark_uncertain(session, actor, proposal, ticket, run, ledger, commit=False)
        _commit_owned(session, job, token, generation, release=True)
        return
    job.attempt_count += 1
    job.last_error_code = "not_found" if kind == "missing" else "unavailable"
    job.updated_at = utcnow()
    if job.attempt_count >= job.max_attempts:
        job.state = "blocked"
        job.block_reason = "delivery_uncertain"
        from resolveai.services.execution import _mark_uncertain

        _mark_uncertain(session, actor, proposal, ticket, run, ledger, commit=False)
    else:
        delay = BACKOFF_SECONDS[min(job.attempt_count - 1, len(BACKOFF_SECONDS) - 1)]
        job.state = "queued"
        job.next_attempt_at = utcnow() + timedelta(seconds=delay)
    _commit_owned(session, job, token, generation, release=True)


def _fence(
    session: Session, job_id: UUID, token: UUID, generation: int
) -> tuple[RecoveryJob, Proposal, Ticket, Run, ActionLedger]:
    preview = session.get(RecoveryJob, job_id)
    if preview is None:
        raise OwnershipLost()
    proposal = session.get(Proposal, preview.proposal_id)
    if proposal is None:
        raise OwnershipLost()
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == proposal.ticket_id, Ticket.tenant_id == preview.tenant_id)
        .with_for_update()
    )
    proposal = session.scalar(
        select(Proposal)
        .where(Proposal.id == proposal.id, Proposal.tenant_id == preview.tenant_id)
        .with_for_update()
    )
    approval = session.scalar(
        select(Approval)
        .where(Approval.id == preview.approval_id, Approval.tenant_id == preview.tenant_id)
        .with_for_update()
    )
    run = session.scalar(
        select(Run)
        .where(Run.id == preview.run_id, Run.tenant_id == preview.tenant_id)
        .with_for_update()
    )
    ledger = session.scalar(
        select(ActionLedger)
        .where(ActionLedger.id == preview.ledger_id, ActionLedger.tenant_id == preview.tenant_id)
        .with_for_update()
    )
    owned = session.execute(
        text(
            """
            SELECT id FROM app.recovery_job
            WHERE id = :id
              AND owner_token = :token
              AND owner_generation = :generation
              AND leased_until > clock_timestamp()
            FOR UPDATE
            """
        ),
        {"id": job_id, "token": token, "generation": generation},
    ).one_or_none()
    job = session.get(RecoveryJob, job_id)
    if (
        owned is None
        or job is None
        or ticket is None
        or proposal is None
        or approval is None
        or run is None
        or ledger is None
    ):
        raise OwnershipLost()
    for row in (ticket, proposal, approval, run, ledger, job):
        session.refresh(row)
    return job, proposal, ticket, run, ledger


def _active_jobs(session: Session, ledger_id: UUID) -> list[RecoveryJob]:
    jobs = list(
        session.scalars(
            select(RecoveryJob)
            .where(RecoveryJob.ledger_id == ledger_id, RecoveryJob.state.in_(ACTIVE))
            .with_for_update()
        ).all()
    )
    for job in jobs:
        session.refresh(job)
    return jobs


def _next_generation(session: Session, ledger_id: UUID) -> int:
    current = session.scalar(
        select(RecoveryJob.command_generation)
        .where(RecoveryJob.ledger_id == ledger_id)
        .order_by(RecoveryJob.command_generation.desc())
    )
    return int(current or 0) + 1


def _lock_recovery_graph(
    session: Session, job_id: UUID, tenant_id: UUID
) -> tuple[RecoveryJob, Proposal, Ticket, Run, ActionLedger]:
    """Lock ticket, proposal, run, ledger, then job.

    The unlocked lookup reads identifiers only. A later ``FOR UPDATE`` does not
    refresh an object already stored in the identity map, so every locked row is
    reloaded before its mutable state is used.
    """
    identified = session.execute(
        select(RecoveryJob.proposal_id, RecoveryJob.run_id, RecoveryJob.ledger_id).where(
            RecoveryJob.id == job_id,
            RecoveryJob.tenant_id == tenant_id,
        )
    ).one_or_none()
    if identified is None:
        raise APIError(404, "not_found", "Recovery job was not found")
    proposal_id, run_id, ledger_id = identified
    ticket_id = session.scalar(
        select(Proposal.ticket_id).where(
            Proposal.id == proposal_id,
            Proposal.tenant_id == tenant_id,
        )
    )
    ticket = session.scalar(
        select(Ticket)
        .where(Ticket.id == ticket_id, Ticket.tenant_id == tenant_id)
        .with_for_update()
    )
    proposal = session.scalar(
        select(Proposal)
        .where(Proposal.id == proposal_id, Proposal.tenant_id == tenant_id)
        .with_for_update()
    )
    run = session.scalar(
        select(Run).where(Run.id == run_id, Run.tenant_id == tenant_id).with_for_update()
    )
    ledger = session.scalar(
        select(ActionLedger)
        .where(ActionLedger.id == ledger_id, ActionLedger.tenant_id == tenant_id)
        .with_for_update()
    )
    job = session.scalar(
        select(RecoveryJob)
        .where(RecoveryJob.id == job_id, RecoveryJob.tenant_id == tenant_id)
        .with_for_update()
    )
    if ticket is None or proposal is None or run is None or ledger is None or job is None:
        raise APIError(404, "not_found", "Recovery job was not found")
    for row in (ticket, proposal, run, ledger, job):
        session.refresh(row)
    return job, proposal, ticket, run, ledger


def _process_cancellation(
    session: Session, tools: Any, job_id: UUID, token: UUID, generation: int
) -> None:
    """Admit one period-end command, then only query it."""
    job, proposal, ticket, _run, ledger = _fence(session, job_id, token, generation)
    command = dict(job.command)
    if job.admitted_at is None:
        from resolveai.domain.cancellation import canonical_instant
        from resolveai.services.cancellation import subscription_views
        from resolveai.services.input import latest_input_revision
        from resolveai.services.tenancy import require_bound_account

        require_bound_account(session, ticket)
        if not _cancellation_command_is_bound(job, proposal, ticket, ledger):
            _block_cancellation(session, job, token, generation, "authorization_stale")
            return
        if latest_input_revision(session, ticket.id) != job.input_revision:
            _block_cancellation(session, job, token, generation, "authorization_stale")
            return
        read = tools.list_subscriptions(ticket.account_id)
        if read.status != "ok":
            session.rollback()
            _defer_pre_admission(
                session, job_id, token, generation, read.error_code or "unavailable"
            )
            return
        viewed = next(
            (
                row
                for row in subscription_views(read.data)
                if row.id == command.get("subscription_id")
            ),
            None,
        )
        now = canonical_instant(utcnow())
        approved_version = int(job.subscription_resource_version or 0)
        if (
            viewed is None
            or viewed.resource_version != approved_version
            or int(command["expected_resource_version"]) != approved_version
        ):
            _block_cancellation(session, job, token, generation, "version_conflict")
            return
        if (
            viewed.current_period_end != command.get("effective_at")
            or viewed.current_period_end <= now
        ):
            _block_cancellation(session, job, token, generation, "period_elapsed")
            return
        if viewed.cancel_at_period_end:
            _block_cancellation(session, job, token, generation, "already_scheduled")
            return
        job.admitted_at = utcnow()
        ledger.dispatch_state = "dispatched"
        ledger.status = "pending"
        ledger.updated_at = utcnow()
        _commit_admission(session, job, token, generation)
        hold("after_admission")
        hold("before_post")
        job = session.get(RecoveryJob, job_id)
        if job is None:
            return
        command = dict(job.command)
        posted = tools.schedule_period_end(
            account_id=ticket.account_id,
            subscription_id=proposal.target_subscription_id,
            effective_at=str(command["effective_at"]),
            action_id=str(command["action_id"]),
            effect_hash=str(command["effect_hash"]),
            expected_resource_version=int(command["expected_resource_version"]),
            policy_version=str(command["policy_version"]),
        )
        hold("before_local_confirm")
        if posted.status != "ok" and posted.error_code in {
            "period_elapsed",
            "version_conflict",
            "already_scheduled",
            "payload_conflict",
        }:
            refreshed = session.get(RecoveryJob, job_id)
            if refreshed is not None:
                _block_cancellation(
                    session, refreshed, token, generation, posted.error_code or "conflict"
                )
            return
    _settle_cancellation(session, tools, job_id, token, generation)


def _block_cancellation(
    session: Session, job: RecoveryJob, token: UUID, generation: int, reason: str
) -> None:
    job.state = "blocked"
    job.block_reason = reason
    job.last_error_code = reason
    job.updated_at = utcnow()
    _note_combined_job(session, job)
    _commit_owned(session, job, token, generation, release=True)


def _settle_cancellation(
    session: Session, tools: Any, job_id: UUID, token: UUID, generation: int
) -> None:
    from resolveai.locale import phrase
    from resolveai.services.cancellation import receipt_matches, subscription_views
    from resolveai.services.replies import upsert_reply_draft
    from resolveai.services.tenancy import require_bound_account

    job, proposal, ticket, run, ledger = _fence(session, job_id, token, generation)
    if ledger.status == "confirmed":
        job.state = "settled"
        job.updated_at = utcnow()
        _commit_owned(session, job, token, generation, release=True)
        return
    require_bound_account(session, ticket)
    command = dict(job.command)
    operation = tools.get_operation(ticket.account_id, str(command["action_id"]))
    listing = tools.list_subscriptions(ticket.account_id)
    viewed = next(
        (
            row
            for row in subscription_views(listing.data)
            if row.id == command.get("subscription_id")
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
    if (
        operation.status == "ok"
        and listing.status == "ok"
        and receipt_matches(command, operation.data, subscription)
    ):
        result = (
            operation.data.get("result")
            if isinstance(operation.data.get("result"), dict)
            else {}
        )
        ledger.status = "confirmed"
        ledger.dispatch_state = "settled"
        ledger.operation_id = str(result.get("operation_id"))
        ledger.result = result
        ledger.error_code = None
        ledger.updated_at = utcnow()
        proposal.status = "executed"
        future = str(command.get("effective_at")) > canonical_instant_now()
        body = phrase(
            "reply_cancellation_confirmed_future"
            if future
            else "reply_cancellation_confirmed_past",
            customer_name=ticket.customer_label,
            effective_at=str(command.get("effective_at")),
            operation_id=str(result.get("operation_id")),
        )
        from resolveai.services.combined import record_financial_state

        if record_financial_state(session, ticket, run, proposal, job, code="confirmed"):
            job.state = "settled"
            job.block_reason = None
            job.updated_at = utcnow()
            _commit_owned(session, job, token, generation, release=True)
            return
        draft = upsert_reply_draft(
            session, ticket=ticket, run=run, body=body, actor_user_id=job.authorizer_user_id
        )
        ledger.reply_draft_id = draft.id
        run.state = "awaiting_reply_review"
        run.error_code = None
        run.updated_at = utcnow()
        ticket.status = "awaiting_reply"
        ticket.updated_at = utcnow()
        job.state = "settled"
        job.block_reason = None
        job.updated_at = utcnow()
        _commit_owned(session, job, token, generation, release=True)
        return
    job.attempt_count += 1
    job.last_error_code = "not_found" if operation.error_code == "not_found" else "unavailable"
    job.updated_at = utcnow()
    if job.attempt_count >= job.max_attempts:
        job.state = "blocked"
        job.block_reason = "delivery_uncertain"
        from resolveai.services.combined import record_financial_state

        ledger.status = "unknown"
        ledger.error_code = "unknown"
        if record_financial_state(
            session, ticket, run, proposal, job, code="uncertain", reason="unknown"
        ):
            _commit_owned(session, job, token, generation, release=True)
            return
        body = phrase(
            "reply_cancellation_unknown",
            customer_name=ticket.customer_label,
            action_id=str(command.get("action_id")),
        )
        draft = upsert_reply_draft(
            session, ticket=ticket, run=run, body=body, actor_user_id=job.authorizer_user_id
        )
        ledger.reply_draft_id = draft.id
        ledger.status = "unknown"
        ledger.error_code = "unknown"
        run.state = "awaiting_reply_review"
        run.error_code = "unknown"
        ticket.status = "awaiting_reply"
    else:
        delay = BACKOFF_SECONDS[min(job.attempt_count - 1, len(BACKOFF_SECONDS) - 1)]
        job.state = "queued"
        job.next_attempt_at = utcnow() + timedelta(seconds=delay)
    _commit_owned(session, job, token, generation, release=True)


def _note_combined_enqueue(session, ticket, run, proposal, job) -> None:
    from resolveai.services.combined import record_financial_state

    record_financial_state(session, ticket, run, proposal, job, code="in_flight")


def _note_combined_job(session: Session, job: RecoveryJob) -> None:
    from resolveai.db.models import Proposal, Run, Ticket
    from resolveai.services.combined import record_financial_state

    proposal = session.get(Proposal, job.proposal_id)
    run = session.get(Run, job.run_id)
    if proposal is None or run is None:
        return
    ticket = session.get(Ticket, proposal.ticket_id)
    if ticket is None:
        return
    record_financial_state(session, ticket, run, proposal, job)


def canonical_instant_now() -> str:
    from resolveai.domain.cancellation import canonical_instant

    return canonical_instant(utcnow())
