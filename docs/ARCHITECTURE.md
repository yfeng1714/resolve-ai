# Planned architecture

Status: M1 and the M2 refund path are accepted locally as of 2026-10-18. The recorded
Luna campaign predates a small offline revision and login patch; that fingerprint was
not rewritten. Durable recovery is the next Portfolio slice. A second tenant
and Portfolio recovery are not implemented. The default process remains the fake provider.

```text
React workbench -> FastAPI (session, ticket, proposal, approval)
                 -> bounded LangGraph agent -> policy retrieval / read tool registry
                 -> deterministic policy + approval gateway
                 -> HTTP simulator -> operation query / independent verification
                 -> reviewed reply + audit events
PostgreSQL: app schema (tickets, runs, proposals, approvals, ledger, knowledge)
            simulator schema (accounts, invoices, payments, refunds; later subscriptions/incidents)
Portfolio: persisted worker + checkpoints + reconciliation + second tenant
```

## Module map (created in M1)

| Path | Responsibility |
| --- | --- |
| `src/resolveai/api/` | FastAPI routes, authenticated demo sessions, authorization |
| `src/resolveai/domain/` | Schemas, state transitions, integer money, policies |
| `src/resolveai/db/`, `migrations/` | App models, repositories and Alembic migrations |
| `src/resolveai/agent/` | Bounded graph, provider adapter, fake provider, observations |
| `src/resolveai/tools/` | Read registry, HTTP client, proposal execution gateway |
| `src/resolveai/simulator/` | Separate HTTP service and authoritative business records |
| `src/resolveai/retrieval/` | Import, evidence metadata and access-filtered crc32 token-bucket retrieval |
| `src/resolveai/evaluation/` | Runner/oracle/reporting; labels excluded from model context |
| `apps/web/` | React/Vite UI, typed API client and browser acceptance |
| `fixtures/`, `knowledge/`, `policies/` | Versioned synthetic business/policy data |
| `tests/` | Domain, HTTP/DB integration, failures and browser tests |

One Python project/lockfile serves API, simulator, agent and evaluation. Simulator
business writes remain HTTP-only from the app, even though both services may use
one local PostgreSQL server. Runtime roles have separate schema grants; only the
evaluation oracle may inspect simulator state directly. Do not duplicate the simulator
billing ledger as a second authoritative application ledger.

## Authority and writes

Server sessions provide tenant/operator/customer identity. Proposals bind action
type, target, payload hash/revision, resource and policy versions. Human approvals
refer to those exact proposals. Deterministic validation runs again before execution.
Simulator conditional writes close the read/check/write race and enforce balances.

Action ledger tracks attempted/pending/confirmed/unknown results. Stable business
action IDs survive new runs; unique keys plus payment/event balance constraints
prevent both retries and regenerated proposals from double refunding. Unknown
results are queried with the original action identity, not retried with a fresh key.
Checkpoint state never substitutes for business receipts.

## Core runtime

API-driven runs with short bounded tasks; manual waits save structured state and
return control. Keep ticket, run and per-action states separate. A process restart
may require manual reconciliation; persist attempts before HTTP side effects and
read the ledger/simulator before resuming. Budget: 12 model calls, 20 tools, two
replans, at most two read retries, plus ticket-level cumulative limits.

## Retrieval and isolation

Core: one policy snapshot, vector retrieval or another justified simple retrieval
path, version/effective-date checks and evidence links. Portfolio: historical/conflict
handling, PostgreSQL lexical + pgvector + RRF; every path checks access before retrieval.
Keep tenant-scoped identities from M1. Add the second tenant and exercised restricted
roles/RLS in M3; privileged initialization connections are never runtime connections.

## Observability

Link ticket -> run -> proposal -> action -> invocation -> simulator operation.
Record versions, observations, concise reasons, outcomes, tokens and latency with
redaction. No chain-of-thought storage. Readable business events come first; detailed
traces are authenticated. Portfolio adds distributed spans and recovery views.
