# Product scope and acceptance

Technical requirements, aligned with the locally maintained project brief v1.1
(2026-10-12). This document contains the project contract needed to work from a clone.

## Core MVP

One synthetic SaaS tenant, authenticated support/approver roles, billing tickets,
account/invoice/payment/refund HTTP tools, current policy retrieval with evidence,
an observation-driven LLM loop, refund proposals, authenticated approvals, simulator
execution, independent verification and reviewed simulated replies.

Refund demonstration policy: USD only, integer cents; duplicate settled payment,
within seven days, refundable amount at most $100, no successful or unresolved
refund for the same event. Deterministic code calculates eligibility and balance;
the LLM cannot grant approval or financial authority. Other cases clarify or hand off.

Core must persist tickets, proposals/revisions, approvals, action IDs/ledger and
tool observations. It must handle denied approvals, stale proposals, duplicate
requests and uncertain writes safely. Full automatic crash recovery can follow later.

### Adaptive tasks

Select at least two of these families, with paired observation variants:

1. Ambiguous billing complaint: pending payment, duplicate settlement or distinct
   invoices requires different follow-up queries or clarification.
2. Missing/conflicting evidence: the agent chooses which facts or policy to fetch,
   asks a necessary question, or hands off unsupported conclusions.
3. New observations: existing refund/unknown operation/customer clarification changes
   the next query or proposal without widening approval or duplicating a write.

The LLM chooses permitted read tools and next steps based on observations; it cannot
merely fill parameters for a fully predetermined sequence. Fixed safety stages remain.
Record observations, actual calls, concise reasons and final state; never private reasoning.

### Core completion

- One browser-operated refund workflow with confirmed simulator state and reviewed reply.
- Real LLM + RAG + adaptive tools in at least two task families; fake provider for CI.
- 24 labeled smoke cases pass in a recorded real-model acceptance batch, preserving
  failures/reruns. At least 12 cover adaptive families, at least four per selected family.
- Deterministic regressions prove no unapproved writes/sends, role or account violations,
  stale execution, duplicate refunds, blind unknown-result retries or false success claims.
- B1 fixed-workflow prototype shares tools/fixtures/safety and reads observations competently.
- Local reproduction and known limitations documented; no production claims.

## Portfolio MVP

Add period-end cancellation (never immediate termination), subscription approval/version
checks, incident correlation and handoff (no invented ETA or compensation), multi-intent
actions, two-tenant isolation, full approval expiry/revocation, hybrid retrieval,
durable checkpoint/worker and reconciliation. Keep separate per-action results for partial success.

Required recovery tests include concurrent/repeated writes, response lost after simulator
commit, crash before checkpoint, stale/revoked approval, conditional writes, cross-tenant
access, untrusted document/tool text, finite retries/budgets, partial success and unsent drafts.

Evaluation: 240 labeled synthetic scenarios in eight categories (policy, refunds,
subscriptions, incidents, ambiguity, conflicts, security, recovery); 80 development,
160 frozen test cases, three runs per frozen case. Group near-duplicate templates to
prevent leakage. Smoke cases may be reused only in the development allocation.

Targets, not results: >=85% overall E2E success, >=75% each category, >=95% citation
support precision, >=90% citation coverage, zero observed critical violations, all
defined recovery/security regressions passing. Report per-case outcomes and grouped
uncertainty; zero observed violations does not establish zero risk.

Provisional performance targets: p95 automatic processing <=60s, average estimated
cost <=$0.20, excluding but separately reporting human waiting. Confirm targets before
freezing on at least 100 measured runs, concurrency 1 and 5, with environment metadata.

B0 RAG reply assistant is compared only on relevant reply tasks; B1 fixed workflow
and B2 bounded agent share tools, data, safety and budget caps. Perform at least three
ablations: hybrid vs vector-only retrieval, no retrieved context, no dynamic replanning.
Use 40 preselected challenge cases, three runs/configuration; change one factor at a time.

Agent benefit is a separate target: meaningful improvement in at least two preregistered
task families, in success or efficiency without sacrificing safety/quality. Define
metrics and minimum worthwhile differences on development data before freezing.
If benefits are not established, mark that target unmet and keep useful fixed workflows.

Current delivery preference (2026-10-12): preserve sanitized reproducible evidence and
a recorded local demo with local reproduction. Public hosting or a live public demo
is not required for now; later public sharing is a separate owner decision. Clearly
distinguish local, published and deployed status.

## Deferred / excluded

Real sandbox connector, selective automation, reranking/model cascades, long-term
memory, multiple agents and MCP/Temporal adapters are stretch goals after Portfolio.
No real customer data, payments, email, arbitrary shell/SQL/URLs or unrestricted
financial autonomy. The provisional Core model is `gpt-6-luna` at low reasoning effort.
The default process stays on the fake provider until the owner starts a live process
with an explicit model, credential and positive budget.
