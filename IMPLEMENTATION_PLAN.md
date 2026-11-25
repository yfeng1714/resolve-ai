# ResolveAI implementation plan

Version: M1 / 2026-10-12. Scope: staged implementation, not permission to run every
milestone. Requirements: [product scope](docs/PRODUCT_SCOPE.md). Decisions:
[ADRs](docs/adr/README.md).

M1, the fake-provider refund slice, is implemented locally and was accepted through the M1.3 review.
M2 local work now includes current-policy retrieval, observation-driven tool choice for two
billing families, a B1 workflow, an executable 24-case development catalog, and a
provisional Luna live path. The 2026-10-17 pilot selected `gpt-6-luna` at low reasoning
effort. The repaired campaign in `docs/evaluation/M2_REPORT.md` passed 24 of 24
`gpt-6-luna` cases on the first pass and recorded a live browser approve/reject demo.
Application-ledger spend for that campaign, including the browser investigations, was
$0.013109. The earlier 2026-10-17 batch remains historical. A later offline patch
corrects investigation revisions and the login commit boundary. It was verified
offline and does not replace the recorded campaign. M2 was accepted on 2026-10-18
as the local Core MVP.
M3.1 one-tenant refund recovery was accepted locally on 2026-10-25. After admission
it only queries the original action, and an admitted command that was never sent may
stay blocked. M3.2 approval expiry and revocation was accepted locally on 2026-10-26.
M3.3 two-tenant isolation was accepted locally on 2026-10-28.
M3.4 period-end cancellation was accepted locally on 2026-11-02.
M3.5 incident correlation was accepted locally on 2026-11-06 for that bounded slice.
It is not acceptance of all M3 or the Portfolio. M3.6 same-ticket refund and
period-end cancellation was accepted locally on 2026-11-12. ADR 0012 hybrid
retrieval was accepted locally on 2026-11-25, closing the M3 engineering milestone.
M4 evaluation and M5 delivery remain pending.
Fake-provider checks remain the development and CI default.

## 1. Repository audit and constraints

- The existing GitHub repository was empty: no commits, application, authentication,
  database migrations, tests, CI or deployment to reuse. The checkout is the repository
  root; the surrounding local coordination workspace must never be uploaded.
- Default branch is `main`. Preserve the remote's existing public visibility.
- M0 adds plans, ADRs, runtime declarations, dependency locks and local database config.
  It does not add application endpoints, a frontend, business fixtures or app tests.
  M1 adds those pieces for the fake refund slice only.
- Backend baseline: Python 3.12/uv, FastAPI/Pydantic, SQLAlchemy/Alembic, psycopg,
  LangGraph and pgvector. Frontend baseline: React/TypeScript/Vite, Node 24/pnpm.
  Exact backend versions are resolved in uv.lock; web dependencies are locked in M1.
- Docker Compose provides a dedicated localhost-only PostgreSQL 18/pgvector database.
  Do not use or modify an unrelated local PostgreSQL server. Initializer credentials
  are demo-only and must not become unrestricted application credentials.
- Development/CI defaults: fake provider, synthetic data, no real external connector.
  USD integer cents, one tenant in Core, reviewed financial actions and customer replies.
- No existing app checks can establish a baseline; M0 checks are dependency imports,
  lock consistency, Compose config, local database connectivity when available, Git
  boundary and remote-tree verification. App tests start in M1.

## 2. Decisions and open questions

Accepted defaults: single bounded logical agent; server-controlled safety stages;
dynamic read-tool selection; stable action ledger; separate HTTP simulator; one
Python project; one database server with separate app/simulator schemas and roles.

| Item | Default / next decision | Blocking scope |
| --- | --- | --- |
| Model and embedding provider | Fake until owner chooses; provider adapter supports one live model initially | Real-model M2 acceptance, not M1 fake development |
| Paid usage | Hard default $0; estimate then configure explicit aggregate cap | Any billable provider request |
| Available time | Dependency-based slices; 6–8 weeks is a rough hypothesis | Calendar commitments, not local setup |
| Hosting / demo | Recorded local tests and demo; no public hosting or live public demo needed for now | Any later deployment or public sharing decision |
| License | No license selected; owner chooses before granting reuse rights | License publication, not local implementation |
| Real integrations / customer data | Excluded; synthetic HTTP simulator | Any later expansion requires separate scope decision |

