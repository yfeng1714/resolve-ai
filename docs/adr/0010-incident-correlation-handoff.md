# ADR 0010: Incident correlation and evidence-backed handoff

Status: Accepted locally on 2026-11-06 for the bounded incident slice. This is
not acceptance of all M3 or the Portfolio. The slice adds a read, a
clarification, and a local handoff beside the accepted refund and period-end
paths. It does not add a financial action, immediate termination, automatic
repair, hybrid retrieval, or a paid evaluation. Migration `20261106_0011`
checks new confirmed handoffs and does not retrospectively validate older rows.

Problem: a customer can report API failures and ask for compensation. The
current handoff is an internal note and, for billing, an audit row whose
reasons come from the refund decision. It does not store an incident id,
an observation, or the difference between a confirmed record and a possible
explanation. There is no incident read. A missing fact can be filled by the
model, and an empty result can be described as a healthy service.

Alternatives: page a real on-call system; share one incident catalog across
tenants; let the model name the account, the incident, or the repair time;
pay a credit through the refund command; treat "no rows" as proof the service
is healthy; add a second worker to deliver the handoff.

Choice: incident facts are account-private simulator rows. The server reads
them only for the ticket's bound account. A confirmed correlation requires
one owned incident that matches service, region, and reported start. Anything
weaker is labeled possible, several, none, or unknown. Compensation language
creates a handoff for a person and does not create a refund, a credit, or a
cancellation. The handoff is an append-only row plus a reply draft that a
human still reviews. No repair time is stored or spoken.

## What exists today

The refund and cancellation handoff is unchanged. `handoff_ticket` on those
paths appends an internal message and does not name an incident. The incident
path is separate: a support report, an account-scoped list, and one local
handoff with a human-reviewed reply. It still does not write a refund, a
credit, or a cancellation.

Customer and operator messages already advance `input_revision`.
`invalidate_pending_intent` supersedes an unsent proposal and cancels a
waiting run. `cancel_pre_admission` fences an unsent recovery job and leaves
an admitted command unchanged. Reply drafts still require human review before
the simulated send. Those mechanisms stay. This slice does not put an incident
on the recovery worker.

## Simulator contract

`simulator.incident` is scoped to one account, like a subscription. Columns:

| Column | Meaning |
| --- | --- |
| `id` | Stable incident id. |
| `account_id` | The only account allowed to read the row. |
| `service_code` | `api`, `billing`, or `auth`. |
| `region_code` | `us-east` or `eu-west`. |
| `status` | `investigating`, `identified`, or `resolved`. |
| `started_at` | UTC `timestamptz`, microsecond precision. |
| `resolved_at` | Null while open. Same timestamp form when set. |
| `summary` | Short synthetic description. Not a log. |
| `reference_ids` | This account's synthetic request ids. Empty is allowed. |
| `resource_version` | Positive integer. A change bumps it. |
| `created_at` | Insert time. |

There is no ETA column and no credit amount. The only new read is
`GET /v1/accounts/{account_id}/incidents`. The simulator rejects a path
account that does not match the request scope. The application calls it only
after the ticket's bound account is checked. A client-supplied account id,
incident id, or tenant id is not a scope.

Two tenants can describe similar symptoms. They do not share rows. Harbor
cannot read a Northwind incident id, and the reverse is also true. The shared
simulator bearer token remains a local-demo limit; the account path is the
boundary this slice enforces.

Timestamps in payloads use the existing canonical form
`YYYY-MM-DDTHH:MM:SS.ffffffZ`.

## Reported input

The model does not write the report. Authenticated support records a complete
`app.incident_report` under the ticket lock, with the same origin and CSRF
checks as a subscription choice. The body has no account id. The server copies
the ticket's tenant, account, and customer.

A stored report requires `service_code`, `region_code`, and
`reported_started_at`. Those fields are not nullable. Service and region must
belong to the allowlists above. The start must be the canonical instant.
`request_id` is optional. When it is present it is 1 to 64 characters from
`A-Za-z0-9_-`; when it is absent the match does not require one. Unknown
codes, a missing required field, and a malformed time are rejected, and no
report row is written.

