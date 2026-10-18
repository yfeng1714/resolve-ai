# ADR 0004: Isolation and retrieval

Status: Accepted for M1–M3. Date: 2026-10-12.

Problem: evidence can be stale or belong to another customer/tenant.

Alternatives: prompt filters; UI-only tenant switching; server-enforced filtering.

Choice: service-injected tenant/customer identity, ownership checks on each object,
tenant-aware data keys and authenticated evidence links from M1. Core has one tenant,
current versioned policies and simple RAG. Portfolio adds a second tenant, restricted
runtime DB roles/RLS validation, cache/trace isolation and lexical/vector RRF retrieval.
Hard financial policy comes from deterministic versioned rules, not document instructions.

Cost: metadata and scoped queries; full historical policy/import management is deferred.

Reconsider: measured retrieval failures justify reranking or another lexical strategy;
every replacement preserves filtering and evidence provenance.

Database image tag was checked against the [pgvector Docker documentation](https://github.com/pgvector/pgvector#docker).