Do not defer routine implementation choices while waiting for these answers. Record
new decisions in an ADR and update this plan if they change architecture or acceptance.

Owner preference recorded 2026-10-12: begin with small-to-medium local trials and
preserve configurations, test results, sanitized traces, business-state evidence and
browser recordings. Consider lower-cost providers such as DeepSeek in a later measured
comparison; no provider has been selected and no paid usage is authorized. M1 remains
fake-model development. Revisit full Portfolio experiment scale/budget explicitly
after Core measurements; the early-trial preference does not waive held-out evaluation.

## 3. Core vs Portfolio vs stretch

Core (M1–M2): billing/refund + policy RAG + at least two adaptive task families,
approval/rejection, verified simulator execution, reviewed replies, 24 smoke cases,
B1 prototype and local demo. Basic financial safety starts with the first write.

Portfolio (M3–M5): subscriptions/incidents and multi-action results, second tenant,
complete approval lifecycle, worker/checkpoint/reconciliation, hybrid retrieval,
240-case evaluation, B0/B1/B2, three ablations and a recorded local demonstration;
public hosting is optional under the current delivery preference.

Stretch: one real sandbox connector, selective low-risk automation, reranker/cascade,
long-term memory, multi-agent or MCP/Temporal only after measured need.

Do not hold Core behind full Portfolio infrastructure. When time is limited, defer
stretch, visual dashboards and new domains; preserve approval, deduplication, ownership,
verified outcomes and the promised real-model Core evidence.

## 4. Data model and authority

M1 app schema: tenant, user/membership/session, ticket, message, run, proposal,
approval, action_ledger, tool_invocation and audit_event. M2 adds knowledge_document,
chunk and policy metadata. Simulator schema: customer identity/account, invoice,
payment, refund and operation. M3 adds subscriptions/incidents, task leases,
checkpoint/outbox and full lifecycle history.

- All resource relationships carry tenant identity; resolve customer from trusted
  channel mapping, never from model-supplied tenant or role fields.
- Store money as integer cents with USD enum and nonnegative/balance constraints.
- Proposals have immutable revision, canonical payload hash, target, policy version,
  resource preconditions and linked evidence. Editing creates a new proposal revision.
- Approvals bind exact revision/hash and authorized human. M1 revalidates actor,
  relevant business state and policy at execution; M3 adds expiry/revocation coverage.
- Unique `(tenant_id, action_id)` and simulator operation keys enforce same-identity
  retries; same key/different payload is rejected. Payment/event-level refundable
  balance and pending-refund constraints stop duplicate effects under new proposal IDs.
- Persist attempted action before HTTP write. Simulator business records determine
  actual financial state; app action ledger determines recorded attempts. Inspect both.
- API runtime cannot directly mutate simulator tables. Migrations and eval oracle use
  dedicated privileges; never use initializer/superuser role in runtime code.

Use Alembic revision files and reproducible seed commands. Seed/reset operates only
on explicitly selected ResolveAI demo databases, with an environment guard.

## 5. API, tools and authorization

Proposed API surface (final schemas implemented and checked in M1):

| Route | Purpose / boundary |
| --- | --- |
| `GET /health` | Service health, no sensitive data |
| `POST /sessions`, `DELETE /sessions/current` | Synthetic demo login/logout; HttpOnly session, no client-selected authority |
| `GET /tickets`, `GET /tickets/{id}` | Authenticated, tenant and object scoped |
| `POST /tickets/{id}/messages`, `POST /tickets/{id}/runs` | Append input / start bounded processing |
| `GET /runs/{id}` | State, observations, per-action outcomes; authorized trace access |
| `POST /proposals/{id}/approve`, `/reject` | Human session, role and exact revision; agent has no approval tool |
| `POST /proposals/{id}/execute` | Gateway validates approved payload and preconditions; no arbitrary financial arguments |
| `POST /reply-drafts/{id}/approve-and-send` | Bind recipient/body revision; stable send identity and receipt |
| `GET /evidence/{id}` | M2 authenticated citation source/version |

