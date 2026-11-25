# ADR 0009: Period-end subscription cancellation

Status: Accepted locally on 2026-11-02. The slice schedules cancellation at the
current period end for the existing two tenants, with a protected human choice,
separate approval, query-only recovery after admission, and a receipt for that
operation. It does not add immediate termination, renewal, incidents, combined
multi-action execution, hybrid retrieval, or a paid evaluation.

Problem: the accepted path can refund one payment. It cannot schedule the end
of a subscription, and its proposal, ledger, and recovery command are shaped
only for refunds. A customer sentence that says "cancel now" must not become
an immediate loss of access, and a refund approval must not become permission
to change a subscription.

Alternatives: terminate the subscription in the same request; let the model
choose the account, subscription, or effective time; store cancellation as a
refund with a zero amount; add a second worker framework; treat a proposal id
as the business idempotency key; put the resource version inside the operation
identity.

Choice: the only new write schedules `cancel_at_period_end` at the period end
already stored on the subscription, and only while that instant is still in
the future. Access stays active until that time. The operation identity is the
subscription plus that period end. The resource version is an attempt
precondition, not a second operation. The existing recovery worker gains one
command branch. Refund validation, grants, and query-only recovery stay as
accepted.

## Simulator state

`simulator.subscription` is account-scoped, like payments. It has `id`,
`account_id`, `plan_code`, `status`, `current_period_end`,
`cancel_at_period_end`, `resource_version`, and `created_at`.
`current_period_end` is `timestamptz`, stored in UTC at microsecond precision.
`status` is `active` for every row this slice creates. There is no
`terminated` status and no endpoint that clears access or renews a period.

The scheduled effective time is the row's `current_period_end` at the accepted
write. This slice does not move a lifecycle clock.

The read is `GET /accounts/{account_id}/subscriptions`. The write is
`POST /accounts/{account_id}/subscriptions/{subscription_id}/schedule-period-end`.
The body carries `action_id`, `effect_hash`, `expected_resource_version`, and
`effective_at`. The simulator writes only when all of these hold: the version
matches, `effective_at` equals `current_period_end`,
`current_period_end > clock_timestamp()`, and `cancel_at_period_end` is false.
It then sets the flag, increments `resource_version`, and stores one
operation. A passed period end returns `period_elapsed` and writes nothing,
even if the version and timestamp still match.

The same `action_id` and `effect_hash` replay the stored operation and do not
increment the version again. The expected version on a replay is not a new
identity. A different `effect_hash` for that action id returns
`payload_conflict` and writes nothing. A version mismatch on a first write
returns `version_conflict` and writes nothing. If the flag is already true and
this action id has no matching operation, the response is `already_scheduled`
and the row is unchanged. That response is not a receipt for a missing
operation.

Operation lookup stays the existing action-id read. No new connector is added.
The shared local bearer token remains outside tenant row security.

## Time representation

Every period end uses one form in storage, the payload, and the action id:
UTC `timestamptz` rendered as `YYYY-MM-DDTHH:MM:SS.ffffffZ` with six
fractional digits. PostgreSQL's microsecond precision is that same value.
Two period ends that differ by one microsecond are different instants and
different action ids. Truncation to seconds is not used.

Eligibility requires `current_period_end > clock_timestamp()` at proposal time
and again inside the simulator write. Approval that waits until after that
instant does not become a schedule. A POST that reaches the simulator after
the instant receives `period_elapsed` and creates no operation. Query-only
recovery of a write that committed before the instant still confirms that
operation after the instant has passed. Passing time does not erase a real
earlier receipt, and it does not authorize a new POST.

## Two identities

The business action id is

`cancel_at_period_end:{subscription_id}:{effective_at}`

with `effective_at` in the canonical form above. It names one subscription and
one period end. It is not a proposal id.

`effect_hash` is the canonical hash of `action`, `action_id`, `account_id`,
`subscription_id`, `effective_at`, and `policy_version`. It does not include
the resource version. The simulator treats `action_id` plus `effect_hash` as
the same operation.

`payload_hash` on the proposal is the attempt hash. It is the effect fields
plus `expected_resource_version`. A version change keeps the action id and the
effect hash, and changes the attempt hash. The ledger stores the effect hash.
The proposal and the job command store the attempt hash and the expected
version. Refund rows are unchanged: their ledger hash and proposal hash remain
the one refund payload.

## Unsent replacement

Replacement is allowed only while no job for that ledger has `admitted_at`
set and the ledger is still `reserved` and not `confirmed`. The locks are the
accepted order: ticket, proposal, approval, ledger, then job.

A new read with the same action id and effect hash but a new attempt hash
supersedes the old proposal. The old proposal, grant, and cancelled job keep
their original bytes. The ledger stays the one reservation: its effect hash
does not change, and its `proposal_id` moves to the new proposal. Unsent jobs
for the old grant are cancelled in that same transaction. Their commands are
not edited. A new job is enqueued only after a new approval of the new
proposal. Audit records the old and new proposal ids.

Same attempt hash means the same open proposal. A second execute returns the
existing unsent job or the existing confirmation. It does not reserve another
ledger row.

