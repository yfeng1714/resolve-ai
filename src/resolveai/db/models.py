from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class AppBase(DeclarativeBase):
    metadata = MetaData(schema="app")


class SimBase(DeclarativeBase):
    metadata = MetaData(schema="simulator")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Tenant(AppBase):
    __tablename__ = "tenant"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)


class UserAccount(AppBase):
    __tablename__ = "user_account"
    __table_args__ = (UniqueConstraint("tenant_id", "email"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    email: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)


class Membership(AppBase):
    __tablename__ = "membership"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id"),
        CheckConstraint("role IN ('support', 'approver')", name="membership_role"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.user_account.id"))
    role: Mapped[str] = mapped_column(Text)


class UserSession(AppBase):
    __tablename__ = "user_session"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.user_account.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    csrf_hash: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Ticket(AppBase):
    __tablename__ = "ticket"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'awaiting_approval', 'awaiting_reply', 'handed_off', 'reply_sent')",
            name="ticket_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"), index=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    customer_label: Mapped[str] = mapped_column(Text)
    customer_email: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Message(AppBase):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("ticket_id", "revision"),
        CheckConstraint(
            "author_kind IN ('customer', 'operator', 'internal', 'system')",
            name="message_author_kind",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.ticket.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    author_kind: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Run(AppBase):
    __tablename__ = "run"
    __table_args__ = (
        CheckConstraint(
            "state IN ("
            "'analyzing', 'gathering_evidence', 'proposing', 'awaiting_customer', "
            "'awaiting_approval', 'executing', 'verifying', 'awaiting_reply_review', "
            "'completed', 'handed_off', 'failed', 'cancelled', 'reconciling'"
            ")",
            name="run_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.ticket.id"), index=True)
    state: Mapped[str] = mapped_column(Text)
    message_revision: Mapped[int] = mapped_column(Integer)
    observations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    concise_reasons: Mapped[list[str]] = mapped_column(JSONB)
    provider: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    model_calls: Mapped[int] = mapped_column(Integer)
    tool_calls: Mapped[int] = mapped_column(Integer)
    replans: Mapped[int] = mapped_column(Integer)
    read_retries: Mapped[int] = mapped_column(Integer)
    budget_usd_micros: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Proposal(AppBase):
    __tablename__ = "proposal"
    __table_args__ = (
        UniqueConstraint("tenant_id", "action_id", "revision"),
        CheckConstraint("action_type IN ('refund')", name="proposal_action_type"),
        CheckConstraint("currency = 'USD'", name="proposal_currency"),
        CheckConstraint("amount_cents > 0", name="proposal_amount"),
        CheckConstraint(
            "status IN ('open', 'approved', 'rejected', 'superseded', "
            "'executed', 'stale', 'failed')",
            name="proposal_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.ticket.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.run.id"))
    action_type: Mapped[str] = mapped_column(Text)
    action_id: Mapped[str] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(Text)
    target_payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    target_event_id: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(Text)
    preconditions: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Approval(AppBase):
    __tablename__ = "approval"
    __table_args__ = (
        UniqueConstraint("proposal_id"),
        CheckConstraint("decision IN ('approved', 'rejected')", name="approval_decision"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    proposal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.proposal.id"))
    revision: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(Text)
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.user_account.id"))
    decision: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ActionLedger(AppBase):
    __tablename__ = "action_ledger"
    __table_args__ = (
        UniqueConstraint("tenant_id", "action_id"),
        CheckConstraint("action_type IN ('refund', 'reply')", name="ledger_action_type"),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected', 'unknown')",
            name="ledger_status",
        ),
        CheckConstraint(
            "dispatch_state IN ('reserved', 'dispatched', 'settled')",
            name="ledger_dispatch_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    action_id: Mapped[str] = mapped_column(Text)
    action_type: Mapped[str] = mapped_column(Text)
    proposal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app.proposal.id"), nullable=True
    )
    reply_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app.reply_draft.id"), nullable=True
    )
    payload_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    dispatch_state: Mapped[str] = mapped_column(Text)
    operation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolInvocation(AppBase):
    __tablename__ = "tool_invocation"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.run.id"), index=True)
    tool_name: Mapped[str] = mapped_column(Text)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean)
    operation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(Text)


class AuditEvent(AppBase):
    __tablename__ = "audit_event"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReplyDraft(AppBase):
    __tablename__ = "reply_draft"
    __table_args__ = (
        UniqueConstraint("ticket_id", "revision"),
        UniqueConstraint("tenant_id", "send_action_id"),
        CheckConstraint(
            "status IN ('draft', 'sent', 'rejected', 'superseded')",
            name="reply_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.tenant.id"))
    ticket_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.ticket.id"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app.run.id"))
    revision: Mapped[int] = mapped_column(Integer)
    recipient: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    send_action_id: Mapped[str] = mapped_column(Text)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Customer(SimBase):
    __tablename__ = "customer"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    display_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Account(SimBase):
    __tablename__ = "account"
    __table_args__ = (CheckConstraint("currency = 'USD'", name="account_currency"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("simulator.customer.id"), unique=True)
    currency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Invoice(SimBase):
    __tablename__ = "invoice"
    __table_args__ = (
        UniqueConstraint("account_id", "number"),
        CheckConstraint("currency = 'USD'", name="invoice_currency"),
        CheckConstraint("amount_cents >= 0", name="invoice_amount"),
        CheckConstraint("status IN ('open', 'paid', 'void')", name="invoice_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("simulator.account.id"), index=True)
    number: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Payment(SimBase):
    __tablename__ = "payment"
    __table_args__ = (
        CheckConstraint("currency = 'USD'", name="payment_currency"),
        CheckConstraint("amount_cents > 0", name="payment_amount"),
        CheckConstraint("status IN ('pending', 'settled', 'failed')", name="payment_status"),
        CheckConstraint(
            "status <> 'settled' OR settled_at IS NOT NULL",
            name="payment_settled_timestamp",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("simulator.account.id"), index=True)
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("simulator.invoice.id"), nullable=True
    )
    event_id: Mapped[str] = mapped_column(Text, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resource_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Refund(SimBase):
    __tablename__ = "refund"
    __table_args__ = (
        CheckConstraint("currency = 'USD'", name="refund_currency"),
        CheckConstraint("amount_cents > 0", name="refund_amount"),
        CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="refund_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("simulator.account.id"))
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("simulator.payment.id"), index=True)
    event_id: Mapped[str] = mapped_column(Text)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    action_id: Mapped[str] = mapped_column(Text, unique=True)
    operation_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Operation(SimBase):
    __tablename__ = "operation"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'rejected')",
            name="operation_status",
        ),
    )
    action_id: Mapped[str] = mapped_column(Text, primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    payload_hash: Mapped[str] = mapped_column(Text)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    write_attempts: Mapped[int] = mapped_column(Integer)
    replay_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