Cookie-based writes require CSRF protection and origin checks. Permit only the
configured web origin. Hash synthetic demo user passwords; do not put role/identity
claims in customer ticket text or accept caller-selected unrestricted tenant headers.

Core tools: get_customer_account, list_invoices, list_payments, list_refunds,
search_knowledge, propose_refund, get_operation_status, add_internal_note,
handoff_ticket, draft_reply. M1 omits unimplemented retrieval tools; M2 registers them.
The gateway, rather than the free-form model tool loop, executes approved refund/send.

Use strict Pydantic schemas: reject unknown keys, invalid IDs/enums/currency/amounts.
Inject session authority server-side. Output: status, data, error_code, retryable,
operation_id, resource_version, observed_at, request_id. Distinguish denied, stale,
rate-limited, temporary failure and unknown outcome. Fixed simulator URL; no arbitrary
URL, SQL, shell, recipients or approval tools.

Simulator HTTP endpoints must independently authorize resource scope and implement
conditional refund writes, operation lookup and an idempotency contract. Fault injection
is fixture/test configuration outside model context, not a tool the agent can activate.

## 6. Agent state and policy execution

Ticket state differs from run state. Proposed run states: analyzing, gathering_evidence,
proposing, awaiting_customer, awaiting_approval, executing, verifying,
awaiting_reply_review, completed, handed_off, failed, cancelled, reconciling.
Per-action state: not_started, pending, confirmed, rejected, unknown. Sending a reply
does not by itself resolve a ticket.

Run state includes server identity, message revision, observations and their timestamps,
evidence/version IDs, proposal revision/preconditions, approvals, action/operation IDs,
provider/prompt versions, budget usage and errors. Persist key facts; requery mutable
financial data before execution. No private chain-of-thought storage.

LangGraph outer stages are deterministic; read-tool loop allows model-selected queries
and finite replanning. M1 fake provider is for repeatable integration only. M2 live
provider must choose tools from observations in at least two paired task families.
Limits: 12 model calls, 20 tools, two replans, up to two read retries, cumulative ticket
limits. Manual waits release processing. Unknown write -> query original operation or
safe handoff, never fresh-key retry. Core recovery is explicit/manual; M3 durable.

## 7. Retrieval and evidence

M2 imports synthetic English Markdown/JSON policies with tenant/access scope, document
hash, current version, effective dates, title and source location. Start with single
vector retrieval and pgvector; evaluate any simpler existing alternative explicitly.
Embedding adapter is fake for CI and owner-configured for live retrieval.

Chunk by headings/paragraphs; retain source offsets. Scope and validity filters precede
candidate search. Return evidence ID, exact source snippet, version/date and protected
link. Map claims to supporting evidence; missing/conflicting policy prevents unsupported
claims/writes. Hard refund rules stay deterministic even when retrieval is absent.

M3 adds lexical/vector RRF and historical policy cases. Tune only on development data.
Reranking is stretch; every path/cached result preserves access and invalidation rules.

## 8. Evaluation and budget plan

Case schema: case_id, task_family/category, split, fixture/policy versions, initial
state, messages, required evidence, allowed outcomes, expected/forbidden actions,
external approval script, fault schedule and final-state assertions. Labels and oracle
answers never enter model context/retrieval. Reset independently per run.

Core 24-case allocation proposal: 12 paired adaptive variants (choose two families,
six variants each), four normal/policy/citation cases, four approval/identity/staleness
cases, four deduplication/unknown-result/unsent-reply/budget cases. A case may exercise
multiple assertions; deterministic regressions separately enumerate sensitive edges.
Select distinct families from ambiguous billing, missing/conflicting evidence, and
new observations/clarification. Record why observations changed actual tool choices.

Build B1 in M2 with competent deterministic observation branches. Do not degrade it
to a fixed list that ignores all new evidence. Share simulator, retrieval, hard rules,
approval scripts and budget caps with B2. Core comparison is developmental, not proof.

