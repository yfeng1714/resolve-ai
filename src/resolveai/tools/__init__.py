"""Read tools and the HTTP client used to reach the simulator."""

READ_TOOLS = (
    "get_customer_account",
    "list_invoices",
    "list_payments",
    "list_refunds",
)
KNOWLEDGE_TOOLS = ("search_knowledge",)
WRITE_ADJACENT_TOOLS = (
    "propose_refund",
    "get_operation_status",
    "add_internal_note",
    "handoff_ticket",
    "draft_reply",
)
ALL_TOOLS = READ_TOOLS + KNOWLEDGE_TOOLS + WRITE_ADJACENT_TOOLS
