# Architecture decision records

M0 defaults are accepted for initial implementation, subject to documented evidence
and later user direction. Do not quietly replace them or add unrelated infrastructure.

- [0001 — Stack and staged runtime](0001-stack.md)
- [0002 — Approval and financial writes](0002-authorized-actions.md)
- [0003 — Authority and recovery](0003-authority-recovery.md)
- [0004 — Isolation and retrieval](0004-isolation-retrieval.md)
- [0005 — Agent capability and evaluation](0005-agent-evaluation.md)
- [0006 — Durable refund recovery](0006-durable-refund-recovery.md) (M3.1 implementation accepted locally on 2026-10-25)
- [0007 — Refund approval expiry and revocation](0007-approval-expiry-revocation.md) (M3.2 implementation accepted locally on 2026-10-26)
- [0008 — Two-tenant isolation](0008-two-tenant-isolation.md) (implementation accepted locally on 2026-10-28)
- [0009 — Period-end subscription cancellation](0009-period-end-cancellation.md) (accepted locally on 2026-11-02)
- [0010 — Incident correlation and handoff](0010-incident-correlation-handoff.md) (accepted locally on 2026-11-06)
- [0011 — Combined refund and period-end cancellation](0011-combined-refund-cancellation.md) (accepted locally on 2026-11-12)
- [0012 — Hybrid policy retrieval](0012-hybrid-retrieval.md) (M3.7 implementation accepted locally on 2026-11-25; M3 engineering complete)

Every change records problem, alternatives, choice, cost and reconsideration trigger.