M4: 80 dev + 160 frozen cases, eight categories, three runs/frozen case, grouped template
split; meet targets in PRODUCT_SCOPE.md or clearly mark unmet ones. Prespecify at least
two adaptive task-family benefit metrics/minimum differences, report per-family results
and uncertainty. Deterministic checks grade money/side effects/permissions/state;
rubric/model-assisted language grading requires at least 20 manually audited samples.

Three required ablations use 40 fixed challenge cases x 3 runs: vector-only retrieval,
no retrieved context, no dynamic replanning. No safety boundary is removed. Raw results:
`eval/results/<run_id>/{results.jsonl,summary.json,REPORT.md}` (ignored); curated sanitized
reports/configs go into tracked `docs/evaluation/`. Save all failures and versions.

Budget worksheet (estimates, not authorization or measured unit prices):

| Batch | Illustrative run count | At assumed $0.20/run |
| --- | --- | --- |
| Core live B2 smoke + B1 development + six demos | 24 + 24 + 6 = 54 | $10.80 |
| Portfolio B2 frozen | 160 x 3 = 480 | $96 |
| Portfolio B1 frozen | 160 x 3 = 480 | $96 |
| B0 policy-only comparison | 20 x 3 = 60 | $12 |
| Three ablations | 3 x 40 x 3 = 360 | $72 |
| Portfolio subtotal | 1,380 | $276 |

Actual costs depend on token/tool depth and provider; B0 can have a different cost.
This excludes development, embeddings, judge calls, additional benchmark runs, retries
and hosting. Thus the brief's $96 main-system estimate is not a complete experimental
budget. Before live calls: choose provider/model, use current official pricing, measure
small sample within an explicit cap, estimate every batch, reserve headroom, enforce
aggregate pre-call admission/accounting and support resumable batches. Default budget $0.

## 9. Milestones and dependency order

| Milestone | Main paths | Exit evidence |
| --- | --- | --- |
| M0 setup/plan | README, plan, docs/adr, manifests/locks, Compose | Audited empty baseline, reproducible environment checks, private/public boundary, remote connection |
| M1 refund vertical slice | src/resolveai/{api,domain,db,tools,simulator,agent}, migrations, fixtures, apps/web, tests | UI approve/reject; exactly one confirmed refund; no denied effects; reviewed reply; fake regression |
| M2 Core | retrieval, knowledge/policies, providers, evaluation, B1, smoke cases | Real LLM + RAG; >=2 adaptive families; 24 cases; local demo and short report |
| M3 Portfolio breadth/recovery | subscriptions/incidents, worker/checkpoint/outbox, roles/RLS, hybrid retrieval | Business paths + full safety/crash/tenant suite; browser reconciliation and partial success |
| M4 experiments | frozen datasets, runner, reports, baselines/ablations | Reproducible raw/aggregate evidence, separately assessed agent benefit, costs and limitations |
| M5 delivery | README, architecture/ADRs, demo script/video; hosting only if later requested | Clean-clone reproduction, recorded local demonstration, truthful project evidence |

Each slice updates plan/status and runs targeted checks. Preserve the Core/Portfolio
exit checklists in PRODUCT_SCOPE.md; don't declare completed functionality from mocks.

## 9.1 M3.4 period-end cancellation

The sequence below is implemented locally from ADR 0009 and was accepted locally on 2026-11-02.

1. Add `simulator.subscription` and the period-end schedule endpoint. The
   operation identity is the action id plus the effect hash. The resource
   version and `current_period_end > clock_timestamp()` are write guards.
   Prove replay, payload conflict, version conflict, `already_scheduled`, and
   `period_elapsed` without a second version bump. Do not add a termination
   or renewal endpoint.
2. Make the refund payment fields and `recovery_job.payment_resource_version`
   nullable, and add the subscription columns from ADR 0009. Enforce the
   mutually exclusive shapes and the command trigger. Do not insert dummy
   payment values.
3. Add `list_subscriptions` and the append-only subscription choice. Implement
   the decision table in both the fake provider and B1, including the resume
   after a valid choice. Record those tool sequences as development traces.
