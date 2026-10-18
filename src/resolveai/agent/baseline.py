"""B1 reads the billing tools in a fixed order, then branches on observations.

It uses the same tool names and safety gateway as the investigation provider.
It does not ignore a pending payment or conflicting invoices once those reads exist.
"""

from resolveai.agent.provider import (
    ProviderAction,
    RunSnapshot,
    classify_observations,
    successful_tools,
)
from resolveai.locale import phrase

BILLING_READS = (
    "get_customer_account",
    "list_invoices",
    "list_payments",
    "list_refunds",
)


class BaselineWorkflow:
    prompt_version = "b1-m2-v1"

    def next_action(self, snapshot: RunSnapshot) -> ProviderAction:
        done = successful_tools(snapshot)
        last = snapshot.observations[-1] if snapshot.observations else None
        if last and last.get("status") == "ok" and last.get("tool") in {
            "propose_refund",
            "handoff_ticket",
            "add_internal_note",
        }:
            return ProviderAction(route="decide", reason=phrase("no_further_read"))
        for name in BILLING_READS:
            if name not in done:
                return ProviderAction(
                    route="tool",
                    tool_name=name,
                    reason=phrase("read_before_deciding", tool=name.replace("_", " ")),
                )
        shape = classify_observations(snapshot)
        if shape == "pending" and "add_internal_note" not in done:
            return ProviderAction(
                route="tool",
                tool_name="add_internal_note",
                reason=phrase("pending_no_refund"),
            )
        if shape == "conflict" and "search_knowledge" not in done:
            return ProviderAction(
                route="tool",
                tool_name="search_knowledge",
                reason=phrase("conflict_needs_policy"),
                arguments={"query": "conflicting invoice evidence"},
            )
        if shape == "duplicate" and not _refund_blocks(snapshot) and "search_knowledge" not in done:
            return ProviderAction(
                route="tool",
                tool_name="search_knowledge",
                reason=phrase("look_up_policy"),
                arguments={"query": "duplicate payment refund"},
            )
        if (
            shape == "duplicate"
            and not _refund_blocks(snapshot)
            and not any(item.get("tool") == "propose_refund" for item in snapshot.observations)
        ):
            return ProviderAction(
                route="tool",
                tool_name="propose_refund",
                reason=phrase("evidence_complete"),
            )
        if "handoff_ticket" not in done:
            return ProviderAction(
                route="tool",
                tool_name="handoff_ticket",
                reason=phrase("outside_rule_handoff"),
            )
        return ProviderAction(route="decide", reason=phrase("no_further_read"))


def _refund_blocks(snapshot: RunSnapshot) -> bool:
    for item in reversed(snapshot.observations):
        if item.get("tool") == "list_refunds" and item.get("status") == "ok":
            rows = (item.get("data") or {}).get("refunds") or []
            return any(row.get("status") in {"pending", "succeeded"} for row in rows)
    return False
