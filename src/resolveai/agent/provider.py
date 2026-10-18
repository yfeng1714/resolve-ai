"""Provider contract for an investigation step.

The snapshot carries customer and operator messages, trusted ticket scope,
observations and the permitted tool list. Model output cannot approve, send,
or choose a refund amount. The fake provider is the CI default. Its next tool
depends on the billing observations already collected.
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from resolveai.locale import phrase

FAKE_PROMPT_VERSION = "fake-m2-v1"
OPENING_READS = (
    "get_customer_account",
    "list_invoices",
    "list_payments",
)
PERMITTED_TOOLS = (
    "get_customer_account",
    "list_invoices",
    "list_payments",
    "list_refunds",
    "search_knowledge",
    "propose_refund",
    "get_operation_status",
    "add_internal_note",
    "handoff_ticket",
    "draft_reply",
)


@dataclass
class RunSnapshot:
    observations: list[dict[str, Any]]
    messages: list[dict[str, Any]] = field(default_factory=list)
    tenant_id: str = ""
    account_id: str = ""
    ticket_id: str = ""
    permitted_tools: tuple[str, ...] = PERMITTED_TOOLS
    model_calls: int = 0
    tool_calls: int = 0
    replans: int = 0
    awaiting_customer: bool = False


@dataclass(frozen=True)
class ProviderAction:
    route: Literal["tool", "decide", "replan", "stop"]
    reason: str
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


class Provider(Protocol):
    prompt_version: str

    def next_action(self, snapshot: RunSnapshot) -> ProviderAction: ...


def successful_tools(snapshot: RunSnapshot) -> set[str]:
    return {
        str(item.get("tool"))
        for item in snapshot.observations
        if item.get("status") == "ok" and item.get("tool")
    }


def classify_observations(snapshot: RunSnapshot) -> str:
    """Route from payment observations. This does not set a refund amount."""
    payments = _rows(snapshot, "list_payments", "payments")
    if payments is None:
        return "unread"
    grouped: dict[str, list[dict[str, Any]]] = {}
    for payment in payments:
        grouped.setdefault(str(payment.get("event_id") or ""), []).append(payment)
    saw_duplicate = False
    for group in grouped.values():
        if any(payment.get("status") == "pending" for payment in group):
            return "pending"
        settled = [payment for payment in group if payment.get("status") == "settled"]
        if len(settled) != 2:
            continue
        saw_duplicate = True
        invoices = {str(payment.get("invoice_id") or "") for payment in settled}
        if len(invoices) > 1 and "" not in invoices:
            return "conflict"
    if saw_duplicate:
        return "duplicate"
    return "other"


class FakeProvider:
    prompt_version = FAKE_PROMPT_VERSION

    def next_action(self, snapshot: RunSnapshot) -> ProviderAction:
        last = snapshot.observations[-1] if snapshot.observations else None
        if last and last.get("status") == "ok" and last.get("tool") == "propose_refund":
            return ProviderAction(route="decide", reason=phrase("proposal_waiting"))
        if last and last.get("status") == "ok" and last.get("tool") == "handoff_ticket":
            return ProviderAction(route="decide", reason=phrase("handed_off_done"))
        if last and last.get("status") == "ok" and last.get("tool") == "add_internal_note":
            return ProviderAction(route="decide", reason=phrase("clarification_recorded"))
        billing_reads = (*OPENING_READS, "list_refunds")
        if last and last.get("tool") in billing_reads and last.get("status") == "error":
            if not last.get("retryable"):
                return ProviderAction(
                    route="tool",
                    tool_name="handoff_ticket",
                    reason=phrase("read_failed_handoff"),
                )
        if last and last.get("tool") == "propose_refund" and last.get("status") == "error":
            outcome = (last.get("data") or {}).get("outcome")
            if outcome == "clarify_pending":
                return ProviderAction(
                    route="tool",
                    tool_name="add_internal_note",
                    reason=phrase("pending_no_refund"),
                )
            return ProviderAction(
                route="tool",
                tool_name="handoff_ticket",
                reason=phrase("outside_rule_handoff"),
            )
        done = successful_tools(snapshot)
        for name in OPENING_READS:
            if name not in done:
                return ProviderAction(
                    route="tool",
                    tool_name=name,
                    reason=phrase("read_before_deciding", tool=name.replace("_", " ")),
                )
        shape = classify_observations(snapshot)
        if shape == "pending":
            return _once(
                done,
                "add_internal_note",
                phrase("pending_no_refund"),
            )
        if shape == "conflict":
            if "search_knowledge" not in done:
                return ProviderAction(
                    route="tool",
                    tool_name="search_knowledge",
                    reason=phrase("conflict_needs_policy"),
                    arguments={"query": "conflicting invoice evidence"},
                )
            return _once(done, "handoff_ticket", phrase("outside_rule_handoff"))
        if shape == "duplicate":
            if "list_refunds" not in done:
                return ProviderAction(
                    route="tool",
                    tool_name="list_refunds",
                    reason=phrase("refunds_before_proposal"),
                )
            if _refund_blocks(snapshot):
                return _once(done, "handoff_ticket", phrase("outside_rule_handoff"))
            if "search_knowledge" not in done:
                return ProviderAction(
                    route="tool",
                    tool_name="search_knowledge",
                    reason=phrase("look_up_policy"),
                    arguments={"query": "duplicate payment refund"},
                )
            if not any(item.get("tool") == "propose_refund" for item in snapshot.observations):
                return ProviderAction(
                    route="tool",
                    tool_name="propose_refund",
                    reason=phrase("evidence_complete"),
                )
            return ProviderAction(route="decide", reason=phrase("no_further_read"))
        if "list_refunds" not in done:
            return ProviderAction(
                route="tool",
                tool_name="list_refunds",
                reason=phrase("read_before_deciding", tool="list refunds"),
            )
        return _once(done, "handoff_ticket", phrase("outside_rule_handoff"))


def _once(done: set[str], tool_name: str, reason: str) -> ProviderAction:
    if tool_name in done:
        return ProviderAction(route="decide", reason=phrase("no_further_read"))
    return ProviderAction(route="tool", tool_name=tool_name, reason=reason)


def _rows(snapshot: RunSnapshot, tool_name: str, key: str) -> list[dict[str, Any]] | None:
    found = None
    for item in snapshot.observations:
        if item.get("tool") == tool_name and item.get("status") == "ok":
            found = item
    if found is None:
        return None
    rows = (found.get("data") or {}).get(key) or []
    return [row for row in rows if isinstance(row, dict)]


def _refund_blocks(snapshot: RunSnapshot) -> bool:
    rows = _rows(snapshot, "list_refunds", "refunds") or []
    return any(row.get("status") in {"pending", "succeeded"} for row in rows)