4. Enqueue through the unsent-replacement rules, the current grant, and the
   existing expiry and revocation rules. Extend `process_available` only when
   the ledger type and the command type agree. Keep `claim_recovery_job()`
   unchanged. After admission, restart work is query-only.
5. Show a proposed period end before confirmation, and "Cancellation
   scheduled" only when this command's own operation matches. Add browser
   approve, reject, clarification, and one stale or unknown case on new
   tickets.
6. Refuse downgrade while any subscription choice or cancellation-shaped app
   row exists, and prove the row hashes do not change. From a refund-only
   database, downgrade and reapply while preserving refund history.

Acceptance tests, all on the fake provider and `resolveai_pytest`:

- Tenants A and B each complete one approved future schedule. The subscription
  stays `active`, the flag is true, the effective time equals that row's
  period end, and the version increases once. Refund and payment counts stay
  at their pre-test values.
- A version change before enqueue, and another after enqueue but before
  admission, each keep the old attempt immutable, avoid a POST of the stale
  command, and allow one new unsent attempt with the same effect hash. A
  regenerated same-version proposal does not create a second operation.
  Regeneration after admission does not POST. An old grant and a new grant
  cannot both admit.
- Reject, expired grant, revoked grant, a withdrawn input, a guessed
  subscription id, and a cross-account choice leave the other tenant unchanged
  and create no operation.
- Two unscheduled subscriptions, or a mix of scheduled and unscheduled, wait
  without a proposal. A valid stored choice then schedules only that owned
  target. An already scheduled set explains every time and does not write.
  An invalid, stale, or cross-account choice does not propose.
- "Terminate immediately" creates no operation. A later explicit period-end
  choice can be proposed and still requires its own approval.
- Approval that waits past the period end, and a POST attempted after the
  period end, write nothing. A write committed before that instant still
  confirms on a later query. A flag set by another operation does not confirm
  this action id.
- Kill the worker after admission and again after the simulator write. The
  replacement process reads the original action. The subscription and
  operation rows, not the dead process's HTTP response, decide the outcome.
- The maintained refund, tenant, grant, and recovery suites stay green.

B1 must follow the same choice and resume path. That is a competence check,
not a claim that one agent is better. Do not start the 240-case evaluation
from this slice.

## 9.2 M3.5 incident correlation and handoff, accepted locally

ADR 0010 was accepted locally on 2026-11-06 for the bounded incident slice.
The worker still has no incident command. The checks below are the accepted
contract. Migration `20261106_0011` enforces the confirmed-evidence shape on
new writes and does not retrospectively validate older handoff rows. The local
slice does the following:

1. Add account-scoped `simulator.incident` rows and `GET /v1/accounts/{account_id}/incidents`.
   The read uses the ticket's bound account. Prove a cross-account id and a
   cross-tenant id return no row. Do not add a write, an ETA, or a credit field.
2. Add append-only `app.incident_report` and `app.incident_handoff`, with forced
   row-level security, tenant-aware foreign keys, and no runtime update or delete.
   Downgrade refuses while either table has a row. An empty database drops only
   those objects and `simulator.incident`.
3. Implement the ordered classifier in the server proposal and handoff path,
   and call that same classifier from the fake provider and B1. A missing
   service, region, or start waits for a complete support report. Compensation
   after a complete report outranks a record match and creates no financial
   proposal. A forced refund or cancellation call on an incident ticket is
   refused in the server function.
4. Persist the handoff from the validation snapshot, at most three account
   list reads, with the tool invocation, digest, and candidate evidence
   attached. Create a new incident reply draft bound to that run and input
   revision. Do not reuse a same-body draft from another run. Do not call the
   recovery worker. Admitted commands stay unchanged.
5. Show a known incident, a clarification resume, an unavailable read, and
   the human reply review in the browser. A newer report before send removes
   the unsent draft and allows another investigation. Record tool sequences
   for a record match, a missing start, no overlap, and a changed snapshot
   as development traces.

Acceptance tests, all on the fake provider and `resolveai_pytest`. These are
planned checks, not measured results:

