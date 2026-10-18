"""Operator-facing language for the workbench and drafted replies."""

from __future__ import annotations

from contextvars import ContextVar, Token

LOCALES = ("en", "zh-CN")

_locale: ContextVar[str] = ContextVar("resolveai_locale", default="en")

_EN = {
    "read_before_deciding": "Read {tool} before deciding.",
    "proposal_waiting": "A refund proposal is waiting for a human approver.",
    "handed_off_done": "The ticket was handed off without a refund.",
    "clarification_recorded": "Clarification was recorded and no refund was proposed.",
    "read_failed_handoff": "A required billing read failed, so the case is handed off.",
    "pending_no_refund": "A matching payment is still pending, so no refund is proposed.",
    "outside_rule_handoff": "The case is outside the duplicate-refund rule.",
    "evidence_complete": "Billing evidence is complete, so eligibility can be checked.",
    "look_up_policy": "Look up the current duplicate-refund policy before proposing.",
    "conflict_needs_policy": "Invoice evidence conflicts, so look up the policy before handoff.",
    "refunds_before_proposal": "Read existing refunds before proposing.",
    "no_further_read": "No further read is required.",
    "model_limit": "Model call limit reached",
    "tool_limit": "Tool call limit reached",
    "replan_limit": "Replan limit reached",
    "replanned": "Replanned from the latest observation.",
    "incomplete": "The run ended without a decision",
    "note_no_refund": "No refund proposed. Policy reasons: {reasons}.",
    "note_handoff": "Handed off without a refund. Policy reasons: {reasons}.",
    "reply_confirmed": (
        "Hello {customer_name},\n\n"
        "We confirmed a refund of {amount} USD for the duplicate payment{invoice}.\n"
        "Refund reference: {operation_id}.\n"
        "Payment {payment_id}, event {event_id}.\n"
    ),
    "reply_unknown": (
        "Hello {customer_name},\n\n"
        "The approved refund of {amount} USD is not confirmed. "
        "Action {action_id} was not submitted again.\n"
    ),
    "reply_rejected": "Hello {customer_name},\n\nNo refund was issued for this request.\n",
    "invoice_clause": " on invoice {invoice_number}",
    "reason.eligible_duplicate": "eligible duplicate payment",
    "reason.pending_payment_present": "a matching payment is still pending",
    "reason.no_duplicate_settled_payment": "no duplicate settled payment",
    "reason.refund_already_open_or_settled": "a refund is already open or settled",
    "reason.ambiguous_duplicates": "more than one duplicate pattern",
    "reason.non_usd": "a charge is not USD",
    "reason.amount_exceeds_policy_cap": "the amount exceeds the policy cap",
    "reason.outside_refund_window": "the payment is outside the refund window",
    "reason.invalid_amount": "the amount is invalid",
    "reason.target_not_eligible": "the requested payment is not the eligible duplicate",
    "reason.unsupported_case": "the case is not covered by the refund rule",
    "reason.conflicting_invoice_evidence": "the payments do not share one invoice",
}

_ZH = {
    "read_before_deciding": "决策前先读取 {tool}。",
    "proposal_waiting": "退款提案正在等待人工审批。",
    "handed_off_done": "工单已移交，未提出退款。",
    "clarification_recorded": "已记录需要澄清的事项，未提出退款。",
    "read_failed_handoff": "必要的账单读取失败，因此将工单移交。",
    "pending_no_refund": "有一笔对应付款仍在处理中，因此不提出退款。",
    "outside_rule_handoff": "该案例不在重复扣款退款规则范围内。",
    "evidence_complete": "账单证据已齐全，可以核对是否符合退款条件。",
    "look_up_policy": "提出退款前先查阅当前的重复扣款退款政策。",
    "conflict_needs_policy": "发票证据相互冲突，移交前先查阅对应政策。",
    "refunds_before_proposal": "提出退款前先读取已有退款。",
    "no_further_read": "不需要继续读取。",
    "model_limit": "已达到模型调用上限",
    "tool_limit": "已达到工具调用上限",
    "replan_limit": "已达到重新规划上限",
    "replanned": "根据最新观察重新规划。",
    "incomplete": "调查结束时尚未做出决定",
    "note_no_refund": "未提出退款。政策原因：{reasons}。",
    "note_handoff": "已移交，未退款。政策原因：{reasons}。",
    "reply_confirmed": (
        "{customer_name}，您好：\n\n"
        "我们已确认退还重复扣款 {amount} 美元{invoice}。\n"
        "退款参考号：{operation_id}。\n"
        "付款 {payment_id}，事件 {event_id}。\n"
    ),
    "reply_unknown": (
        "{customer_name}，您好：\n\n"
        "已批准的 {amount} 美元退款尚未确认。未再次提交操作 {action_id}。\n"
    ),
    "reply_rejected": "{customer_name}，您好：\n\n此请求未发放退款。\n",
    "invoice_clause": "（发票 {invoice_number}）",
    "reason.eligible_duplicate": "符合条件的重复付款",
    "reason.pending_payment_present": "有一笔对应付款仍在处理中",
    "reason.no_duplicate_settled_payment": "没有已结算的重复付款",
    "reason.refund_already_open_or_settled": "该事件已有未完成或已成功的退款",
    "reason.ambiguous_duplicates": "存在不止一种重复扣款情形",
    "reason.non_usd": "有一笔扣款不是美元",
    "reason.amount_exceeds_policy_cap": "金额超过政策上限",
    "reason.outside_refund_window": "付款超出退款时限",
    "reason.invalid_amount": "金额无效",
    "reason.target_not_eligible": "所请求的付款不是符合条件的重复扣款",
    "reason.unsupported_case": "该案例不在退款规则范围内",
    "reason.conflicting_invoice_evidence": "这些付款不属于同一张发票",
}

_CATALOG = {"en": _EN, "zh-CN": _ZH}


def normalize_locale(value: str | None) -> str:
    if not value:
        return "en"
    lowered = value.strip().lower()
    if lowered.startswith("zh"):
        return "zh-CN"
    return "en"


def get_locale() -> str:
    return _locale.get()


def push_locale(value: str | None) -> Token[str]:
    return _locale.set(normalize_locale(value))


def reset_locale(token: Token[str]) -> None:
    _locale.reset(token)


def phrase(key: str, **values: object) -> str:
    catalog = _CATALOG.get(get_locale(), _EN)
    template = catalog.get(key, _EN[key])
    return template.format(**values)


def phrase_reason(code: str) -> str:
    return phrase(f"reason.{code}") if f"reason.{code}" in _EN else code