If any job has been admitted, or the ledger is `dispatched`, `settled`, or
`confirmed`, replacement is refused. A later investigation does not open
another POST for that action id. The truthful stop is
`period_already_dispatched`. Recovery continues on the original command only.

A rejected, expired, or revoked grant does not admit. Its unsent job is
cancelled by the existing grant rules. A later unsent proposal may then use
the replacement above. Those old approval rows stay immutable. An admitted
command is not reopened by a new version, a new proposal, or a new grant.

## Version and admission examples

| When the version changes | What is kept | What may happen next |
| --- | --- | --- |
| Before enqueue | No ledger yet. The old unapproved or unexecuted proposal is superseded. | A new proposal and a new grant carry the new attempt hash. One later enqueue reserves the action id. |
| After enqueue, before admission | The old job remains, cancelled, with its original command. No POST ran. | Admission of the old command fails `version_conflict` and does not POST. After replacement and a new grant, a new unsent job may admit the new version. |
| Same version regenerated | The open proposal, grant, and unsent job. | No second reservation and no second operation. |
| Old and new grants both exist | The old proposal is superseded before the new grant is usable. The old grant cannot execute. | Only the new unsent job can reach admission. The old job cannot be admitted beside it. |
| After admission, including unknown and confirmed | The admitted command, its attempt hash, and its action id. | No replacement and no new POST. Unknown stays query-only. Confirmed stays the stored receipt. |

The simulator's stored operation is the first successful write's `action_id`
and `effect_hash`. A later attempt hash is not a second operation.

## Choice and clarification

A subscription id in customer text or model output selects nothing. Support
may record a choice with `POST /tickets/{ticket_id}/subscription-choice` and
a subscription id. The server re-reads the ticket's bound account and stores
the choice only when that id belongs to the account. The row is append-only
and tenant-scoped, with forced row security. It stores `id`, `tenant_id`,
`ticket_id`, `account_id`, `customer_id`, `subscription_id`, `actor_user_id`,
`input_revision`, and `created_at`. The account must be the ticket's bound
account. Runtime roles may insert and read choices; they cannot update or
delete them. The insert bumps the ticket input revision and binds the choice
to that revision. The current choice is the row with the greatest
`input_revision` and then the greatest `id`. An id from another account, or
an id absent from that read, is `choice_not_owned` or `choice_unknown`,
stores nothing, and proposes nothing. A missing or stale choice does not fall
back to a different single eligible subscription.

The fresh read, not the stored id alone, decides the next step. A stale
choice whose id is gone, already scheduled, or past its period end does not
propose. A new choice is a new input revision. It supersedes an unsent
proposal, and the new target needs a new proposal and a new approval. The old
grant cannot execute.

B1 uses this same choice. Several candidates do not end as a handoff. The run
waits. On the next run, a valid stored choice produces the same single target,
or the same explanation, that the fake provider produces.

`U` means active, not scheduled, and the period end is still in the future.
`S` means active and already scheduled. `E` means active, not scheduled, and
the period end is not in the future.

| Owned rows | Stored choice | Result |
| --- | --- | --- |
| One `U`, no `S`, no `E` | Not required | Propose that subscription. |
| One `S`, no `U` | Not required | Explain that effective time. No write. |
| Several `S`, no `U` | Any | Explain each effective time. No write. |
| No active row, or only `E` | Any | Explain that there is nothing to schedule. No write. |
| More than one `U` | None | Wait for a choice. No proposal. |
| One `U` and one or more `E`, no `S` | None | Wait for a choice. Do not propose the unscheduled row. |
| Any `U` together with any `S` | None | Wait for a choice. Do not pick the unscheduled row. |
| Any | Choice is one current `U` on this account | Propose that subscription only. |
| Any | Choice is one current `S` | Explain that schedule. No write. |
| Any | Choice is `E` or missing from the fresh read | No proposal. Ask for another choice. |
| Any | Choice account is not the ticket account | Reject the choice. No row and no proposal. |
| Refund and cancellation both requested | Not applicable | Hand off `combined_intent`. No proposal and no operation. |
| "Terminate immediately", with no later period-end confirmation | Not applicable | Wait or hand off. No proposal and no operation. |
| "Terminate immediately", then support records an explicit period-end choice | That choice | Normal period-end proposal, then a separate approval. Still not immediate termination. |

Immediate termination is never inferred from the first phrase. A period-end
alternative exists only after that explicit later choice, and it still does
nothing until a human approves the proposal.

## Proposal, grant, and ledger

`app.proposal.action_type` may be `refund` or `cancel_at_period_end`. The
migration changes these current `NOT NULL` columns: `target_payment_id`,
`target_event_id`, `amount_cents`, and `currency`. It also changes
`recovery_job.payment_resource_version` from `NOT NULL` to nullable and adds
`subscription_resource_version`. No cancellation row uses a zero amount, a
`USD` currency, a dummy payment id, or a fabricated payment version.