- Each tenant sees only its own incidents. One full overlap records that
  incident id and version and says the match is not proof of cause. A region
  or service mismatch, two full overlaps, no rows, a resolved time equal to
  the report start, and a request id that is not listed each have the kind
  named in ADR 0010. None of those sets `incident_id` except the single full
  overlap.
- Missing fields produce no handoff, including when the text also asks for
  compensation. After a complete report, compensation produces a handoff and
  no refund, credit, or cancellation operation. A forced provider call on
  that ticket also creates no financial proposal. A refund ticket without
  the incident or compensation phrase still refunds. Refund and subscription
  rows on the incident tickets stay at their pre-test counts.
- A later complete report resumes. A newer report or ordinary message
  committed before send supersedes the unsent incident draft and allows a new
  run. The same reply text does not reuse the old draft. A send that commits
  first keeps its receipt, and later input creates a new current handoff.
  An admitted refund or cancellation command is not edited.
- An unavailable read says the result is unknown and stores no incident id.
  A new incident that appears while an older row's version is unchanged is
  reclassified from the whole snapshot. More than three differing reads, or
  a failed validation read, stores `conflict` or `unavailable` rather than a
  confirmed id.
- B1 follows the same observations. That is a competence check, not an
  agent comparison. Do not start the 240-case evaluation from this slice.

## 9.3 M3.6 combined refund and period-end cancellation, accepted locally

ADR 0011 was accepted locally on 2026-11-12. That acceptance does not close
M3 and does not accept hybrid retrieval. The ordinary combined family uses
the existing refund and period-end gateways. Incident and compensation
tickets still open neither financial action. A same-proposal retry after a
transient pre-admission read failure requires unchanged input and a live
exact grant. New input does not inherit the old approval.

What the accepted slices already prove, and what they do not:

| Requirement | Current evidence | Gap |
| --- | --- | --- |
| One duplicate refund, its own approval, action id `refund:{payment_id}`, and query-only recovery after admission | Refund, recovery, and combined tests | The refund half stays on that action id after admission |
| One period-end cancellation, its own approval, action id `cancel_at_period_end:{subscription_id}:{effective_at}`, and its own command binding | Cancellation tests and ADR 0009 | The cancellation half uses the same period-end command |
| Refund and cancellation phrases together | ADR 0011 and `src/resolveai/services/combined.py` | Accepted locally on 2026-11-12. An immediate phrase still waits for a later period-end choice |
| Independent grants, stale input, and revocation | Approval tests and the combined grant checks | Each proposal has its own grant. Ordinary tickets still show one current proposal; a combined ticket shows each action |
| Incident or compensation text | ADR 0010 | Both financial proposals stay refused |
| Two proposals of different action ids | Combined half and reply rows | Each half has its own outcome version. A confirmed or admitted effect is kept when the ticket is investigated again |

The proposed family is one eligible duplicate refund and one future unscheduled
subscription on the same account. Each half uses the gateway it already has.
Approving or settling one does not approve, settle, reverse, or resend the
other. A confirmed receipt stays. An admitted action that is not yet confirmed
stays on its original action id and is queried, not described as a definite
miss. ADR 0011 records each outcome as a new immutable version. The current
version is the greatest version for that input revision and half. A reply
binds to those two version ids. A pre-admission unavailable block is
`not_dispatched`: the attempt was not sent, and the customer is not called
ineligible. A confirmed section quotes that action's ledger receipt. An
immediate phrase inside the refund request leaves cancellation waiting for a
later period-end choice.

## 9.4 M3.7 hybrid policy retrieval, accepted locally

ADR 0012 was accepted locally on 2026-11-25. This acceptance does not authorize
a hosted embedding model or the M4 ablation. `search_knowledge`
filters tenant, current version, access scope, and effective dates before either
branch is ranked. The vector branch is pgvector distance over the 32-dimensional
crc32 token buckets. The lexical branch keeps a row only when the English
`tsvector` matches `websearch_to_tsquery`. Hybrid is the default and fuses the
two limited lists with reciprocal rank fusion, `k = 60`. Vector-only and
lexical-only use the same function and the same corpus. Ties break on
`(source_path, document version, chunk ordinal)` in Unicode code-point order
for the two text fields, and in numeric order for the ordinal. PostgreSQL
uses `COLLATE "C"` so that order matches fusion. The runtime chunk id is not
a tie key.

