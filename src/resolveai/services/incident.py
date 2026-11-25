"""Incident reports and local handoffs.

The shared reply helper is not used for these drafts. Admitted financial
commands are not edited here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import (
    IncidentHandoff,
    IncidentReport,
    ReplyDraft,
    Run,
    Ticket,
    ToolInvocation,
)
from resolveai.domain.cancellation import canonical_instant, parse_instant
from resolveai.domain.incident import (
    REGIONS,
    SERVICES,
    classify_incidents,
    compensation_requested,
    incident_requested,
    parse_snapshot,
    reply_body,
    snapshot_digest,
    validate_request_id,
)
from resolveai.errors import APIError
from resolveai.services.audit import audit
from resolveai.services.input import latest_input_revision
from resolveai.services.replies import append_message
from resolveai.services.tenancy import require_bound_account

MAX_INCIDENT_READS = 3


def current_report(session: Session, ticket_id: UUID) -> IncidentReport | None:
    return session.scalar(
        select(IncidentReport)
        .where(IncidentReport.ticket_id == ticket_id)
        .order_by(IncidentReport.input_revision.desc(), IncidentReport.id.desc())
        .limit(1)
    )


def current_incident_run(session: Session, ticket: Ticket) -> Run | None:
    revision = latest_input_revision(session, ticket.id)
    return session.scalar(
        select(Run)
        .where(Run.ticket_id == ticket.id, Run.message_revision == revision)
        .order_by(Run.created_at.desc(), Run.id.desc())
        .limit(1)
    )


def record_incident_report(
    session: Session,
    *,
    actor: Any,
    ticket: Ticket,
    service_code: str,
    region_code: str,
    reported_started_at: str,
    request_id: str | None,
) -> IncidentReport:
    if actor.role != "support":
        raise APIError(403, "denied", "Only support can record an incident report")
    require_bound_account(session, ticket)
    if service_code not in SERVICES or region_code not in REGIONS:
        raise APIError(422, "validation", "Service or region is not valid")
    try:
        started = parse_instant(reported_started_at)
        cleaned_request = validate_request_id(request_id)
    except ValueError as exc:
        raise APIError(422, "validation", "The incident report is not valid") from exc
    message = append_message(
        session,
        ticket=ticket,
        author_kind="operator",
        body="An incident report was recorded.",
    )
    report = IncidentReport(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        account_id=ticket.account_id,
        customer_id=ticket.customer_id,
        actor_user_id=actor.user_id,
        input_revision=message.revision,
        service_code=service_code,
        region_code=region_code,
        reported_started_at=started,
        request_id=cleaned_request,
        created_at=utcnow(),
    )
    session.add(report)
    session.flush()
    invalidate_incident_reply(session, ticket)
    from resolveai.services.input import invalidate_pending_intent
    from resolveai.services.recovery import cancel_pre_admission

    invalidate_pending_intent(session, ticket)
    cancel_pre_admission(session, ticket)
    audit(
        session,
        tenant_id=ticket.tenant_id,
        event_type="incident_report_recorded",
        actor_user_id=actor.user_id,
        ticket_id=ticket.id,
        payload={"report_id": str(report.id), "input_revision": report.input_revision},
    )
    return report


def invalidate_incident_reply(session: Session, ticket: Ticket) -> None:
    """Supersede an unsent incident reply when input moved.

    Refund and cancellation drafts are left to their own helper. Admitted
    recovery commands are not loaded here.
    """
    current = latest_input_revision(session, ticket.id)
    handoffs = session.scalars(
        select(IncidentHandoff).where(
            IncidentHandoff.ticket_id == ticket.id,
            IncidentHandoff.input_revision != current,
        )
    ).all()
    for handoff in handoffs:
        if handoff.reply_draft_id is None:
            continue
        draft = session.get(ReplyDraft, handoff.reply_draft_id)
        if draft is not None and draft.status == "draft":
            draft.status = "superseded"
        run = session.get(Run, handoff.run_id)
        if run is not None and run.state == "awaiting_reply_review":
            run.state = "cancelled"
            run.error_code = "input_changed"
            run.updated_at = utcnow()


def refuse_financial_proposal(session: Session, ticket_id: UUID) -> None:
    from resolveai.db.models import Message

    rows = session.scalars(
        select(Message)
        .where(
            Message.ticket_id == ticket_id,
            Message.author_kind.in_(("customer", "operator")),
        )
        .order_by(Message.revision)
    ).all()
    messages = [{"body": row.body} for row in rows]
    if incident_requested(messages):
        raise APIError(
            409,
            "incident_no_financial_action",
            "This ticket cannot open a refund or cancellation",
        )


def create_incident_handoff(
    session: Session,
    *,
    actor: Any,
    ticket: Ticket,
    run: Run,
    tools: Any,
) -> ReplyDraft:
    report = current_report(session, ticket.id)
    if report is None:
        raise APIError(409, "report_required", "An incident report is required first")
    existing = session.scalar(
        select(IncidentHandoff).where(
            IncidentHandoff.tenant_id == ticket.tenant_id,
            IncidentHandoff.run_id == run.id,
        )
    )
    if existing is not None and existing.reply_draft_id is not None:
        draft = session.get(ReplyDraft, existing.reply_draft_id)
        if draft is not None:
            return draft
    if (
        report.ticket_id != ticket.id
        or report.account_id != ticket.account_id
        or report.customer_id != ticket.customer_id
        or report.tenant_id != ticket.tenant_id
        or run.ticket_id != ticket.id
        or run.tenant_id != ticket.tenant_id
    ):
        raise APIError(409, "report_mismatch", "The incident report does not match this ticket")
    messages = _messages(session, ticket.id)
    compensation = compensation_requested(messages)
    account_id = str(ticket.account_id)
    reported = canonical_instant(report.reported_started_at)
    first = _latest_list(run)
    if first is None or first.get("status") != "ok":
        return _persist(
            session,
            actor=actor,
            ticket=ticket,
            run=run,
            report=report,
            kind="unavailable",
            reason="read_unavailable",
            incident_id=None,
            incident_version=None,
            evidence={"verified": False, "candidates": [], "context": []},
            digest=None,
            invocation=_invocation_for(session, run, first),
            observed_at=utcnow(),
            compensation=compensation,
        )
    decision = parse_snapshot(account_id, first.get("data"))
    if decision is None:
        return _persist(
            session,
            actor=actor,
            ticket=ticket,
            run=run,
            report=report,
            kind="unavailable",
            reason="read_unavailable",
            incident_id=None,
            incident_version=None,
            evidence={"verified": False, "candidates": [], "context": [_context(first)]},
            digest=None,
            invocation=_invocation_for(session, run, first),
            observed_at=utcnow(),
            compensation=compensation,
        )
    validation = _record_list(session, actor, ticket, run, tools)
    if validation["rows"] is None:
        return _persist(
            session,
            actor=actor,
            ticket=ticket,
            run=run,
            report=report,
            kind="unavailable",
            reason="read_unavailable",
            incident_id=None,
            incident_version=None,
            evidence={
                "verified": False,
                "candidates": [],
                "context": [_historical(decision, first)],
            },
            digest=None,
            invocation=validation["invocation"],
            observed_at=validation["observed_at"],
            compensation=compensation,
        )
    if snapshot_digest(decision) == snapshot_digest(validation["rows"]):
        return _persist_classified(
            session,
            actor=actor,
            ticket=ticket,
            run=run,
            report=report,
            rows=validation["rows"],
            reported=reported,
            compensation=compensation,
            invocation=validation["invocation"],
            observed_at=validation["observed_at"],
        )
    confirming = _record_list(session, actor, ticket, run, tools)
    if confirming["rows"] is None:
        return _persist(
            session,
            actor=actor,
            ticket=ticket,
            run=run,
            report=report,
            kind="unavailable",
            reason="read_unavailable",
            incident_id=None,
            incident_version=None,
            evidence={
                "verified": False,
                "candidates": [],
                "context": [_historical(validation["rows"], None)],
            },
            digest=None,
            invocation=confirming["invocation"],
            observed_at=confirming["observed_at"],
            compensation=compensation,
        )
    if snapshot_digest(validation["rows"]) == snapshot_digest(confirming["rows"]):
        return _persist_classified(
            session,
            actor=actor,
            ticket=ticket,
            run=run,
            report=report,
            rows=confirming["rows"],
            reported=reported,
            compensation=compensation,
            invocation=confirming["invocation"],
            observed_at=confirming["observed_at"],
        )
    return _persist(
        session,
        actor=actor,
        ticket=ticket,
        run=run,
        report=report,
        kind="conflict",
        reason="snapshot_conflict",
        incident_id=None,
        incident_version=None,
        evidence={
            "verified": False,
            "candidates": [],
            "context": [
                {
                    "digest": snapshot_digest(decision),
                    "observation_id": str(_invocation_for(session, run, first).id),
                    "historical": True,
                },
                {
                    "digest": snapshot_digest(validation["rows"]),
                    "observation_id": str(validation["invocation"].id),
                    "historical": True,
                },
                {
                    "digest": snapshot_digest(confirming["rows"]),
                    "observation_id": str(confirming["invocation"].id),
                    "historical": True,
                },
            ],
        },
        digest=None,
        invocation=confirming["invocation"],
        observed_at=confirming["observed_at"],
        compensation=compensation,
    )


def incident_list_count(session: Session, run_id: UUID) -> int:
    value = session.scalar(
        select(func.count())
        .select_from(ToolInvocation)
        .where(
            ToolInvocation.run_id == run_id,
            ToolInvocation.tool_name == "list_incidents",
        )
    )
    return int(value or 0)


def incident_reads_remaining(session: Session, run_id: UUID) -> bool:
    return incident_list_count(session, run_id) < MAX_INCIDENT_READS


def incident_reply_is_current(
    session: Session, handoff: IncidentHandoff, ticket: Ticket, draft: ReplyDraft | None
) -> bool:
    if draft is None or draft.run_id != handoff.run_id:
        return False
    if handoff.input_revision != latest_input_revision(session, ticket.id):
        return False
    current_run = current_incident_run(session, ticket)
    return current_run is not None and handoff.run_id == current_run.id


def lock_incident_reply(
    session: Session, reply_id: UUID, *, sending: bool
) -> IncidentHandoff | None:
    located = session.execute(
        select(IncidentHandoff.ticket_id, IncidentHandoff.run_id, IncidentHandoff.id).where(
            IncidentHandoff.reply_draft_id == reply_id
        )
    ).one_or_none()
    if located is None:
        return None
    ticket_id, run_id, handoff_id = located
    ticket = session.scalar(select(Ticket).where(Ticket.id == ticket_id).with_for_update())
    session.scalar(select(Run).where(Run.id == run_id).with_for_update())
    session.scalar(select(ReplyDraft).where(ReplyDraft.id == reply_id).with_for_update())
    handoff = session.get(IncidentHandoff, handoff_id)
    if ticket is None or handoff is None:
        raise APIError(404, "not_found", "Incident handoff was not found")
    if not sending:
        return handoff
    draft = session.get(ReplyDraft, reply_id)
    if draft is not None and draft.status == "sent":
        return handoff
    if not incident_reply_is_current(session, handoff, ticket, draft):
        raise APIError(409, "stale_proposal", "The incident reply is not current")
    return handoff


def _persist_classified(
    session: Session,
    *,
    actor: Any,
    ticket: Ticket,
    run: Run,
    report: IncidentReport,
    rows: list[dict[str, Any]],
    reported: str,
    compensation: bool,
    invocation: ToolInvocation,
    observed_at: Any,
) -> ReplyDraft:
    outcome = classify_incidents(
        rows,
        service_code=report.service_code,
        region_code=report.region_code,
        reported_started_at=reported,
        request_id=report.request_id,
        compensation=compensation,
    )
    return _persist(
        session,
        actor=actor,
        ticket=ticket,
        run=run,
        report=report,
        kind=outcome["kind"],
        reason=outcome["reason_code"],
        incident_id=outcome["incident_id"],
        incident_version=outcome["incident_resource_version"],
        evidence={"verified": True, "candidates": outcome["evidence"], "context": []},
        digest=snapshot_digest(rows),
        invocation=invocation,
        observed_at=observed_at,
        compensation=compensation,
    )


def _persist(
    session: Session,
    *,
    actor: Any,
    ticket: Ticket,
    run: Run,
    report: IncidentReport,
    kind: str,
    reason: str,
    incident_id: str | None,
    incident_version: int | None,
    evidence: dict[str, Any],
    digest: str | None,
    invocation: ToolInvocation,
    observed_at: Any,
    compensation: bool,
) -> ReplyDraft:
    outcome = {
        "kind": kind,
        "reason_code": reason,
        "incident_id": incident_id,
        "incident_resource_version": incident_version,
        "evidence": evidence.get("candidates") or [],
    }
    body = reply_body(outcome, customer_name=ticket.customer_label)
    draft = _new_draft(session, ticket, run, body, actor.user_id)
    handoff = IncidentHandoff(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        account_id=ticket.account_id,
        customer_id=ticket.customer_id,
        run_id=run.id,
        report_id=report.id,
        actor_user_id=actor.user_id,
        input_revision=latest_input_revision(session, ticket.id),
        reply_draft_id=draft.id,
        tool_invocation_id=invocation.id,
        observed_at=observed_at,
        snapshot_digest=digest,
        kind=kind,
        incident_id=None if incident_id is None else UUID(incident_id),
        incident_resource_version=incident_version,
        evidence=evidence,
        compensation_requested=compensation,
        reason_code=reason,
        next_step="human_review",
        created_at=utcnow(),
    )
    session.add(handoff)
    append_message(session, ticket=ticket, author_kind="internal", body=body)
    run.state = "awaiting_reply_review"
    run.updated_at = utcnow()
    ticket.status = "awaiting_reply"
    ticket.updated_at = utcnow()
    session.flush()
    return draft


def _new_draft(
    session: Session, ticket: Ticket, run: Run, body: str, actor_user_id: UUID | None
) -> ReplyDraft:
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
        actor_user_id=actor_user_id,
        ticket_id=ticket.id,
        run_id=run.id,
        payload={"reply_id": str(created.id), "revision": revision, "incident": True},
    )
    return created


def _messages(session: Session, ticket_id: UUID) -> list[dict[str, Any]]:
    from resolveai.db.models import Message

    rows = session.scalars(
        select(Message)
        .where(
            Message.ticket_id == ticket_id,
            Message.author_kind.in_(("customer", "operator")),
        )
        .order_by(Message.revision)
    ).all()
    return [{"revision": row.revision, "body": row.body} for row in rows]


def _latest_list(run: Run) -> dict[str, Any] | None:
    found = None
    for item in run.observations or []:
        if item.get("tool") == "list_incidents":
            found = item
    return found


def _invocation_for(
    session: Session, run: Run, observation: dict[str, Any] | None
) -> ToolInvocation:
    request_id = None if observation is None else observation.get("request_id")
    found = session.scalar(
        select(ToolInvocation)
        .where(
            ToolInvocation.run_id == run.id,
            ToolInvocation.tool_name == "list_incidents",
            ToolInvocation.request_id == str(request_id or ""),
        )
        .order_by(ToolInvocation.observed_at.desc())
        .limit(1)
    )
    if found is None:
        found = session.scalar(
            select(ToolInvocation)
            .where(ToolInvocation.run_id == run.id, ToolInvocation.tool_name == "list_incidents")
            .order_by(ToolInvocation.observed_at.desc())
            .limit(1)
        )
    if found is None:
        raise APIError(409, "report_required", "The incident read was not recorded")
    return found


def _record_list(
    session: Session, actor: Any, ticket: Ticket, run: Run, tools: Any
) -> dict[str, Any]:
    from uuid import uuid4

    if not incident_reads_remaining(session, run.id):
        return {
            "rows": None,
            "invocation": _invocation_for(session, run, _latest_list(run)),
            "observed_at": utcnow(),
            "skipped": True,
        }
    result = tools.list_incidents(ticket.account_id)
    observed = utcnow()
    request_id = result.request_id or uuid4().hex
    rows = None
    if result.status == "ok":
        rows = parse_snapshot(str(ticket.account_id), result.data)
    payload = result.data if isinstance(result.data, dict) else {}
    invocation = ToolInvocation(
        tenant_id=ticket.tenant_id,
        run_id=run.id,
        tool_name="list_incidents",
        request={"account_id": str(ticket.account_id), "validation": True},
        response={
            "tool": "list_incidents",
            "status": "ok" if rows is not None else "error",
            "data": payload,
            "error_code": None if rows is not None else (result.error_code or "unavailable"),
        },
        status="ok" if rows is not None else "error",
        error_code=None if rows is not None else (result.error_code or "unavailable"),
        retryable=False,
        operation_id=None,
        resource_version=None,
        observed_at=observed,
        request_id=request_id,
    )
    session.add(invocation)
    session.flush()
    return {"rows": rows, "invocation": invocation, "observed_at": observed}


def _context(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": observation.get("request_id"),
        "status": observation.get("status"),
        "historical": True,
    }


def _historical(rows: list[dict[str, Any]], observation: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "digest": snapshot_digest(rows),
        "historical": True,
        "request_id": None if observation is None else observation.get("request_id"),
    }
