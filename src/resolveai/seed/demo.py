"""Reset and load the synthetic demo into the local ResolveAI database."""

import json
from datetime import timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from resolveai.clock import utcnow
from resolveai.db.models import (
    Account,
    Customer,
    Invoice,
    Membership,
    Message,
    Operation,
    Payment,
    Refund,
    Tenant,
    Ticket,
    UserAccount,
)
from resolveai.db.session import make_engine, make_session_factory
from resolveai.domain.hashing import canonical_hash, refund_action_id, refund_payload
from resolveai.domain.passwords import hash_password
from resolveai.domain.policy import load_refund_policy
from resolveai.retrieval.corpus import import_knowledge
from resolveai.settings import LOCAL_DATABASES, Settings, SettingsError, load_settings, repo_root


def load_manifest() -> dict:
    path = repo_root() / "fixtures" / "demo_manifest.json"
    return json.loads(path.read_text(encoding="utf-8"))


def seed_demo(settings: Settings | None = None) -> dict[str, int | str]:
    settings = settings or load_settings()
    if settings.resolveai_env != "demo":
        raise SettingsError("Refusing to seed because RESOLVEAI_ENV is not demo")
    engine = make_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            identity = connection.execute(
                text("SELECT current_user, current_database()")
            ).one()
        database_ok = identity.current_database in LOCAL_DATABASES
        user_ok = identity.current_user == "resolveai_demo"
        if not database_ok or not user_ok:
            raise SettingsError(
                "Refusing to seed a database that is not the local demo initializer"
            )
        manifest = load_manifest()
        factory = make_session_factory(engine)
        with factory() as session:
            try:
                session.execute(
                    text(
                        """
                        TRUNCATE TABLE
                          app.audit_event,
                          app.tool_invocation,
                          app.action_ledger,
                          app.approval,
                          app.reply_draft,
                          app.proposal,
                          app.run,
                          app.message,
                          app.user_session,
                          app.ticket,
                          app.knowledge_chunk,
                          app.knowledge_document,
                          app.membership,
                          app.user_account,
                          app.tenant
                        RESTART IDENTITY CASCADE
                        """
                    )
                )
                session.execute(
                    text(
                        """
                        TRUNCATE TABLE
                          simulator.refund,
                          simulator.operation,
                          simulator.payment,
                          simulator.invoice,
                          simulator.account,
                          simulator.customer
                        RESTART IDENTITY CASCADE
                        """
                    )
                )
            except ProgrammingError as exc:
                raise SettingsError(
                    "Seed failed. Apply migrations with `make migrate` first."
                ) from exc
            now = utcnow()
            tenant = manifest["tenant"]
            session.add(Tenant(id=UUID(tenant["id"]), slug=tenant["slug"], name=tenant["name"]))
            session.flush()
            for user in manifest["users"]:
                session.add(
                    UserAccount(
                        id=UUID(user["id"]),
                        tenant_id=UUID(tenant["id"]),
                        email=user["email"].lower(),
                        password_hash=hash_password(user["password"]),
                        display_name=user["display_name"],
                    )
                )
            session.flush()
            for user in manifest["users"]:
                session.add(
                    Membership(
                        id=UUID(user["membership_id"]),
                        tenant_id=UUID(tenant["id"]),
                        user_id=UUID(user["id"]),
                        role=user["role"],
                    )
                )
            session.flush()
            policy_version = load_refund_policy().version
            for case in manifest["cases"]:
                session.add(
                    Customer(
                        id=UUID(case["customer_id"]),
                        display_name=case["display_name"],
                        email=case["email"],
                        created_at=now,
                    )
                )
                session.flush()
                session.add(
                    Account(
                        id=UUID(case["account_id"]),
                        customer_id=UUID(case["customer_id"]),
                        currency="USD",
                        status="active",
                        created_at=now,
                    )
                )
                session.flush()
                invoices = {row["number"]: row for row in case["invoices"]}
                for invoice in case["invoices"]:
                    session.add(
                        Invoice(
                            id=UUID(invoice["id"]),
                            account_id=UUID(case["account_id"]),
                            number=invoice["number"],
                            amount_cents=invoice["amount_cents"],
                            currency="USD",
                            status=invoice["status"],
                            issued_at=now - timedelta(days=invoice["issued_days_ago"]),
                        )
                    )
                session.flush()
                payments = {row["id"]: row for row in case["payments"]}
                refunded_payments = {row["payment_id"] for row in case["refunds"]}
                for payment in case["payments"]:
                    settled_at = None
                    if payment["status"] == "settled":
                        settled_at = now - timedelta(days=payment["settled_days_ago"]) + timedelta(
                            minutes=payment["settled_offset_minutes"]
                        )
                    invoice = invoices[payment["invoice_number"]]
                    session.add(
                        Payment(
                            id=UUID(payment["id"]),
                            account_id=UUID(case["account_id"]),
                            invoice_id=UUID(invoice["id"]),
                            event_id=payment["event_id"],
                            amount_cents=payment["amount_cents"],
                            currency="USD",
                            status=payment["status"],
                            settled_at=settled_at,
                            resource_version=2 if payment["id"] in refunded_payments else 1,
                            created_at=now,
                        )
                    )
                session.flush()
                for refund in case["refunds"]:
                    payment = payments[refund["payment_id"]]
                    action_id = refund_action_id(refund["payment_id"])
                    payload = refund_payload(
                        action_id=action_id,
                        account_id=case["account_id"],
                        payment_id=refund["payment_id"],
                        event_id=payment["event_id"],
                        amount_cents=refund["amount_cents"],
                        policy_version=policy_version,
                    )
                    operation_status = "confirmed" if refund["status"] == "succeeded" else "pending"
                    session.add(
                        Refund(
                            id=UUID(refund["id"]),
                            account_id=UUID(case["account_id"]),
                            payment_id=UUID(refund["payment_id"]),
                            event_id=payment["event_id"],
                            amount_cents=refund["amount_cents"],
                            currency="USD",
                            status=refund["status"],
                            action_id=action_id,
                            operation_id=refund["operation_id"],
                            created_at=now,
                        )
                    )
                    session.add(
                        Operation(
                            action_id=action_id,
                            operation_id=UUID(refund["operation_id"]),
                            payload_hash=canonical_hash(payload),
                            account_id=UUID(case["account_id"]),
                            status=operation_status,
                            result={
                                "operation_status": operation_status,
                                "operation_id": refund["operation_id"],
                                "refund_id": refund["id"],
                                "action_id": action_id,
                                "amount_cents": refund["amount_cents"],
                                "currency": "USD",
                                "payment_id": refund["payment_id"],
                                "event_id": payment["event_id"],
                                "resource_version": 2,
                                "replayed": False,
                            },
                            write_attempts=1,
                            replay_count=0,
                            created_at=now,
                        )
                    )
                session.add(
                    Ticket(
                        id=UUID(case["ticket_id"]),
                        tenant_id=UUID(tenant["id"]),
                        customer_id=UUID(case["customer_id"]),
                        account_id=UUID(case["account_id"]),
                        customer_label=case["display_name"],
                        customer_email=case["email"],
                        subject=case["subject"],
                        status="open",
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.flush()
                session.add(
                    Message(
                        tenant_id=UUID(tenant["id"]),
                        ticket_id=UUID(case["ticket_id"]),
                        revision=1,
                        author_kind="customer",
                        body=case["body"],
                        created_at=now,
                    )
                )
            import_knowledge(session, UUID(tenant["id"]))
            session.commit()
        return {"tenant": tenant["slug"], "tickets": len(manifest["cases"])}
    finally:
        engine.dispose()


def main() -> None:
    counts = seed_demo()
    print(f"Seeded {counts['tickets']} tickets for {counts['tenant']}")