The customer message can start the incident path, but it is not a report.
Until support submits a complete report, the run waits. It does not insert a
partial report and it does not name an incident.

The insert appends an operator message, "An incident report was recorded.",
and stores that message's revision on the report. The message is display. It
is not parsed for a service or a region. The current report is the row with
the greatest `input_revision`, then the greatest id. The run separately stores
the customer/operator revision it investigated. Those two numbers are equal
when the report message is the latest input, and they are both copied onto the
handoff.

Customer text may contain the symptom. Fixed phrases, not a model classifier:

- `api failure` or `api failures` starts the incident path.
- `compensation`, `sla credit`, or `credit` sets the compensation flag.
- A ticket with neither an incident phrase nor a compensation phrase keeps
  the accepted refund and cancellation behavior.
- An incident phrase, or a compensation phrase, blocks a new refund proposal
  and a new cancellation proposal. The block is in the server proposal
  functions, not only in the fake provider or B1. A forced tool call creates
  no proposal and no dispatch. An already admitted command stays as stored.

## Input, handoff, and reply lifecycle

`invalidate_pending_intent` does not implement this lifecycle. It cancels only
`analyzing`, `gathering_evidence`, `proposing`, `awaiting_customer`, and
`awaiting_approval` runs, and it supersedes drafts only for those runs. An
incident reply waits in `awaiting_reply_review`, which that helper ignores.
`send_reply` checks the draft revision, body hash, status, and recipient, not
the current input. `upsert_reply_draft` returns an existing draft with the
same body hash even when the run or input differs. The new-run gate treats
`awaiting_reply_review` as open, and the workbench does not offer Investigate
in that state. Reusing those helpers unchanged would leave the old reply
sendable and block the replacement run.

Incident drafts therefore do not use `upsert_reply_draft`. Each incident reply
is a new draft bound to one run, one investigated input revision, one report
id when a report exists, and one snapshot digest. The same words on an older
run are not reused.

A new customer or operator message, including a new report, runs an
incident-only invalidation after the message is appended. It does not add
`awaiting_reply_review` to the shared waiting-run list, and it does not change
refund or cancellation send behavior. It supersedes an unsent incident draft
whose investigated revision is no longer current, and it cancels only that
draft's incident run when the run is in `awaiting_reply_review`. The cancelled
run is terminal, so the next investigation can start and the workbench can
offer it. Sent replies stay sent. Admitted refund and cancellation commands
are not edited.

Lock order for the incident report, this invalidation, and the incident send
is the ticket, then the incident run, then the incident reply draft, then the
reply ledger when one exists. Refund and cancellation sends keep their current
order and are not used for an incident-linked draft.

Two races follow from that order:

- New input commits first. The unsent draft is superseded and its run is
  cancelled. The send then finds that draft or run and returns a stale-input
  refusal. No receipt is written. The new investigation is resumable.
- Send commits first. The check sees the same investigated revision, the draft
  becomes sent, and the run completes. The sent receipt remains a historical
  fact. Later input creates a new report, handoff, and draft. It does not
  rewrite the sent receipt.

The current incident run is the latest run whose investigated input revision
equals the ticket's latest customer or operator revision, ordered by
`(created_at, id)`. Only a handoff bound to that run is current. If that run
has no handoff, earlier handoffs are history and are not send targets.

## Decision table

The server classifies one account-scoped snapshot. Overlap uses a half-open
window: `reported_started_at >= started_at`, and, when `resolved_at` is set,
`reported_started_at < resolved_at`. A report start equal to `resolved_at` is
outside that window.

Each owned row receives one class:

- `full`: same service, same region, overlap, and either no request id or the
  request id is in `reference_ids`.
- `request_mismatch`: same service, same region, overlap, and a supplied
  request id that is not listed.
- `resolved_outside`: same service, same region, `resolved_at` is set, and the
  report start is greater than or equal to `resolved_at`.
