from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resolveai.clock import utcnow
from resolveai.db.models import Message, ReplyDraft, Run, Ticket
from resolveai.domain.hashing import body_hash
from resolveai.domain.replies import confirmed_reply, rejected_reply, unknown_reply
from resolveai.services.audit import audit


def render_reply(
    *,
    kind: str,
    ticket: Ticket,
    evidence: dict,
    amount_cents: int,
    action_id: str,
    operation_id: str | None,
    payment_id: UUID,
    event_id: str,
) -> str:
    customer_name = str(evidence.get("customer_name") or ticket.customer_label)
    if kind == "confirmed":
        if not operation_id:
            raise ValueError("confirmed reply requires an operation id")
        return confirmed_reply(
            customer_name=customer_name,
            amount_cents=amount_cents,
            operation_id=operation_id,
            payment_id=payment_id,
            event_id=event_id,
            invoice_number=evidence.get("invoice_number"),
        )
    if kind == "unknown":
        return unknown_reply(
            customer_name=customer_name,
            amount_cents=amount_cents,
            action_id=action_id,
        )
    if kind == "rejected":
        return rejected_reply(customer_name=customer_name)
    raise ValueError(f"unknown reply kind {kind}")


def upsert_reply_draft(
    session: Session,
    *,
    ticket: Ticket,
    run: Run,
    body: str,
    actor_user_id: UUID | None,
) -> ReplyDraft:
    digest = body_hash(body)
    drafts = session.scalars(
        select(ReplyDraft)
        .where(ReplyDraft.ticket_id == ticket.id)
        .order_by(ReplyDraft.revision)
    ).all()
    for draft in drafts:
        if draft.status == "draft" and draft.body_hash == digest:
            return draft
        if draft.status == "draft":
            draft.status = "superseded"
    revision = (drafts[-1].revision if drafts else 0) + 1
    created = ReplyDraft(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        run_id=run.id,
        revision=revision,
        recipient=ticket.customer_email,
        body=body,
        body_hash=digest,
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
        payload={"reply_id": str(created.id), "revision": revision},
    )
    return created


def append_message(
    session: Session,
    *,
    ticket: Ticket,
    author_kind: str,
    body: str,
) -> Message:
    current = session.scalar(
        select(func.max(Message.revision)).where(Message.ticket_id == ticket.id)
    )
    message = Message(
        tenant_id=ticket.tenant_id,
        ticket_id=ticket.id,
        revision=(current or 0) + 1,
        author_kind=author_kind,
        body=body,
        created_at=utcnow(),
    )
    session.add(message)
    session.flush()
    return message


def new_receipt(draft: ReplyDraft) -> dict:
    return {
        "receipt_id": str(uuid4()),
        "recipient": draft.recipient,
        "body_hash": draft.body_hash,
        "sent_at": utcnow().isoformat(),
    }
