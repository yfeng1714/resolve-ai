"""Period-end cancellation identity, time, and the decision table.

Customer or model text does not choose an account or a subscription. A stored
choice id is meaningful only after the server has matched it to a fresh read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from resolveai.domain.hashing import canonical_hash

DecisionKind = Literal["propose", "explain", "wait", "handoff"]


def canonical_instant(value: datetime) -> str:
    """UTC timestamp with six fractional digits, used in storage, payload, and action id."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC).replace(tzinfo=UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond:06d}Z"


def parse_instant(value: str) -> datetime:
    if not value.endswith("Z") or len(value) != 27:
        raise ValueError("period end must be a canonical UTC instant")
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    if canonical_instant(parsed) != value:
        raise ValueError("period end must be a canonical UTC instant")
    return parsed


def cancellation_action_id(subscription_id: str, effective_at: str) -> str:
    parse_instant(effective_at)
    return f"cancel_at_period_end:{subscription_id}:{effective_at}"


def effect_payload(
    *,
    action_id: str,
    account_id: str,
    subscription_id: str,
    effective_at: str,
    policy_version: str,
) -> dict[str, Any]:
    parse_instant(effective_at)
    return {
        "account_id": account_id,
        "action": "cancel_at_period_end",
        "action_id": action_id,
        "effective_at": effective_at,
        "policy_version": policy_version,
        "subscription_id": subscription_id,
    }


def attempt_payload(effect: dict[str, Any], expected_resource_version: int) -> dict[str, Any]:
    if expected_resource_version < 1:
        raise ValueError("resource version must be positive")
    body = dict(effect)
    body["expected_resource_version"] = expected_resource_version
    return body


def effect_hash(effect: dict[str, Any]) -> str:
    return canonical_hash(effect)


def attempt_hash(effect: dict[str, Any], expected_resource_version: int) -> str:
    return canonical_hash(attempt_payload(effect, expected_resource_version))


def bound_command(
    *,
    action_id: str,
    account_id: str,
    subscription_id: str,
    effective_at: str,
    policy_version: str,
    expected_resource_version: int,
) -> dict[str, Any]:
    """The only cancellation command an approval may dispatch."""
    effect = effect_payload(
        action_id=action_id,
        account_id=account_id,
        subscription_id=subscription_id,
        effective_at=effective_at,
        policy_version=policy_version,
    )
    return {
        "account_id": account_id,
        "action": "cancel_at_period_end",
        "action_id": action_id,
        "attempt_hash": attempt_hash(effect, expected_resource_version),
        "effect_hash": effect_hash(effect),
        "effective_at": effective_at,
        "expected_resource_version": expected_resource_version,
        "policy_version": policy_version,
        "subscription_id": subscription_id,
    }


def _message_kind(body: str) -> str | None:
    """Classify one message. A later message can replace an earlier one."""
    text = body.lower()
    period = "schedule cancellation at period end" in text
    immediate = "terminate immediately" in text
    refund = "refund" in text
    if refund and (period or immediate):
        return "combined_intent"
    if immediate:
        return "immediate"
    if period:
        return "period_end"
    return None


def intent_gate(messages: list[dict[str, Any]]) -> str | None:
    """Fixed phrases only. The latest phrase-bearing message is the request.

    A refund together with either cancellation phrase, in one message or across
    the request, stays a combined stop. An older period-end phrase does not
    outrank a newer immediate request.
    """
    saw_refund = False
    saw_cancel = False
    latest: str | None = None
    for item in messages:
        kind = _message_kind(str(item.get("body") or ""))
        body = str(item.get("body") or "").lower()
        saw_refund = saw_refund or "refund" in body
        saw_cancel = saw_cancel or kind in {"combined_intent", "immediate", "period_end"}
        if kind is not None:
            latest = kind
    if saw_refund and saw_cancel:
        return "combined_intent"
    return latest


def latest_immediate_revision(messages: list[dict[str, Any]]) -> int | None:
    """Revision of the newest message that asks to terminate immediately.

    A refund in the same message does not erase the immediate phrase. The
    cancellation half still waits for a later stored period-end choice.
    """
    found: int | None = None
    for index, item in enumerate(messages, start=1):
        if "terminate immediately" not in str(item.get("body") or "").lower():
            continue
        revision = item.get("revision")
        found = int(revision) if revision is not None else index
    return found


