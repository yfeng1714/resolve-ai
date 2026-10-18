# ADR 0003: Authority and recovery

Status: Accepted for M1/M3. Date: 2026-10-12.

Problem: a timeout or crash can occur after a refund commits.

Alternatives: retry every failure; trust graph checkpoint; reconcile authoritative state.

Choice: simulator business ledger is authoritative; app approvals/action ledger are
authoritative for permission/attempt tracking. Persist attempts with stable action IDs
before executing. Enforce unique operation identity, same-key/same-payload checks and
payment refund balance constraints. Query unknown results by original identity.
Core permits visible manual reconciliation; M3 adds checkpoint, worker leasing,
outbox and automatic reconciliation with crash-window tests. No cross-system exactly-once claim.

Cost: uncertain states and recovery UI; checkpoints alone cannot resolve writes.

Reconsider: real connector semantics may require adjusted receipts/retry contracts;
preserve business deduplication and unknown-result handling.