The development catalog in `policies/retrieval_catalog.json` records honest hits
and misses for a rare term and a paraphrase. It is not a measurement of a hosted
semantic model and it is not an agent-benefit result.

## 9.5 M3 exit and M4 entry

M3.1 through M3.5 are accepted locally for their own slices. M3.6 same-ticket
refund and period-end cancellation was accepted locally on 2026-11-12. M3.7
hybrid retrieval was accepted locally on 2026-11-25, closing M3. Development
checks cover hybrid, vector-only, and lexical-only modes. The acceptance run
passed 253 maintained Python tests, 141 distinct independent backend cases,
23 browser cases, and 15 grouped database/business/citation checks. These
used the fake provider and zero budget; they are not M4 evaluation results.

These local-demo limits remain after M3 closure. They are not M4 work by
themselves: post-admission recovery is query-only; an admitted command that
was never sent may stay blocked; migration `20261106_0011` does not certify
older handoffs; the simulator bearer token is outside row-level security;
incident reads do not pay a credit or promise a repair time; immediate
termination stays unavailable; the crc32 embedding is not live semantic
retrieval; public hosting is optional.

M4 does not start from this design. It needs its own frozen-set plan and an
owner spending limit. B0, B1, and B2 would share tools, fixtures, safety
rules, the retrieval mode flag, and budget caps. The three ablations change
one factor each: hybrid versus vector-only, no retrieved context, and no
dynamic replanning. A safety regression is not an agent-benefit result.
Deterministic money and permission checks stay separate from any later
comparison of agents.

## 10. Executable first-stage work list

1. Verify branch/changes, read decisions, run `make env`, `make check-setup`, then
   `make db-up` against the dedicated database. Confirm pgvector/version and isolation.
2. Create Python module tree and two FastAPI entrypoints; configure explicit settings
   with fake provider and zero-budget enforcement. Add sanitized health endpoints.
3. Add Alembic initial migration for app/simulator schemas, restricted runtime roles,
   identity/ticket/message/proposal/approval/action tables and simulator billing tables.
4. Seed one tenant, two support/approval roles, one duplicate-settlement refund case,
   distinct-invoice/pending/existing-refund variants, and versioned hard refund policy.
5. Implement simulator read endpoints + conditional refund + operation lookup; test
   same action twice/concurrently, same key/different payload and business-level dedup.
6. Implement trusted demo sessions, authorization and CSRF; test denied/guessed IDs
   and separate approval authority without relying on frontend behavior.
7. Implement strict read-tool contracts and proposal/gateway service; bind exact
   revision/hash, revalidate policy/versions, persist action before write, verify receipt.
8. Add fake provider + bounded run graph and saved waiting states; test approve/reject,
   edited/stale proposal, uncertain outcome, finite budget and accurately drafted reply.
9. Scaffold React/TypeScript/Vite in apps/web with pnpm; lock dependencies. Add ticket,
   evidence/observations, proposal approve/reject and per-action/result/reply-review UI.
10. Add independent integration oracle and Playwright M1 acceptance; check DB state
    and simulator receipts, not only API responses. Add CI for lint/tests and isolated DB.
11. Demonstrate one approved refund and one rejection from browser; preserve commands,
    relevant commit and sanitized evidence. Update README/plan with actual launch commands.
12. Stop at the requested milestone and submit review evidence before broadening scope.

## 11. Manager review and release boundaries

For each review: commit/branch + changed modules, exact checks and outcomes, independent
business effects, browser evidence, representative failure, versions/costs and limitations.
The manager assesses correctness first, then agent evidence and scope; findings distinguish
verified defect, design assumption, environment issue and unverified risk.

M1 does not imply Core acceptance; fake results do not establish model capability.
Critical permission/duplicate-effect/false-completion failures block release. Deployment
needs authentication, rate limits, resettable isolated data, secret handling and explicit
cost/publication authorization. Local, pushed, deployed and published states are separate.
