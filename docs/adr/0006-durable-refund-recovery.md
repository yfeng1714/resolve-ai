# ADR 0006: Durable recovery for the existing refund path

Status: Accepted for M3.1 implementation on 2026-10-19. The one-tenant refund
recovery implementation was accepted locally on 2026-10-25. Restart after
admission only queries the original action. A command admitted but never sent
may stay blocked. This is not a production deployment or an exactly-once claim.

Problem: refund execution still finishes inside the API request. The action ledger
can reserve `refund:{payment_id}`, commit a dispatch cutoff, and query that same
identity, but nothing resumes a process that dies between those points.

Alternatives: a second queue or workflow framework; trusting an in-memory graph
checkpoint; retrying a lost response under a new action id; letting the API and a
worker both execute a queued job.

Choice: the API only enqueues a refund recovery job and reports it. A separate
PostgreSQL worker executes it. The action ledger remains the business record. The
job binds one authorized command and one owner at a time. No Redis, broker, or
second application framework. This slice does not recover replies and does not call
the model. It is a local recovery mechanism, not a cross-system exactly-once claim
and not a production claim.

## Reply scope

`send_reply` builds its receipt locally and commits the draft, ledger, ticket
message, and run in one database transaction. There is no simulator reply endpoint
and no separate stored reply approval. M3.1 leaves that atomic send as it is.
Recovery jobs are refund-only. Reply review and the current send regression stay.

## Job schema

`app.recovery_job` is tenant-scoped. Active means `queued`, `leased`, or `blocked`.
A partial unique index allows one active row per `(tenant_id, ledger_id)`.
Settled and cancelled rows remain as history.

| Column | Role |
| --- | --- |
| `id` | Primary key. Worker writes name this row, never "the current job for the action". |
| `tenant_id` | Foreign key to `app.tenant`. Every other app foreign key is tenant-checked. |
| `ledger_id` | Foreign key to `app.action_ledger`. |
| `command_generation` | Immutable for this authorization. The next approval inserts a new row. |
| `proposal_id`, `proposal_revision`, `run_id`, `approval_id` | The authorized attempt. Not rebound later. |
| `authorizer_user_id` | The human approver. Audit uses this id, not a fabricated approver. |
| `payload_hash`, `input_revision`, `payment_resource_version`, `policy_version` | Preconditions copied at enqueue. |
| `command` | Immutable refund command: account, payment, event, amount, currency, action id, payload hash, expected payment version, policy version. |
| `config_snapshot` | Nonsecret `provider`, `model_name` if one was configured, and `prompt_version`, copied at enqueue. `app.run` stores provider and prompt version only, so model name is not read back from the run or from the worker's current settings. |
| `state` | `queued`, `leased`, `blocked`, `settled`, `cancelled`. |
| `owner_token`, `owner_generation` | New token and incremented generation on every claim. |
| `leased_until` | Ownership deadline. A matching token is not enough after this time. |
| `attempt_count`, `max_attempts` | Query attempts. `max_attempts` is 5. |
| `next_attempt_at` | Earliest automatic claim time. Backoff is 1s, 2s, 4s, 8s, then 16s. |
| `admitted_at` | When dispatch was durably admitted. Null means no POST is authorized yet. |
| `last_error_code`, `block_reason` | Persistent reason. A query failure is not a settlement. |
| `created_at`, `updated_at` | Timestamps. |

No browser cookie, password, session, or API key is stored. Refund recovery does
not start an investigation or call a model.

## Ownership

Lock order for every business transaction is ticket, proposal, run, action ledger,
then recovery job. The short claim transaction locks only the job row.

Claim selects one `queued` job whose `next_attempt_at` has arrived, or one `leased`
job whose `leased_until` has passed, with `FOR UPDATE SKIP LOCKED`. It sets a new
`owner_token`, increments `owner_generation`, sets `leased_until` to 30 seconds
ahead, and commits. Thirty seconds is the lease, not a promise of how soon recovery
finishes. Scheduling, backoff, an outage, or contention can take longer.

A renewal updates only `leased_until`. It is one transaction predicated on the same
job id, token, generation, and an unexpired lease. The worker may renew every 10
seconds, including while HTTP is in flight. If the lease has already expired, the
same token cannot renew it. Expiry without a new owner still invalidates the old
process.

Worker writes and API cancellation are different authorities.

A worker write names the job id and changes it only when `owner_token`,
`owner_generation`, and `leased_until > clock_timestamp()` all match. Lease checks
use that database clock, not the process clock. If the predicate matches no row,
the transaction rolls back, including every ledger, proposal, run, or reply change
it attempted. A row-count check after those other rows were already committed is
not fencing.

