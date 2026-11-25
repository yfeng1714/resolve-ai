# ADR 0007: Refund approval expiry and revocation

Status: Accepted for M3.2 implementation on 2026-10-25. The one-tenant
refund approval lifecycle was accepted locally on 2026-10-26. The slice is the existing
one-tenant refund path. It does not add a second tenant, subscriptions,
retrieval changes, or a paid evaluation.

Problem: an approval is one immutable row per proposal. It binds the human
approver, revision, and payload hash, but it never expires and cannot be revoked.
Admission rechecks that row, then a queued job can still be dispatched after the
grant should have ended. The schema also cannot record a later grant without
replacing the earlier decision.

Alternatives: mutate `expires_at` and `decision` on the one approval row; give an
expired grant a new action id; let revocation recall a simulator refund; treat a
development TTL as a customer-support policy; let expiry "win" by committing
ahead of admission.

Choice: keep every approval decision immutable. Store an expiry deadline beside
it, and append a revocation row instead of editing the decision. Admission's
final database write checks the bound grant against the database clock. Expiry
is that predicate, not a competing transaction. Revocation and expiry before
admission cancel only the unsent job bound to that grant. After admission they
do not change the stored command or claim that an external refund was undone.

## Development TTL

`APPROVAL_TTL_SECONDS` is a development setting, not a support policy. The
accepted design default is 86400. Startup rejects a missing, non-integer, or
non-positive value, and any value above 2592000 (30 days). That cap only blocks
an unbounded configuration mistake. Tests may set a shorter positive value.

Only an authenticated approver may revoke. Support cannot. The actor id is the
signed-in approver, never a request field or a model label. A revocation reason
is required text, at most 500 characters, and is stored and shown as entered.
Reply send stays the current one-transaction receipt. This slice does not add a
reply approval.

## Grant and history

`app.approval` keeps `decision`, `revision`, `payload_hash`, `actor_user_id`,
and `created_at`. Those columns are not updated after insert. A new non-null
`expires_at` is set in the insert to
`clock_timestamp() + make_interval(secs => :ttl)`.

The migration drops `approval_proposal_id_key`. Several grants for one proposal
remain. `app.approval_revocation` is append-only: `tenant_id`, `approval_id`,
`actor_user_id`, `reason`, and `created_at`. `approval_id` is unique. A second
revoke of the same grant returns the existing row and inserts nothing.
Tenant-aware foreign keys match the recovery job. No approval or revocation row
is updated or deleted by the application.

The runtime role receives SELECT and INSERT on approval history. PostgreSQL
requires an UPDATE privilege to take `FOR UPDATE`, so that role may hold the
row lock through `approval.id` only. A trigger rejects every actual UPDATE or
DELETE from `resolveai_api`, including a change to the approval id, decision,
deadline, or authorizer. The initializer role can still backfill `expires_at`.
Tests must hit this boundary with the API role. It is not tenant RLS.

Existing approval rows cannot reconstruct their original window. The migration
sets their `expires_at` to `clock_timestamp()` plus the development TTL so the
local demo is not immediately stranded. Grant identities and admitted commands
stay as they are. That backfill is a development-database choice.

A grant is valid only when `decision = 'approved'`, its revision and payload
hash equal the proposal's current revision and hash,
`expires_at > clock_timestamp()`, and no revocation row exists. Expiry is that
clock comparison. It has no event row and does not commit. Rejected approvals
are unchanged and cannot be revoked.

## Which grant is current

One rule serves approve, enqueue, execute, revoke, and presentation. Order is
`(created_at, id)`.

The current grant of a proposal is the latest approval row for that proposal
and tenant whose decision is `approved`, whose revision and payload hash equal
that proposal, and which is valid now. A second currently valid grant for the
same proposal is rejected under the ticket and proposal locks. There is no
partial unique index for "valid", because validity depends on the clock and the
revocation table.

When no row is valid, there is no current grant. Presentation uses the latest
approval row for that proposal anyway, and labels it `rejected`, `revoked`, or
`expired`. If the proposal has no approval row, the label is `none`. History
for the proposal is every approval row in `(created_at, id)` order, each with
its own deadline and revocation. The workbench badge is the latest row's
label, not an unordered map from proposal id to an arbitrary row.

A recovery job stores `approval_id` and `authorizer_user_id` at enqueue. Later
reads of that job use that approval id. They do not follow a newer grant.
Enqueue of a new attempt binds the current grant's id and the human who made
that grant.

Revoke names an approval id. It appends history for that grant only. If G1 has
expired and G2 is the current grant with its own queued job, a late revoke of
G1 does not cancel G2's job, reopen G2's proposal, or change G2's authorizer.
Superseded, rejected, and already invalid grants are not revived.

## Lazy release before admission

`release_invalid_grant` is the shared transition. Approve, execute, and revoke
call it for the grant they are about to use or have just revoked. It also runs
in a new transaction after admission rolls back because the grant predicate
failed. GET ticket detail only displays status. It does not call this
transition. No scheduler is added. The ordinary claim loop is not the trigger:
it does not visit blocked jobs, and a stopped worker must not be required.

The transition does not take a worker lease. It locks ticket, proposal,
approval, run, ledger, and any bound job, then refreshes those rows. It cancels
a job only when that job's `approval_id` is the invalid grant, `admitted_at` is
null, and its state is `queued`, `leased`, or `blocked`, including a lease that
has already expired. Cancellation sets `block_reason` to `approval_expired` or
`approval_revoked` and clears the owner, which frees the active-job index. Jobs
for any other approval id are left alone. An admitted job is left alone.