def choice_confirms_immediate(
    messages: list[dict[str, Any]], *, choice_revision: int | None
) -> bool:
    """A choice confirms an immediate request only when it was recorded later."""
    immediate_at = latest_immediate_revision(messages)
    if choice_revision is None or immediate_at is None:
        return False
    return choice_revision > immediate_at


@dataclass(frozen=True)
class SubscriptionView:
    id: str
    plan_code: str
    status: str
    cancel_at_period_end: bool
    current_period_end: str
    resource_version: int


@dataclass(frozen=True)
class CancellationDecision:
    kind: DecisionKind
    subscription_id: str | None
    reason: str
    subscriptions: tuple[SubscriptionView, ...] = ()


def subscription_route(
    messages: list[dict[str, Any]],
    rows: list[SubscriptionView],
    *,
    choice_id: str | None,
    now: datetime,
    done: set[str],
    choice_revision: int | None = None,
) -> tuple[str, str] | None:
    """Return `(tool_or_decide, phrase_key)` for a cancellation ticket."""
    gate = intent_gate(messages)
    if gate is None:
        return None
    if "list_subscriptions" not in done:
        return ("list_subscriptions", "read_before_deciding")
    if gate == "combined_intent":
        if "handoff_ticket" in done:
            return ("decide", "handed_off_done")
        return ("handoff_ticket", "note_cancellation_combined")
    confirmed = choice_confirms_immediate(messages, choice_revision=choice_revision)
    if latest_immediate_revision(messages) is not None and not confirmed:
        if "add_internal_note" in done:
            return ("decide", "clarification_recorded")
        return ("add_internal_note", "note_cancellation_immediate")
    decision = classify_subscriptions(rows, choice_id=choice_id, now=now)
    if decision.kind == "propose":
        if "propose_cancellation" in done:
            return ("decide", "proposal_waiting")
        return ("propose_cancellation", "evidence_complete")
    if decision.kind == "wait":
        if "add_internal_note" in done:
            return ("decide", "clarification_recorded")
        return ("add_internal_note", "note_cancellation_wait")
    if decision.kind == "explain":
        if "add_internal_note" in done:
            return ("decide", "clarification_recorded")
        return ("add_internal_note", "note_cancellation_explain")
    if "handoff_ticket" in done:
        return ("decide", "handed_off_done")
    return ("handoff_ticket", "outside_rule_handoff")


def classify_subscriptions(
    rows: list[SubscriptionView],
    *,
    choice_id: str | None,
    now: datetime,
) -> CancellationDecision:
    """Intent gates are applied by the caller before this function."""
    current = canonical_instant(now)
    active = [row for row in rows if row.status == "active"]
    unscheduled = [
        row for row in active if not row.cancel_at_period_end and row.current_period_end > current
    ]
    scheduled = [row for row in active if row.cancel_at_period_end]
    elapsed = [
        row for row in active if not row.cancel_at_period_end and row.current_period_end <= current
    ]
    by_id = {row.id: row for row in active}
    if choice_id:
        chosen = by_id.get(choice_id)
        if chosen is None:
            return CancellationDecision("wait", None, "stale_choice", tuple(active))
        if chosen.cancel_at_period_end:
            return CancellationDecision("explain", chosen.id, "already_scheduled", (chosen,))
        if chosen.current_period_end <= current:
            return CancellationDecision("explain", chosen.id, "period_elapsed", (chosen,))
        return CancellationDecision("propose", chosen.id, "chosen_subscription", (chosen,))
    if len(unscheduled) == 1 and not scheduled and not elapsed:
        only = unscheduled[0]
        return CancellationDecision("propose", only.id, "single_subscription", (only,))
    if not unscheduled and scheduled and not elapsed:
        return CancellationDecision("explain", None, "already_scheduled", tuple(scheduled))
    if not unscheduled and not scheduled:
        return CancellationDecision("handoff", None, "no_subscription", tuple(elapsed))
    return CancellationDecision("wait", None, "choice_required", tuple(active))
