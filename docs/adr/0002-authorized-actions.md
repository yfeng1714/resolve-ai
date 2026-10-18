# ADR 0002: Authorized actions

Status: Accepted for M1. Date: 2026-10-12.

Problem: model text and customer requests cannot authorize financial writes.

Alternatives: prompt-only approval; direct model write tools; deterministic gateway.

Choice: server-generated proposals with immutable revisions/hash, target/amount,
policy and resource versions; authenticated human approve/reject; final server
permission/policy/version validation; conditional simulator execution. Replies also
require review. Amounts are USD integer cents. Approval does not extend to another action.

Cost: slower happy path and additional data models. Basic approvals and payment-level
deduplication are M1 requirements; full expiry/revocation lifecycle is M3.

Reconsider: explicit low-risk automation configuration after evaluated shadow mode;
never remove deterministic authorization or balance invariants.