- `partial`: none of the classes above, and the row shares the service, the
  region, or the time overlap.
- unrelated rows are ignored.

The classifier returns exactly one outcome. Earlier rows win:

| Order | Condition | State | `kind` | `incident_id` | Next step |
| --- | --- | --- | --- | --- | --- |
| 1 | Service, region, or start is missing | Wait | None | None | Ask for a complete report. No handoff row, even if a list was attempted and failed, and even if the text also asks for compensation. |
| 2 | A complete report exists and the list call fails or is malformed | Handoff | `unavailable` | Null | Store the failed observation. No incident id, version, or invented empty-set digest. |
| 3 | Compensation flag, after a complete report | Handoff | `compensation` | Null | A person reviews any credit. None is approved. Matching rows are candidate evidence only. |
| 4 | Exactly one `full` row | Handoff | `confirmed` | That id | Quote status and version. Say the match does not prove cause, and no repair time is known. |
| 5 | More than one `full` row | Handoff | `several` | Null | List those ids in the evidence. Do not pick one. |
| 6 | Any `request_mismatch` | Handoff | `possible` | Null | Reason `request_id_not_listed`. |
| 7 | Any `resolved_outside` | Handoff | `possible` | Null | Reason `resolved_before_report`. Quote `resolved_at`. |
| 8 | Any `partial` | Handoff | `possible` | Null | Reason `partial_overlap`. |
| 9 | Nothing above | Handoff | `none` | Null | Say no matching incident was found. Do not say the service is healthy. |

`confirmed` means the stored record matches the reported service, region, and
time. It does not mean the record caused this customer's errors. No outcome
stores or says a repair time. An incident id from another account or tenant is
not a candidate; the read does not return it, and a supplied foreign id is
rejected without a report or a handoff.

A missing report waits even when the customer also asks for compensation. The
compensation row applies only after the required fields exist. Refund,
cancellation, and credit language on an incident ticket follow the server
refusal above and then this table. They do not open a second financial action.

## Observed snapshot

A decision uses at most three account-scoped list calls.

1. The decision read stores one observation: its `observed_at`, every returned
   candidate, and a digest of the ordered set. The digest covers id, service,
   region, status, start, resolved time, reference ids, and `resource_version`.
2. The validation read runs immediately before the handoff insert. The whole
   digest is compared, not the version of one previously selected row. An
   added or removed incident changes the outcome even when an older row keeps
   its version.
3. If the digests match, the handoff persists the validation snapshot and its
   `observed_at`. If they differ, the server reclassifies from the validation
   snapshot and makes one confirming read. If that third digest matches the
   reclassified snapshot, that third observation is stored. If the third
   digest differs, the kind is `conflict`. If the validation or confirming
   read fails or is malformed, the kind is `unavailable`. That result is not an
   empty list and is not `none`. It stores the failed observation, no incident
   id or version, and no digest of an invented empty set. An earlier successful
   candidate list is historical context only.

There is no fourth read in the run. The persisted result is the snapshot
verified as observed at its `observed_at`. The HTTP read and the application
commit are not one atomic fact, and the reply must not say the record was
proven current at commit time.

The handoff row stores `tenant_id`, `ticket_id`, `run_id`, `report_id` when a
report exists, the investigated input revision, `observed_at`, the snapshot
digest, and a foreign key to the tool invocation for that same tenant and run.
`incident_id` and `incident_resource_version` are set only when `kind` is
`confirmed`. The evidence field keeps every classified candidate that the
decision used, with its match class, status, times, and version, including
possible, several, and resolved-outside rows. For `confirmed`, that field
contains exactly one `full` candidate and its id equals `incident_id`. A
request-id string by itself is not the observation binding.

The current handoff is the row whose investigated revision equals the latest
customer/operator revision. Older rows remain history. All foreign keys include
the tenant, and the observation's run must be the handoff's run.

## Handoff and reply

The handoff insert, internal note, and reply draft commit together. The run
becomes `awaiting_reply_review` and the ticket becomes `awaiting_reply`. The
billing path's `handed_off` status stays the "no refund" outcome and is not
the incident wait state. Sending the draft does not close the ticket, repair
the incident, or notify an external team.

