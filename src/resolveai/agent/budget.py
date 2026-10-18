"""Whether a model call is allowed before it starts.

Fake development calls cost nothing. A live call needs an owner-selected
provider, credential, model name and a positive total cap. Ticket spend includes
earlier runs, retries included.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from resolveai.db.models import Run
from resolveai.settings import LiveModelBlocked, Settings


def ticket_spend_micros(session: Session, ticket_id: UUID) -> int:
    spent = session.scalar(
        select(func.coalesce(func.sum(Run.budget_usd_micros), 0)).where(Run.ticket_id == ticket_id)
    )
    return int(spent or 0)


def admission_decision(
    *,
    provider: str,
    cap_micros: int,
    spent_micros: int,
    estimate_micros: int,
    api_key: str,
    model_name: str,
    credential_present: bool | None = None,
) -> None:
    present = bool(api_key) if credential_present is None else credential_present
    if provider == "fake" and estimate_micros <= 0:
        return
    if provider != "fake" and (cap_micros <= 0 or not present or not model_name):
        raise LiveModelBlocked(
            "Live model calls need an owner-selected provider, credential, model name "
            "and a positive total spending cap"
        )
    if cap_micros <= 0 or spent_micros + estimate_micros > cap_micros:
        raise LiveModelBlocked("The ticket or total model budget does not cover this call")


def admit_model_call(
    session: Session,
    *,
    ticket_id: UUID,
    settings: Settings,
    estimate_micros: int,
    spent_micros: int | None = None,
    credential_present: bool | None = None,
) -> int:
    spent = ticket_spend_micros(session, ticket_id) if spent_micros is None else spent_micros
    cap = int(settings.llm_budget_usd * 1_000_000)
    admission_decision(
        provider=settings.model_provider,
        cap_micros=cap,
        spent_micros=spent,
        estimate_micros=estimate_micros,
        api_key=settings.model_api_key,
        model_name=settings.model_name,
        credential_present=credential_present,
    )
    return spent
