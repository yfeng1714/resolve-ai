import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID


def canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def refund_payload(
    *,
    action_id: str,
    account_id: UUID | str,
    payment_id: UUID | str,
    event_id: str,
    amount_cents: int,
    policy_version: str,
) -> dict[str, Any]:
    if amount_cents <= 0:
        raise ValueError("refund amount must be positive")
    return {
        "account_id": str(account_id),
        "action": "refund",
        "action_id": action_id,
        "amount_cents": amount_cents,
        "currency": "USD",
        "event_id": event_id,
        "payment_id": str(payment_id),
        "policy_version": policy_version,
    }


def refund_action_id(payment_id: UUID | str) -> str:
    return f"refund:{payment_id}"


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