Reply wording follows the outcome:

- Confirmed: name the incident, service, region, and status. Say the match
  does not prove cause and the record has no repair time.
- Possible, including a request id that is not listed or a resolved time at
  or before the report: say these records are not confirmed as the cause, and
  quote the resolved time when that is the reason.
- Several: list the matching ids and say none was selected.
- None: say no matching incident was found, and that this is not a statement
  that the service is healthy.
- Compensation: say a person must review any credit or refund, and none was
  approved. Candidate evidence may be mentioned only as an unconfirmed record.
- Unavailable or conflict: say the result is unknown.

The model cannot supply `kind`, the incident id, or the version. The server
fills those from the persisted snapshot.

## Permissions and migration

Both app tables use forced row-level security and tenant-aware foreign keys
to the ticket, the actor, and the account binding. The handoff also references
its run, its nullable report, and its tool invocation in the same tenant.
`resolveai_api` receives `SELECT` and `INSERT` only. A trigger rejects
`UPDATE` and `DELETE` from `resolveai_api` and `resolveai_worker` with
SQLSTATE `42501`. `resolveai_auth` and `resolveai_worker` receive no
privileges. `resolveai_simulator` can maintain `simulator.incident`.
`resolveai_oracle` can select it. The recovery worker is not granted a new
command and does not read these tables.

A check constraint requires `incident_id` to be null except when `kind` is
`confirmed`, and requires that confirmed id to be the single full candidate
in the stored evidence.

The incident tables are revision `20261102_0010`. Revision `20261106_0011`
requires a confirmed handoff to store a digest and exactly one full candidate
whose id and version are the primary identity. Downgrade counts report rows and
handoff rows. A positive count refuses and changes nothing. A zero count drops
the policies, triggers, grants, and both app tables, then drops
`simulator.incident`. Refund, cancellation, choice, approval, and audit rows
stay. Fixture incident rows disappear with the simulator table. Do not delete
rows to make the downgrade succeed.

## Provider and B1

`list_incidents` is a permitted read. Its account argument is the ticket
account, not a model field. The fake provider and B1 call the same classifier
as the server. Support records the report through the API, not through a model
tool. A forced `propose_refund` or `propose_cancellation` on an incident or
compensation ticket is refused in the server proposal function. Development
traces for this slice should show the tool order for a known record match, a
missing start, no overlap, and a snapshot that changes between reads. Those
traces are not a measure of a live model and are not an agent comparison.

## Acceptance to plan

The scenarios below are design examples. They are not measured results.

Both tenants get fixtures. Northwind covers one record match, a region
mismatch, two full overlaps, no incidents, a missing start, a resolved time
equal to the report start, a request id that is not listed, compensation
before the report is complete, and compensation after a complete report.
Harbor gets one incident on its own account. Tests assert the simulator rows
and the handoff row under a runtime role. Refund, payment, subscription, and
cancellation operation counts stay unchanged.

A missing field waits without a handoff. A later complete report resumes. A
newer report or an ordinary operator message, committed before send,
supersedes the unsent incident draft and allows a new run. The same reply
text does not reuse the old draft. A send that commits first keeps its
receipt, and later input creates a new current handoff. An admitted refund or
cancellation command is not edited.

A forced provider call does not create a refund or cancellation proposal on
an incident ticket. A normal refund ticket that lacks the incident and
compensation phrases still follows the accepted refund path.

An unavailable read says the result is unknown and stores no incident id. A
new incident appearing while an older row's version is unchanged changes a
single match to several, or to conflict if the snapshot does not settle
within three reads. B1 follows the same observations. That is a competence
check, not an agent comparison. Do not start the 240-case evaluation from
this slice. The browser covers a known incident, a clarification that then
resumes, an unavailable read, and reply review, including the unsent draft
disappearing after a newer report. Existing refund, cancellation, recovery,
approval, and tenant browser flows stay in place.