API cancellation does not use a worker token or an unexpired lease. The signed-in
support or approver path keeps today's tenant, object, and role checks, then locks
ticket, proposal, run, ledger, and job in that order. While `admitted_at` is null,
it may cancel the bound job from `queued`, `leased`, or `blocked`, including a
lease that has expired and has not been claimed again. The same transaction
supersedes the proposal and clears ownership. It does not renew the old lease or
impersonate the worker. A queued job therefore does not stay active and block the
unique index.

Cancellation and admission take the same lock order. If cancellation commits
first, admission sees a cancelled job and does not set `admitted_at`. If admission
commits first, the command stays admitted and the post-admission query rules apply.
A newer approval cancels the old pre-admission row and inserts the next generation.
It does not edit a settled or cancelled row. The API does not call the simulator.

A database lease cannot retract an HTTP request that was already admitted or is on
the wire. A stale process must not apply the response locally. The simulator, not
the lease, decides the external effect.

## Dispatch admission and late requests

Commit A inserts the reserved ledger row and the `queued` job, then the API returns
the job. The worker later rechecks the bound approval, proposal revision and hash,
input revision, payment version, and policy version. Only that transaction may set
`admitted_at`, mark the ledger `dispatched`, and move the run to `executing`. That
commit is B, the admission cutoff. Admission authorizes one POST of the stored
command. It does not record that a packet was sent or that a refund exists.

The POST uses the stored command unchanged: same action id, payload hash, amount,
payment, event, and expected payment version. The simulator's current contract is
the external guard: an `action_id` identifies one operation; the same account and
payload hash replay the stored result; a different payload returns `payload_conflict`
and writes nothing; concurrent creates serialize on the payment lock. A late POST
from an old owner may still commit or replay under that contract. The old owner
then fails its local ownership predicate and writes nothing. The current owner
settles only after a fresh query matches the bound command. A mismatched result
blocks the job and is not reported as success.

Automatic recovery after admission never uses `not_found` as permission to POST.
A missing operation does not prove that an older request has finished. The worker
queries the original action id. `not_found` and transport failure increment
`attempt_count`, set `last_error_code`, and schedule backoff. They do not create
another action id. After five such attempts, or immediately on a payload or
business conflict, the job becomes `blocked`, the ledger stays unconfirmed, and
`block_reason` says why. Further automatic work stops.

An approver may requeue a job blocked for `delivery_uncertain`. That transition
sets `queued`, clears the owner, and resets the query counter. `admitted_at` stays
set, so the worker only queries again. Requeue does not grant a POST, and a later
claim does not create one. Resetting the query counter is not a dispatch
permission. If the operation is still missing when the new query budget ends, the
job blocks again. A command admitted at B and never delivered can therefore remain
unsent. This slice does not add a second outbound attempt. A new refund command
still requires a new approval and a new job, and only while no dispatch has been
admitted. Conflict and stale-authorization blocks cannot be requeued.

## Authorization replacement

The stable action id does not by itself identify which approval is being resumed.
The existing ledger may be pointed from a reserved, withdrawn proposal to a newly
approved proposal for that same action. A recovery job may not follow that rebind.

While `admitted_at` is null and the ledger is still `reserved`, withdrawal cancels
the old job in the same transaction that supersedes its proposal. A later approval
inserts a new active job with the next `command_generation`, bound to the new
proposal, run, revision, hash, and approver. The old worker's update names the old
job id and token, so it cannot dispatch under the new approval and cannot block or
settle the new row. After admission, the ledger and job stay with the command that
was admitted. A newer approval does not rewrite that history.

The worker is a service principal, not an approver session. Before admission it
rechecks the stored approval row: same proposal, revision, payload hash, and
`approved` decision. It does not call `_require_approver` and does not invent an
approver. Approval expiry and revocation beyond this bound check remain a later
slice.

## State transitions

| From | To | Who | Condition in the same transaction |
| --- | --- | --- | --- |
| — | `queued` | API | Commit A. Ledger reserved. No active job for that ledger. |
| `queued` | `leased` | Worker | Claim. New token and generation. |
| `leased` | `leased` | Current owner | Renewal only, lease still unexpired. |
| expired `leased` | `leased` | Worker | New claim. New token and generation. The old token cannot renew. |
| `leased` | `queued` | Current owner | Retryable query result, attempts remain, backoff set, owner cleared. |
| `leased` | `blocked` | Current owner | Stale authorization before admission, conflict, or attempts exhausted. |
| `queued` | `cancelled` | API | Pre-admission withdrawal. No worker token required. |
| `leased` | `cancelled` | API | Pre-admission withdrawal, including an expired lease with no new claim. |
| `blocked` | `cancelled` | API | Pre-admission withdrawal. `admitted_at` is still null. |
| `leased` | `settled` | Current owner | Ledger `confirmed` in this transaction, or ledger already `confirmed`. No new refund. The claim loop does not select `settled` rows. |
| `blocked` | `queued` | Approver | Requeue of `delivery_uncertain` only. Queries restart. POST count stays zero. |
| `settled`, `cancelled` | — | — | Terminal. Not an ordinary claim source. |