It returns the grant's proposal to `open` only when all of these hold: the
proposal status is `approved`, the grant's revision and hash still match it,
the proposal is the latest proposal on the ticket, the ticket's latest input
revision still matches the proposal, and no job for its ledger is admitted.
It does not change a superseded, rejected, executed, stale, or failed proposal,
and it does not undo newer customer input or a newer proposal.

With no recovery job, the same proposal rule still reopens an eligible current
proposal whose grant has expired or been revoked. Execute after that, without
a new approval, returns `approval_expired` or `approval_revoked` and does not
enqueue. Approve can then insert the next grant.

## Admission

Lock order is ticket, proposal, approval, run, action ledger, recovery job.
Identifiers are read without those locks. After the locks, each row is refreshed
before it is read. The claim transaction still locks only the job. Revocation
takes the same approval lock, so it cannot interleave with the admission
transaction's view of that row. Expiry needs no lock of its own: the deadline
can pass while admission holds the row.

Commit B may set `admitted_at` only from the final conditional write, after any
slow external read and after pending business and audit writes have been
flushed. That write matches the job id, owner token, owner generation, and
`leased_until > clock_timestamp()`, and the job's own `approval_id`. The joined
grant must be `approved`, match the bound revision and payload hash, have
`expires_at > clock_timestamp()`, and have no revocation row. An earlier API
approval or enqueue does not skip this write. The worker lease may still be
valid when the grant deadline passes. A miss rolls back the whole admission
transaction, including ledger, run, job, and audit changes. Cleanup is the
separate `release_invalid_grant` transaction, not more writes inside the
failed one.

This grant predicate is only for setting `admitted_at`. Query-only settlement
of a job that already has `admitted_at` does not require the grant to still be
valid. An admitted result is still recorded after expiry or revocation.

Before B, the released job is not posted. An expired worker lease does not let
the old owner admit a stale grant. If revocation commits before B, the
admission predicate sees the revocation row and rolls back. If admission's
conditional write commits first, the command stays immutable and later recovery
only queries it. Revocation then inserts its row and does not cancel the job,
change the action id, or call the simulator. A lease cannot retract a request
already admitted or on the wire. Confirmation after revocation reports the
original external result. It does not say the refund was reversed.

## Other entry points

Execute uses the current-grant rule. It does not enqueue when that grant is
missing, expired, or revoked. Manual reconcile and repeat execute do not admit
and do not post. After the bound grant expires or is revoked, they still return
a stored confirmation: the ledger stays `confirmed`, and an already sent reply
keeps its body and receipt. An unsent uncertainty draft may become the
confirmed draft when a later query verifies the admitted command. That draft
has no send receipt until someone sends it.

Requeue remains query-only for an admitted `delivery_uncertain` job. Expiry or
revocation does not turn it into a POST. A restarted worker uses the admission
rule above only when `admitted_at` is null. Otherwise it queries the original
action id.

## Workbench

Ticket detail shows the latest grant's label, `valid`, `expired`, `revoked`,
`rejected`, or `none`, plus `expires_at` and any revocation reason. History
remains available in created order. English and Simplified Chinese follow the
existing language menu. A confirmed refund stays visible. A sent reply stays
visible. The status must not imply that a confirmed effect disappeared.

## Migration, API, and evidence

One Alembic revision follows `20261020_0004`. It adds `expires_at`, drops
`approval_proposal_id_key`, creates `app.approval_revocation`, revokes runtime
UPDATE and DELETE on approval history, and grants SELECT and INSERT. It does
not change action ids, ledger statuses, or admitted commands.

`POST /approvals/{approval_id}/revoke` accepts `{"reason": "..."}` from an
approver and then releases that grant if it was still pre-admission. Approve
and execute release an invalid current grant before their own checks. Ticket
detail gains the latest-grant label, deadline, reason, and the ordered history.
Refund recovery otherwise keeps its current routes.

Evidence uses the fake provider, a zero budget, and `resolveai_pytest`. Keep the
M3.1 recovery suite, the nine manager regressions, and the current browser
suites green. Add:

- grant expiry during a slow eligibility read, and during a blocked SQL flush,
  while the worker lease remains valid; admission rolls back and does not post
- expiry and revocation while queued, and while leased, before admission
- an expired lease whose old owner cannot admit a stale grant
- revocation and admission in both lock orders
- no recovery job, worker stopped, expired grant, then a fresh approval
- a blocked pre-admission job, worker stopped, expired grant, then a fresh
  approval, with the active-job index free
- G1 expired, G2 approved and enqueued, then a late revoke of G1 that leaves
  G2's job, proposal, and authorizer unchanged
- a stale cached grant reloaded under the locks
- requeue and a new worker after the grant expires, still query-only
- revocation after admission, then confirmation of the original simulator
  result; a sent body and receipt stay, while an unsent uncertainty draft may
  become the confirmed draft
- repeat execute and manual reconcile after that expiry or revocation, with
  the confirmed ledger and any sent reply unchanged
- support denied, startup rejection of a non-positive or too-large TTL, and
  the API role denied an update of an approval decision and denied DELETE on
  revocation history

One browser test is enough: an approver revokes a not-yet-admitted refund, the
workbench shows the reason, and no refund appears. Expiry races stay in the
database tests. No deployment and no paid model batch.

Implementation waits for this revision to be accepted. A second tenant,
subscriptions, hybrid retrieval, and comparative evaluation remain separate.