| Field | Refund row | Cancellation row |
| --- | --- | --- |
| `target_payment_id`, `target_event_id` | Required | Null |
| `amount_cents` | Required, greater than zero | Null |
| `currency` | Required, `USD` | Null |
| `target_subscription_id`, `effective_at` | Null | Required |
| `subscription_resource_version` | Null | Required |
| `recovery_job.payment_resource_version` | Required | Null |
| `recovery_job.subscription_resource_version` | Null | Required |

One check constraint makes the shapes mutually exclusive. A trigger on
`recovery_job` reads the ledger action type and refuses a command that
disagrees. Before enqueue commits, `proposal.action_type`,
`ledger.action_type`, and `command.action` are equal; the command's
subscription, effective time, and effect hash match the proposal and the
ledger; and the command's attempt hash and version match the proposal. A
mismatch does not dispatch. Admission checks that agreement again.

Approvals stay immutable and still bind one proposal's attempt hash and one
human approver. Expiry and revocation stay the ADR 0007 rules. A refund grant
cannot admit a cancellation, and a cancellation grant cannot admit a refund
or send a reply.

The shared policy file is `policies/subscription_policy_v1.json`. It states
that the only effect is a future period-end schedule. Both tenants use that
file. Their different effective times come from their own subscription rows.

## Receipt

A cancellation confirms only when the operation for this command's action id
exists and its effect hash, account, subscription, and effective time equal
the command, and the subscription row is that same id, with the flag set and
`current_period_end` equal to the command's effective time. Another
operation, or a flag set without this action id, does not confirm the
command. The result stays unknown or blocked.

If this ledger is already confirmed and a later read is unavailable, the
stored receipt stays. The wording says the earlier confirmation stands and
the latest read was unavailable. It does not describe that unavailable read
as the current subscription state, and it does not clear the confirmation.

## Recovery

The API enqueues and returns. The worker claims through the existing
`app.claim_recovery_job()`. The claim function stays action-agnostic.
`process_available` follows the ledger and command types together, not the
ledger type alone.

Admission rechecks the bound grant, the attempt hash, the input revision, the
live version, the period end, and that the period end is still in the future.
Only that transaction may set `admitted_at`. It authorizes one POST of the
stored command. After `admitted_at`, a restart only queries that action id.
A missing operation, a transport failure, `period_elapsed`, or a version
conflict does not permit another POST. The job blocks, and the ledger stays
unconfirmed unless the original operation matches the receipt rule. Requeue
of `delivery_uncertain` only queries again.

Grant expiry or revocation before admission cancels the unsent job. After
admission it does not clear the flag and does not change the command. The
refund lease, owner token, generation, and lock order apply. No second
framework is added.

## Tenants and the migration

Both synthetic tenants receive their own subscriptions and tickets. Reads and
writes use the ticket's bound account. Cross-tenant ids are denied by row
security and by the account binding check. The simulator remains
account-scoped.

Downgrade is refused, and changes nothing, when any choice row or
cancellation-shaped app row exists. A choice alone is enough, even with no
proposal or job. The detection set is any `subscription_choice` row, a
proposal or ledger whose action type is `cancel_at_period_end`, an approval
of such a proposal, a revocation of such an approval, a recovery job whose
ledger is one of those ledgers or whose `subscription_resource_version` is
not null, and a reply draft referenced by one of those ledgers. Audit rows
are not deleted. Refund proposals, grants, jobs, replies, and payments are
not deleted.

When that set is empty, downgrade restores the refund-only `NOT NULL` checks,
drops the new columns, and drops `simulator.subscription`. Fixture
subscription rows disappear with that table. Refund row contents stay.
Upgrade from that empty state applies cleanly again. The migration does not
drop cluster roles.

The tests cover both paths: a populated cancellation history whose downgrade
fails with unchanged row hashes, and a refund-only history whose downgrade
and reapply preserve those refund hashes.

## What the workbench may say

A proposal may show the period end it would schedule. "Cancellation
scheduled" appears only after the receipt rule confirms. An unknown, blocked,
or period-elapsed action does not use those words. A sent reply for a
confirmed future time says access continues until that time. If the confirmed
time is already past, the reply says the schedule was confirmed for that
time and that this slice does not itself end access. Reject, expiry, and
revocation screens do not say the subscription was changed.

Browser acceptance uses new tickets, not `INV-1001` or `INV-2001`. It covers
one approval, one rejection, one clarification followed by a valid choice,
and one stale or unknown outcome. The existing refund, tenant, grant, and
recovery checks stay green.

## Development traces

B1 uses the same read, choice row, account binding, and safety gateway. It
proposes one eligible subscription, explains schedules, waits for a stored
choice when the table above says to wait, and resumes that choice on the next
run. It hands off for a combined refund and cancellation, for immediate
termination with no later period-end choice, and for no schedulable
subscription. Those sequences are development examples, not a measured
advantage and not a frozen evaluation.

## Cost and reconsideration

The risky migration is the nullability change. A mixed shape must fail, and
downgrade must refuse rather than delete shared history. The worker branch
must not accept a cancellation command that still carries payment fields.

Reconsider immediate termination only as a separate accepted action.
Reconsider choosing a subscription from customer text only with a new
accepted input rule. Combined execution of a refund and a cancellation waits
for the later multi-action slice. Renewal automation stays outside this slice.