A job must not be displayed as settled while its ledger is unconfirmed. The
ordinary claim loop does not visit settled rows, so it cannot repair that pair.
An explicit repair check, separate from claim, reports it as corruption and does
not show success. The recoverable crash pair is the opposite one: the ledger is
already `confirmed` and the job is still `leased`. The next owner settles that
job and does not submit another refund.

## Failure matrix

| Durable state | What it proves | Automatic result | Test |
| --- | --- | --- | --- |
| A committed, B not committed. Ledger `reserved`, job active, `admitted_at` null. | No dispatch has been admitted. | Dispatch only if the bound approval and input are still current. | Kill after A. Restart the worker. One refund only when authorization is current. |
| Authorization withdrawn before B. The worker is stopped, the job may be queued, leased, expired, or blocked, and `admitted_at` is null. | The API can cancel without a worker lease. | The old row is `cancelled`. A later approval inserts the next generation. | Withdraw immediately after A. Cancel an expired lease with no new claim. Cancel a pre-admission blocked job. Then approve again. Zero stale refunds. The new job is not blocked by the unique index. |
| Cancellation overlaps admission. | The lock order decides one winner. | Cancellation first means no `admitted_at`. Admission first keeps the command and uses query-only recovery. | Overlap withdrawal with B. The admitted command is unchanged. |
| `delivery_uncertain`, then an approver requeues. | Queries may run again. No new POST was granted. | POST count is zero before and after claim, including a restart before the next claim. | Requeue, stop the worker, restart it, and count simulator creates. |
| B committed, POST not invoked. | A POST is authorized. None is proven. | Query only. `not_found` backs off, then `blocked` / `delivery_uncertain`. No new action id. | Kill after B and before POST. Zero refunds. Job blocked. |
| POST in flight and lookup is `not_found`. | An older request may still commit. | Do not POST automatically. A later matching query may settle. | Pause inside POST, observe `not_found`, then let it commit. Exactly one refund. |
| Lookup timeout or simulator unavailable. | Nothing about the business result. | Count the attempt and back off. Exhaustion blocks. | Unavailable lookup. No second action id. |
| Lookup conflicts with the bound command. | The effect is not this authorization. | Block immediately. Ledger not confirmed. | Mismatched operation or refund. No success reply. |
| Simulator effect confirmed, local confirm not committed. | The business refund exists. | Query, then confirm the ledger and settle the job together. | Kill after the simulator commit and before C. One refund, ledger confirmed, job settled. |
| Ledger confirmed, job still leased. | Local business confirm happened. | Settle the job only. | Restart on this pair. Refund count unchanged. |
| Job settled, ledger not confirmed. | Invalid bookkeeping. The claim loop does not select this row. | The explicit repair check does not report success. | Direct fixture of the invalid pair, read through the repair entry rather than a normal claim. |
| Owner A passed its last ownership check; the lease then moves to B. | A's check is stale. | A's following write matches zero rows. B proceeds. | Pause A after the check, reassign, resume A. |
| Lease expires and nobody else claims. | The old token is dead even though it is unchanged. | A cannot renew or write. A later claim uses a new token. | Expire without reassignment. |
| API cancels or replaces a job while a worker claims it. | One transaction owns the ticket and job. | The loser sees the predicate fail. | Overlap the API withdrawal with a claim. |

The restart test runs the worker in a subprocess, kills it after a named commit,
starts a new process, and reads `app.action_ledger` and `simulator.refund`. An
exception inside the same process is not that test. The workbench shows queued,
leased, blocked, and settled beside the existing action. Approve, reject, login,
reinvestigation, withdrawal, and reply review stay on their current paths.

## Migration and stop

One Alembic revision adds `app.recovery_job`, its checks, tenant-aware foreign keys,
and the active-job unique index. It does not change ledger statuses or action ids.
Rows created before the migration have no job; the existing manual reconcile call
remains their recovery path.

The local recovery slice was accepted on 2026-10-25 after those checks. A second
tenant, subscriptions, and hybrid retrieval remain later work. Refund approval
expiry and revocation are proposed separately in ADR 0007.
